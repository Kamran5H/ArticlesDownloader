# Articles Downloader

A desktop research tool that finds and downloads **scholarly papers across 9 academic APIs** from a single search box, with a local SQLite history so you never fetch the same paper twice.

## Features

- **9 scholarly sources** queried in parallel (Crossref, arXiv, Semantic Scholar, OpenAlex, and more)
- **DuckDuckGo fallback** search (`search_ddg.py`) for hard-to-find papers
- **SQLite history** — deduplicates and tracks every download
- **Journal ranking** built in via `scimago_ranks.csv` (SCImago SJR)
- **Tkinter GUI** — `Articles_v2.py`

## Run

```bash
pip install -r requirements.txt   # or: python import_libraries.py
python Articles_v2.py
```

## Files

- `Articles_v2.py` — main GUI application
- `search_ddg.py` — DuckDuckGo search fallback
- `scimago_ranks.csv` — journal SJR ranking data
- `process_cv.py`, `ascii_cv.py` — supporting utilities
- `test_articles_v9.py` — tests
