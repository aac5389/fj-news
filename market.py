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
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SYMBOLS = {
    "ES": {"yahoo": "ES=F", "name": "E-mini S&P 500", "bin": 1.0, "nd": 2},
    "CL": {"yahoo": "CL=F", "name": "WTI Crude Oil", "bin": 0.05, "nd": 2},
    "GC": {"yahoo": "GC=F", "name": "Gold", "bin": 0.5, "nd": 1},
}
ET = ZoneInfo("America/New_York")
UA = "fj-market/1.0 (personal levels sheet)"
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


def et_date(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, ET).strftime("%Y-%m-%d")


def et_str(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, ET).strftime("%Y-%m-%d %H:%M ET")


def compute(sym: str, cfg: dict) -> dict:
    nd = cfg["nd"]
    m1 = fetch_chart(cfg["yahoo"], "1m", "1d")
    time.sleep(1)  # be polite to Yahoo
    d1 = fetch_chart(cfg["yahoo"], "1d", "3mo")
    if not m1 or not d1:
        raise ValueError(f"{sym}: empty chart data")

    last = m1[-1]
    session_date = et_date(last["t"])

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

    # completed daily bars strictly before the current session
    prior_days = [b for b in d1 if et_date(b["t"]) < session_date]
    if not prior_days:
        raise ValueError(f"{sym}: no prior daily bars")
    prev = prior_days[-1]

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

    return {
        "name": cfg["name"],
        "yahoo_symbol": cfg["yahoo"],
        "last": {"price": round(last["c"], nd),
                 "time_utc": datetime.fromtimestamp(
                     last["t"], timezone.utc).isoformat(),
                 "time_et": et_str(last["t"])},
        "session": {"date_et": session_date,
                    "open": round(m1[0]["o"], nd),
                    "high": round(max(b["h"] for b in m1), nd),
                    "low": round(min(b["l"] for b in m1), nd),
                    "vwap": round(vwap, nd),
                    "volume_poc": round(poc, nd),
                    "volume": vol_total},
        "prior_session": {"date_et": et_date(prev["t"]),
                          "high": round(prev["h"], nd),
                          "low": round(prev["l"], nd),
                          "close": round(prev["c"], nd)},
        "ranges": {"atr14": round(atr14, nd) if atr14 else None,
                   "high_5d": hi5, "low_5d": lo5,
                   "high_20d": hi20, "low_20d": lo20},
    }


def main() -> None:
    now = datetime.now(timezone.utc)
    out = {"updated_utc": now.isoformat(),
           "updated_et": now.astimezone(ET).strftime("%Y-%m-%d %H:%M ET"),
           "note": ("Yahoo futures data, ~10-15 min delayed (CME "
                    "licensing). Session = latest available; on weekends "
                    "that is Friday's."),
           "symbols": {}}
    for sym, cfg in SYMBOLS.items():
        try:
            out["symbols"][sym] = compute(sym, cfg)
        except Exception as e:  # one bad symbol shouldn't kill the sheet
            out["symbols"][sym] = {"error": str(e)}
        time.sleep(1)
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"levels.json written: "
          f"{[s for s in out['symbols']]} at {out['updated_et']}")


if __name__ == "__main__":
    main()
