#!/usr/bin/env python3
"""
Scheduled-event calendar for ES / CL / GC, run by GitHub Actions.

"Is this good support?" and "momentum or mean reversion?" have different
answers five minutes before EIA inventories than they do at 2pm, and the
levels sheet had no idea what time it was relative to what's scheduled.
This fills that in.

Sources are the issuing agencies themselves rather than an aggregator,
because the aggregators only see ~5 weeks forward and none of them apply
EIA's holiday shifts correctly. Everything here is free and keyless.

Note the User-Agent: BLS returns 403 to browser-impersonating bots but
200 to a request that identifies itself with a contact address.
"""

import json
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UA = "TradingTools/1.0 (aac5389@gmail.com)"
OUT = Path(__file__).parent / "events.json"
HORIZON_DAYS = 45
REFRESH_HOURS = 12

EIA_SCHEDULE = "https://www.eia.gov/petroleum/supply/weekly/schedule.php"
BLS_ICS = "https://www.bls.gov/schedule/news_release/bls.ics"
BEA_JSON = "https://apps.bea.gov/API/signup/release_dates.json"

# which instruments each event actually moves
MOVES = {
    "EIA Weekly Petroleum Status Report": ["CL"],
    "API Crude Inventories": ["CL"],
    "Consumer Price Index": ["ES", "GC"],
    "Producer Price Index": ["ES", "GC"],
    "Employment Situation": ["ES", "GC"],
    "Personal Income and Outlays": ["ES", "GC"],
    "Gross Domestic Product": ["ES", "GC"],
}
# BLS publishes ~20 series; these are the ones that move a chart
BLS_WANTED = {"Consumer Price Index", "Producer Price Index",
              "Employment Situation"}
BEA_WANTED = {"Personal Income and Outlays", "Gross Domestic Product"}


def get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).replace("&nbsp;", " ").strip()


def parse_time(s: str) -> tuple[int, int]:
    """'12:00 p.m.' / '10:30 a.m.' -> (hour, minute) in 24h."""
    m = re.match(r"(\d+):(\d+)\s*([ap])", s.strip().lower())
    if not m:
        raise ValueError(f"unparseable time {s!r}")
    h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    if ap == "p" and h != 12:
        h += 12
    if ap == "a" and h == 12:
        h = 0
    return h, mi


