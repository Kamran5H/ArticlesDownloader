# Articles Downloader v10 Ultra Pro

[![Tests](https://img.shields.io/badge/Tests-8%2F8%20Passing-brightgreen?style=for-the-badge)](test_articles_v10.py)
[![Sources](https://img.shields.io/badge/Sources-9%20APIs%20Parallel-00F2FE?style=for-the-badge)](Articles_v2.py)
[![UI](https://img.shields.io/badge/UI-Obsidian%204K%20Modern%20%2B%20Tkinter-8B5CF6?style=for-the-badge)](Articles_v2.py)
[![Export](https://img.shields.io/badge/Exports-BibTeX%20|%20RIS%20|%20APA7-EC4899?style=for-the-badge)](Articles_v2.py)

A state-of-the-art scholarly research suite that discovers, ranks, and downloads peer-reviewed academic papers across **9 parallel academic APIs** from a single interface, featuring native PDF resolution, Scimago journal ranking, and an executive **Obsidian 4K Web App** alongside a classic Tkinter desktop interface.

---

## 🚀 Key Capabilities

- **9 Academic APIs Queried in Parallel**: Crossref, arXiv, Semantic Scholar, OpenAlex, PubMed, Europe PMC, CORE, BioRxiv, and IEEE.
- **Cascading Resolver Pipeline**: Automated DOI fallback, Unpaywall open-access links, and direct PDF discovery.
- **Obsidian 4K Modern Interface**: Zero-config Flask + Server-Sent Events (SSE) live streaming progress dashboard running as a native Windows application window.
- **Classic Tkinter Desktop Fallback**: Run anytime with `--tk` for instantaneous native lightweight operation.
- **SCImago SJR Ranking**: Integrated journal quality index (`scimago_ranks.csv`) showing Quartile (Q1–Q4) and prestige metrics.
- **One-Click Citation Export**: Automatic synthesis and download of `references.bib`, `references.ris`, and formatted `references_APA.txt`.
- **Smart SQLite History**: Complete deduplication engine so you never download or query duplicate papers.

---

## 💻 Quick Start

### Launch the Obsidian 4K Desktop App (Default)
```bash
python Articles_v2.py
```
*Automatically detects Microsoft Edge / Google Chrome app window mode with SSE progress streaming on `http://127.0.0.1:5080`.*

### Launch Classic Tkinter GUI
```bash
python Articles_v2.py --tk
```

### Headless CLI Search & Download
```bash
python Articles_v2.py --cli --keywords "zinc-air battery catalyst" --limit 15 --year-min 2022
```

---

## 🧪 Verification & Testing

Articles Downloader is verified by an extensive automated test suite:
```bash
python -m pytest test_articles_v10.py
```
*All 8 core test suites passed with 100% assertions.*
