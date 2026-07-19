#!/usr/bin/env python3
"""
Levels-sheet generator for ES / CL / GC futures, run by GitHub Actions.

Pulls Yahoo Finance chart data (1m intraday + daily history), computes the
numbers a trader actually anchors on — session OHLC, VWAP, volume POC,
prior-session levels, ATR, multi-day ranges — and writes a compact
levels.json that claude.ai chat fetches on demand.

Note: Yahoo futures quotes are ~10-15 min delayed (CME licensing). All
stored times are UTC; ET strings included for display.
"""

import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SYMBOLS = {
    "ES": {"yahoo": "ES=F", "name": "E-mini S&P 500", "bin": 1.0, "nd": 2,
           "proxies": ["SPY", "IVV", "VOO"]},
    "CL": {"yahoo": "CL=F", "name": "WTI Crude Oil", "bin": 0.05, "nd": 2,
           "proxies": ["USO", "DBO", "USL"]},
    # GLD may be a delayed quote on Yahoo while its peers are real time;
    # rather than hard-code a guess, all three are measured each run and
    # the freshest wins (see pick_proxy)
    "GC": {"yahoo": "GC=F", "name": "Gold", "bin": 0.5, "nd": 1,
           "proxies": ["GLD", "IAU", "GLDM"]},
}
ET = ZoneInfo("America/New_York")
UA = "fj-market/1.0 (personal levels sheet)"
RATIO_WINDOW = 30    # trailing 1m bars used to fit the ETF→futures ratio
RATIO_MIN_BARS = 10  # below this the fit is noise; report no proxy instead
MAX_PROXY_RELERR = 0.0015  # 15 bps of price — beyond this the ETF isn't tracking
OUT = Path(__file__).parent / "levels.json"


def fetch_chart(symbol: str, interval: str, range_: str) -> list[dict]:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?interval={interval}&range={range_}")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    res = data["chart"]["result"][0]
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c = (q["open"][i], q["high"][i],
                      q["low"][i], q["close"][i])
        if None in (o, h, l, c):
            continue
        bars.append({"t": t, "o": o, "h": h, "l": l, "c": c,
                     "v": q["volume"][i] or 0})
    return bars


def globex_open_ts(epoch: int) -> float:
    """Epoch of the most recent Globex open (18:00 ET) at/before epoch."""
    et = datetime.fromtimestamp(epoch, ET)
    day = et.date() if et.hour >= 18 else et.date() - timedelta(days=1)
    return datetime(day.year, day.month, day.day, 18, 0,
                    tzinfo=ET).timestamp()


def session_slice(bars: list[dict]) -> list[dict]:
    """Bars since the most recent Globex open at or before the newest bar.

    Needed because the fetch may span multiple days (see fetch_intraday):
    'session' must keep meaning the futures session, not whatever window
    Yahoo happened to return.
    """
    start = globex_open_ts(bars[-1]["t"])
    return [b for b in bars if b["t"] >= start]


def fetch_intraday(symbol: str) -> list[dict]:
    """1m bars with a range fallback.

    Yahoo's range=1d returns ZERO bars in the stretch right after a
    session opens (verified live at the Sunday 18:00 ET reopen — 1d was
    empty while 2d served Friday's bars fine). Try 1d, fall back to 2d.
    The caller gets the newest data Yahoo has; if that is still the
    prior session, the sheet correctly shows the prior session rather
    than nothing.
    """
    for rng in ("1d", "2d"):
        bars = fetch_chart(symbol, "1m", rng)
        if bars:
            return bars
        time.sleep(1)
    return []


def et_date(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, ET).strftime("%Y-%m-%d")


def et_str(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, ET).strftime("%Y-%m-%d %H:%M ET")


