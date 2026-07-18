#!/usr/bin/env python3
"""
Single-shot FinancialJuice RSS collector, run by GitHub Actions cron.

Fetches the public RSS feed, dedupes against archive.jsonl, appends new
headlines, and rewrites latest.json (rolling window, newest first) — the
file claude.ai chat fetches on demand.

No secrets required: the RSS feed is keyless.
"""

import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

FEED_URL = "https://www.financialjuice.com/feed.ashx?xy=rss"
HERE = Path(__file__).parent
ARCHIVE = HERE / "archive.jsonl"
LATEST = HERE / "latest.json"
LATEST_WINDOW = 500
UA = "fj-news-collector/1.0 (personal news archive)"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm(headline: str) -> str:
    return re.sub(r"\W+", "", headline).lower()[:120]


def main() -> int:
    existing = []
    if ARCHIVE.exists():
        existing = [json.loads(l) for l in
                    ARCHIVE.read_text(encoding="utf-8").splitlines() if l]
    seen_guids = {e["guid"] for e in existing[-5000:] if e.get("guid")}
    seen_text = {norm(e["headline"]) for e in existing[-5000:]}

    req = urllib.request.Request(FEED_URL, headers={"User-Agent": UA})
    raw = urllib.request.urlopen(req, timeout=30).read()
    root = ET.fromstring(raw)

    build = root.findtext("channel/lastBuildDate")
    if build:
        age = (datetime.now(timezone.utc)
               - parsedate_to_datetime(build)).total_seconds()
        if age > 1800:
            print(f"WARNING: feed lastBuildDate is {age/3600:.1f}h old "
                  f"(FJ feed stalled)")

    new = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if title.startswith("FinancialJuice: "):
            title = title[len("FinancialJuice: "):]
        if not title:
            continue
        try:
            guid = int(item.findtext("guid") or "")
        except ValueError:
            guid = None
        if guid is not None and guid in seen_guids:
            continue
        key = norm(title)
        if key in seen_text:
            continue
        if guid is not None:
            seen_guids.add(guid)
        seen_text.add(key)
        ts = None
        pub = item.findtext("pubDate")
        if pub:
            try:
                ts = parsedate_to_datetime(pub).astimezone(
                    timezone.utc).isoformat()
            except (TypeError, ValueError):
                pass
        new.append({"guid": guid, "ts": ts or now_iso(),
                    "ingested": now_iso(), "headline": title,
                    "channel": "rss", "source": "financialjuice"})

    # RSS lists newest first; append oldest-first to keep the archive
    # in chronological order
    new.reverse()
    if new:
        with ARCHIVE.open("a", encoding="utf-8") as f:
            for item in new:
                f.write(json.dumps(item) + "\n")

    window = (existing + new)[-LATEST_WINDOW:]
    LATEST.write_text(json.dumps(
        {"updated": now_iso(), "count": len(window),
         "items": list(reversed(window))}, indent=1), encoding="utf-8")

    print(f"added {len(new)} headlines "
          f"(archive total {len(existing) + len(new)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
