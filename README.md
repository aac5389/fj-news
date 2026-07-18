# fj-news

Automated archive of FinancialJuice market headlines, collected from the
public RSS feed every 20 minutes by GitHub Actions.

- `latest.json` — newest ~500 headlines, newest first (`{updated, count,
  items:[{guid, ts, headline, ...}]}`); fetch this for current news.
- `archive.jsonl` — full history, one JSON object per line, chronological.
- `collect.py` — the collector; `.github/workflows/collect.yml` — the cron.

Headlines are © FinancialJuice; this is a personal-use archive.