def resample(bars: list[dict], minutes: int, keep: int, nd: int) -> list[list]:
    """Downsample 1m bars into `keep` most recent `minutes`-wide candles.

    Emitted as bare arrays [et_hhmm, o, h, l, c, v] rather than objects —
    this is the bulkiest part of the sheet and chat reads it fine as rows.
    """
    buckets: dict[int, list[dict]] = {}
    for b in bars:
        buckets.setdefault(b["t"] - (b["t"] % (minutes * 60)), []).append(b)
    out = []
    for start in sorted(buckets)[-keep:]:
        g = buckets[start]
        out.append([datetime.fromtimestamp(start, ET).strftime("%H:%M"),
                    round(g[0]["o"], nd), round(max(x["h"] for x in g), nd),
                    round(min(x["l"] for x in g), nd), round(g[-1]["c"], nd),
                    sum(x["v"] for x in g)])
    return out


def regime(cfg: dict, bars: list[dict], vwap: float, atr14: float) -> dict:
    """Momentum-vs-mean-reversion read, measured rather than eyeballed.

    Lag-1 autocorrelation of 1m returns is the direct statistical test:
    positive => moves tend to continue (momentum), negative => they tend
    to reverse (fade). Computed over three windows because the answer is
    routinely different at 30m and 120m.
    """
    nd = cfg["nd"]
    closes = [b["c"] for b in bars]
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes))]

    def autocorr(window: int) -> float | None:
        r = rets[-window:]
        if len(r) < 20:
            return None
        mu = sum(r) / len(r)
        dev = [x - mu for x in r]
        var = sum(d * d for d in dev)
        if var == 0:
            return None
        cov = sum(dev[i] * dev[i + 1] for i in range(len(dev) - 1))
        return round(cov / var, 3)

    hi, lo = max(b["h"] for b in bars), min(b["l"] for b in bars)
    last = closes[-1]

    # realized vol of the session, annualised to a daily figure so it is
    # comparable against ATR14 — >1 means today is wider than the norm
    n = len(rets)
    mu = sum(rets) / n if n else 0
    sd = (sum((x - mu) ** 2 for x in rets) / n) ** 0.5 if n else 0
    rv_daily = sd * (n ** 0.5) * last
    vol_vs_atr = round(rv_daily / atr14, 2) if atr14 else None

    # how far price sits from VWAP, in session sigma
    disp = [abs(b["c"] - vwap) for b in bars]
    sigma = (sum(d * d for d in disp) / len(disp)) ** 0.5
    vwap_z = round((last - vwap) / sigma, 2) if sigma else None

    # current streak of consecutive same-direction 1m closes
    streak = 0
    for i in range(len(closes) - 1, 0, -1):
        step = 1 if closes[i] > closes[i - 1] else (
            -1 if closes[i] < closes[i - 1] else 0)
        if step == 0 or (streak and step != (1 if streak > 0 else -1)):
            break
        streak += step

    return {
        "autocorr_1m_30": autocorr(30),
        "autocorr_1m_60": autocorr(60),
        "autocorr_1m_120": autocorr(120),
        "autocorr_hint": ("positive => momentum (moves continue); "
                          "negative => mean reversion (fades work); "
                          "|value| under ~0.05 is noise, treat as neither"),
        "range_position": round((last - lo) / (hi - lo), 2) if hi > lo else None,
        "vwap_dist": round(last - vwap, nd),
        "vwap_z": vwap_z,
        "realized_vol_vs_atr14": vol_vs_atr,
        "streak_1m_bars": streak,
    }