def eia_petroleum(now: datetime) -> list[dict]:
    """Weekly crude inventories: Wednesdays 10:30 ET, except when it isn't.

    EIA's page is an EXCEPTIONS table — it lists only holiday-shifted
    releases, so the normal Wednesday schedule has to be generated and
    then overridden. Both the day and the TIME move (shifted releases go
    to 12:00pm, and Christmas 2025 went to a Monday at 5:00pm), which is
    why this can't be simplified to "Wednesday, or Thursday on holiday
    weeks".

    The table keys on the data week-ending Friday; that week's normal
    release is Friday + 5 days. Verified: week ending 2026-01-16 (a Fri)
    maps to Wed 2026-01-21, which the table shifts to Thu 2026-01-22.
    """
    html = get(EIA_SCHEDULE)
    overrides: dict[str, dict] = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [strip_tags(c) for c in
                 re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
        if len(cells) < 5 or "week ending" in cells[0].lower():
            continue
        try:
            week_end = datetime.strptime(cells[0], "%B %d, %Y")
            alt = datetime.strptime(cells[1], "%B %d, %Y")
            h, mi = parse_time(cells[3])
        except ValueError:
            continue  # a stray row shouldn't kill the whole calendar
        normal = (week_end + timedelta(days=5)).date().isoformat()
        overrides[normal] = {
            "when": datetime(alt.year, alt.month, alt.day, h, mi, tzinfo=ET),
            "note": f"shifted for {cells[4]}"}

    out = []
    d = now.date()
    for i in range(HORIZON_DAYS):
        day = d + timedelta(days=i)
        if day.weekday() != 2:  # Wednesday
            continue
        ov = overrides.get(day.isoformat())
        when = ov["when"] if ov else datetime(
            day.year, day.month, day.day, 10, 30, tzinfo=ET)
        if when < now:
            continue
        out.append({"name": "EIA Weekly Petroleum Status Report",
                    "when": when, "source": "EIA",
                    "note": ov["note"] if ov else ""})
    return out


def bls(now: datetime) -> list[dict]:
    """BLS iCalendar — CPI, PPI, Employment Situation at 08:30 ET."""
    text = get(BLS_ICS)
    out = []
    for block in text.split("BEGIN:VEVENT")[1:]:
        sm = re.search(r"SUMMARY:(.*)", block)
        dm = re.search(r"DTSTART;TZID=[^:]+:(\d{8}T\d{6})", block)
        if not (sm and dm):
            continue
        name = sm.group(1).strip()
        base = next((w for w in BLS_WANTED if name.startswith(w)), None)
        if not base:
            continue
        when = datetime.strptime(dm.group(1), "%Y%m%dT%H%M%S").replace(
            tzinfo=ET)
        if now <= when <= now + timedelta(days=HORIZON_DAYS):
            out.append({"name": base, "when": when, "source": "BLS",
                        "note": ""})
    return out


def bea(now: datetime) -> list[dict]:
    """BEA release dates — PCE lives under 'Personal Income and Outlays'.

    Timestamps are UTC and correctly DST-aware (12:30Z in summer,
    13:30Z in winter — both are 08:30 ET).
    """
    data = json.loads(get(BEA_JSON))
    out = []
    for series in BEA_WANTED:
        # exact key, not startswith — BEA also ships "Gross Domestic
        # Product by State and Personal Income by State", a different
        # release on a different day that a prefix match would swallow
        for raw in data.get(series, {}).get("release_dates", []):
            try:
                when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            when = when.astimezone(ET)
            if now <= when <= now + timedelta(days=HORIZON_DAYS):
                out.append({"name": series, "when": when, "source": "BEA",
                            "note": ""})
    return out


def api_inventories(now: datetime) -> list[dict]:
    """API crude, Tuesdays ~16:30 ET. A rule, not a feed.

    Flagged approximate on purpose: API publishes no machine-readable
    schedule, and this does not model the holiday shift to Wednesday.
    """
    out = []
    for i in range(HORIZON_DAYS):
        day = now.date() + timedelta(days=i)
        if day.weekday() != 1:
            continue
        when = datetime(day.year, day.month, day.day, 16, 30, tzinfo=ET)
        if when >= now:
            out.append({"name": "API Crude Inventories", "when": when,
                        "source": "rule", "note": "approximate; not "
                        "holiday-adjusted"})
    return out


def is_fresh(now: datetime) -> bool:
    """True if events.json was rebuilt recently enough to reuse.

    The levels workflow runs every 10 min, but these schedules change a
    few times a year — polling four government sites 144x/day would be
    rude and invites blocking. Refresh once a day.
    """
    if not OUT.exists():
        return False
    try:
        prev = json.loads(OUT.read_text(encoding="utf-8"))
        stamp = datetime.strptime(
            prev["updated_et"], "%Y-%m-%d %H:%M ET").replace(tzinfo=ET)
    except (json.JSONDecodeError, OSError, KeyError, ValueError):
        return False
    # rebuild anyway if the last run lost a source, so a transient
    # outage doesn't get frozen in for a full day
    if prev.get("source_errors"):
        return False
    return (now - stamp) < timedelta(hours=REFRESH_HOURS)


def main() -> None:
    now = datetime.now(ET)
    if is_fresh(now):
        print("events.json is fresh; skipping refresh")
        return
    events, errors = [], {}
    for label, fn in [("eia", eia_petroleum), ("bls", bls), ("bea", bea),
                      ("api", api_inventories)]:
        try:
            events.extend(fn(now))
        except Exception as e:
            # a dead source must degrade the calendar, not empty it
            errors[label] = str(e)

    events.sort(key=lambda e: e["when"])
    out = {
        "updated_et": now.strftime("%Y-%m-%d %H:%M ET"),
        "horizon_days": HORIZON_DAYS,
        "note": ("Scheduled releases that move ES/CL/GC. All times ET. "
                 "minutes_until is relative to updated_et - recompute it "
                 "from the current time, do not trust it as live."),
        "events": [{
            "name": e["name"],
            "when_et": e["when"].strftime("%Y-%m-%d %H:%M ET"),
            "minutes_until": int((e["when"] - now).total_seconds() // 60),
            "moves": MOVES.get(e["name"], []),
            "source": e["source"],
            **({"note": e["note"]} if e["note"] else {}),
        } for e in events],
    }
    if errors:
        out["source_errors"] = errors

    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"events.json: {len(events)} events, "
          f"{len(errors)} source errors {errors or ''}")


if __name__ == "__main__":
    main()