def fit_proxy(etf: str, cfg: dict, fut_by_t: dict) -> dict:
    """Fit one candidate ETF against the futures bars.

    futures ≈ etf * ratio. The ratio moves far slower than either leg (an
    ES/SPY ratio drifts ~0.01 across a whole session), so a ratio built
    from delayed bars still prices the front month right now — a
    deliberately 15-min-stale ratio came in under one tick on all three
    symbols when tested.

    Median (not mean) over the window: one bad Yahoo print otherwise
    skews the ratio for the next ten minutes.
    """
    etf_bars = fetch_intraday(etf)
    etf_by_t = {b["t"]: b["c"] for b in etf_bars}
    common = sorted(set(fut_by_t) & set(etf_by_t))[-RATIO_WINDOW:]
    if len(common) < RATIO_MIN_BARS:
        raise ValueError(f"only {len(common)} bars overlap {etf}")

    ratios = sorted(fut_by_t[t] / etf_by_t[t] for t in common)
    ratio = ratios[len(ratios) // 2]
    # residual of the median ratio across the window — a live tracking
    # check, so a relationship that has broken down shows up in the sheet
    errs = sorted(abs(etf_by_t[t] * ratio - fut_by_t[t]) for t in common)
    newest = max(etf_by_t)
    med_err = errs[len(errs) // 2]
    return {
        "etf": etf,
        "ratio": round(ratio, 6),
        # the premise of the whole proxy is that this stays near 0-2
        # during RTH. If it tracks the futures bar age instead, this
        # candidate is a delayed quote and buys us nothing.
        "etf_bar_age_min": round((time.time() - newest) / 60, 1),
        "etf_last": round(etf_by_t[newest], 2),
        "etf_last_et": et_str(newest),
        "quote_url": (f"https://query1.finance.yahoo.com/v8/finance/chart/"
                      f"{etf}?interval=1m&range=1d"),
        "ratio_asof_et": et_str(common[-1]),
        "bars_used": len(common),
        "fit_err_median": round(med_err, cfg["nd"]),
        "fit_err_max": round(errs[-1], cfg["nd"]),
        "_newest": newest,
        "_relerr": med_err / fut_by_t[max(fut_by_t)],
    }


def pick_proxy(sym: str, cfg: dict, fut_bars: list[dict]) -> dict:
    """Measure every candidate ETF and keep the freshest good tracker.

    Yahoo serves some NYSE Arca ETFs real time and others delayed, and
    the chart payload carries no field that says which — the meta is
    byte-identical for a real-time and a delayed symbol. So the only
    honest way to tell them apart is to observe bar age at run time and
    let the data choose. Candidates that track badly are dropped first,
    then the freshest of the survivors wins.
    """
    fut_by_t = {b["t"]: b["c"] for b in fut_bars}
    fits, errors = [], []
    for etf in cfg["proxies"]:
        try:
            fits.append(fit_proxy(etf, cfg, fut_by_t))
        except Exception as e:
            errors.append(f"{etf}: {e}")
        time.sleep(1)
    if not fits:
        raise ValueError("; ".join(errors) or "no proxy candidates fit")

    # tracking quality gate first — a fresh quote on an ETF that has
    # decoupled from the future is worse than a stale one that tracks.
    # Freshness is judged by newest BAR TIMESTAMP, not wall-clock age:
    # data-derived, so identical data always picks the same winner. The
    # old round(wall-age) key straddled rounding boundaries on cron
    # jitter and flip-flopped the ETF all weekend, committing junk.
    good = [f for f in fits if f["_relerr"] <= MAX_PROXY_RELERR] or fits
    good.sort(key=lambda f: (-f["_newest"], f["_relerr"]))
    best = good[0]
    best["alternates"] = [
        {"etf": f["etf"], "bar_age_min": f["etf_bar_age_min"],
         "fit_err_median": f["fit_err_median"]}
        for f in fits if f["etf"] != best["etf"]]
    if errors:
        best["candidate_errors"] = errors
    return {k: v for k, v in best.items() if not k.startswith("_")}


def compute(sym: str, cfg: dict, prev: dict | None = None) -> dict:
    nd = cfg["nd"]
    m1 = fetch_intraday(cfg["yahoo"])
    time.sleep(1)  # be polite to Yahoo
    d1 = fetch_chart(cfg["yahoo"], "1d", "3mo")
    if not m1 or not d1:
        raise ValueError(f"{sym}: empty chart data")
    m1 = session_slice(m1)

    last = m1[-1]
    session_date = et_date(last["t"])
    # age of the newest bar. While a session is live this IS the feed
    # delay (plus <1 min for the bar to close), so every run measures it
    # instead of us assuming the oft-quoted 10-15 min.
    lag_min = round((time.time() - last["t"]) / 60, 1)

    # session stats from 1m bars
    vol_total = sum(b["v"] for b in m1)
    if vol_total:
        vwap = (sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in m1)
                / vol_total)
    else:
        vwap = sum(b["c"] for b in m1) / len(m1)

    # volume point-of-control: bin closes, weight by volume
    # (fall back to time-at-price if Yahoo volume is all zero)
    bins: dict[float, float] = {}
    width = cfg["bin"]
    for b in m1:
        key = round(b["c"] / width) * width
        bins[key] = bins.get(key, 0) + (b["v"] if vol_total else 1)
    poc = max(bins, key=bins.get)

    # completed daily bars strictly before the current session. Compare
    # epochs against the Globex open, NOT calendar-date strings: Yahoo
    # stamps completed daily bars 00:00 ET of their trade date, so a
    # date-string comparison excludes the just-settled session for the
    # whole 18:00-24:00 ET evening (prior_session was a full session
    # stale during prime Globex hours — review finding, verified live).
    # The in-progress daily bar is stamped with the current wall clock,
    # which lands after the open and is correctly excluded.
    open_ts = globex_open_ts(last["t"])
    prior_days = [b for b in d1 if b["t"] < open_ts]
    if not prior_days:
        raise ValueError(f"{sym}: no prior daily bars")
    prev_day = prior_days[-1]

    trs = []
    for i in range(1, len(prior_days)):
        h, l = prior_days[i]["h"], prior_days[i]["l"]
        pc = prior_days[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr14 = sum(trs[-14:]) / min(14, len(trs)) if trs else None

    def rng(n):
        w = prior_days[-n:]
        return (round(max(b["h"] for b in w), nd),
                round(min(b["l"] for b in w), nd))

    hi5, lo5 = rng(5)
    hi20, lo20 = rng(20)

    # the proxy is an enhancement, not a dependency — a broken ETF fetch
    # must not cost us the levels for this symbol. And a still-valid old
    # ratio beats no ratio (staleness measured: ES/CL flat out to 2h),
    # so fall back to the previous run's proxy before giving up.
    time.sleep(1)
    try:
        proxy = pick_proxy(sym, cfg, m1)
    except Exception as e:
        old = (prev or {}).get("live_proxy") or {}
        if "ratio" in old:
            proxy = {k: v for k, v in old.items()
                     if k not in ("stale", "stale_reason")}
            proxy["stale"] = True
            proxy["stale_reason"] = str(e)
        else:
            proxy = {"candidates": cfg["proxies"], "error": str(e)}

    return {
        "name": cfg["name"],
        "yahoo_symbol": cfg["yahoo"],
        "last": {"price": round(last["c"], nd),
                 "time_utc": datetime.fromtimestamp(
                     last["t"], timezone.utc).isoformat(),
                 "time_et": et_str(last["t"]),
                 "bar_age_min": lag_min},
        "live_proxy": proxy,
        "regime": regime(cfg, m1, vwap, atr14),
        "bars_5m": resample(m1, 5, 60, nd),
        "bars_5m_cols": ["time_et", "o", "h", "l", "c", "v"],
        "session": {"date_et": session_date,
                    "open": round(m1[0]["o"], nd),
                    "high": round(max(b["h"] for b in m1), nd),
                    "low": round(min(b["l"] for b in m1), nd),
                    "vwap": round(vwap, nd),
                    "volume_poc": round(poc, nd),
                    "volume": vol_total},
        "prior_session": {"date_et": et_date(prev_day["t"]),
                          "high": round(prev_day["h"], nd),
                          "low": round(prev_day["l"], nd),
                          "close": round(prev_day["c"], nd)},
        "ranges": {"atr14": round(atr14, nd) if atr14 else None,
                   "high_5d": hi5, "low_5d": lo5,
                   "high_20d": hi20, "low_20d": lo20},
    }


def upcoming(sym: str, now: datetime, limit: int = 3) -> list[dict]:
    """Next scheduled releases that move this symbol.

    Read from events.json (written by events.py earlier in the same
    workflow) and folded in here so chat gets levels and event risk in a
    single fetch. minutes_until is recomputed against this run's clock
    rather than trusting the value events.py stamped.
    """
    path = OUT.parent / "events.json"
    if not path.exists():
        return []
    try:
        events = json.loads(path.read_text(encoding="utf-8"))["events"]
    except (json.JSONDecodeError, OSError, KeyError):
        return []
    out = []
    for e in events:
        if sym not in e.get("moves", []):
            continue
        try:
            when = datetime.strptime(
                e["when_et"], "%Y-%m-%d %H:%M ET").replace(tzinfo=ET)
        except ValueError:
            continue
        mins = int((when - now).total_seconds() // 60)
        if mins < 0:
            continue
        out.append({"name": e["name"], "when_et": e["when_et"],
                    "minutes_until": mins,
                    **({"note": e["note"]} if e.get("note") else {})})
        if len(out) == limit:
            break
    return out


def stable(symbols) -> str:
    """Serialise the sheet with clock-derived fields removed.

    Age and countdown fields advance on every run by construction, so a
    naive equality check against the previous file never matches and the
    workflow commits every 10 minutes even with the market shut. Compare
    only the parts that move when the *market* moves.
    """
    # stale_reason is scrubbed too: failure messages can embed varying
    # counts ("only 3 bars overlap"), and a reworded failure is not a
    # market change. The stale flag itself still triggers a commit when
    # it flips.
    volatile = {"bar_age_min", "etf_bar_age_min", "minutes_until",
                "stale_reason"}

    def scrub(node):
        if isinstance(node, dict):
            return {k: scrub(v) for k, v in node.items()
                    if k not in volatile}
        if isinstance(node, list):
            return [scrub(v) for v in node]
        return node

    return json.dumps(scrub(symbols), sort_keys=True)


def main() -> None:
    now = datetime.now(timezone.utc)
    out = {"updated_utc": now.isoformat(),
           "updated_et": now.astimezone(ET).strftime("%Y-%m-%d %H:%M ET"),
           "note": ("Yahoo futures are exchange-delayed: ES(CME) ~10 min, "
                    "CL(NYMEX) and GC(COMEX) ~30 min. bar_age_min fields "
                    "were measured at BUILD time and this file is only "
                    "rewritten when the market moves - compute the true "
                    "age as now minus last.time_utc. A symbol or "
                    "live_proxy carrying stale:true is retained data from "
                    "an earlier run kept through a fetch failure. Session "
                    "= latest available; on weekends that is Friday's. "
                    "For a real-time price use live_proxy: fetch its "
                    "quote_url for the ETF's last price and multiply by "
                    "ratio."),
           "symbols": {}}
    # last-good data, for degrading instead of clobbering on a bad run.
    # The Sunday 18:00 reopen proved why: Yahoo served zero bars, the old
    # handler wrote {"error": ...} over Friday's numbers, and chat lost
    # every level mid-session. A fetch failure must never destroy data.
    old_symbols = {}
    if OUT.exists():
        try:
            old_symbols = json.loads(
                OUT.read_text(encoding="utf-8")).get("symbols", {})
        except (json.JSONDecodeError, OSError):
            pass

    now_et = now.astimezone(ET)
    for sym, cfg in SYMBOLS.items():
        prev = old_symbols.get(sym)
        if prev is not None and "last" not in prev:
            prev = None  # an error placeholder is not data worth keeping
        try:
            entry = compute(sym, cfg, prev)
        except Exception as e:
            if prev is not None:
                # keep the numbers, mark them stale, and re-age the bar
                # stamp so nothing claims Friday's print is fresh
                entry = {k: v for k, v in prev.items()
                         if k not in ("stale", "stale_reason")}
                entry["stale"] = True
                entry["stale_reason"] = str(e)
                try:
                    t = datetime.fromisoformat(entry["last"]["time_utc"])
                    entry["last"]["bar_age_min"] = round(
                        (now - t).total_seconds() / 60, 1)
                except (KeyError, ValueError, TypeError):
                    pass
                print(f"{sym}: fetch failed ({e}); keeping previous data")
            else:
                entry = {"error": str(e)}
        out["symbols"][sym] = entry
        out["symbols"][sym]["next_events"] = upcoming(sym, now_et)
        time.sleep(1)

    # skip the rewrite when nothing but the clock would change (markets
    # closed) so the workflow doesn't commit no-op updates
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
            if stable(old.get("symbols")) == stable(out["symbols"]):
                print("levels unchanged; not rewriting")
                return
        except (json.JSONDecodeError, OSError):
            pass
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"levels.json written: "
          f"{[s for s in out['symbols']]} at {out['updated_et']}")


if __name__ == "__main__":
    main()
