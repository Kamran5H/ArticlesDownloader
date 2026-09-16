# 📚 Articles Downloader — Academic Paper Discovery & Retrieval Engine

<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://github.com/Kamran5H/ArticlesDownloader)
[![APIs](https://img.shields.io/badge/Academic%20APIs-9%20Federated%20Sources-8B5CF6?style=for-the-badge)](https://github.com/Kamran5H/ArticlesDownloader)
[![SJR Ranking](https://img.shields.io/badge/Journal%20Prestige-SCImago%20Q1--Q4%20Ranked-10B981?style=for-the-badge)](https://github.com/Kamran5H/ArticlesDownloader)
[![Database](https://img.shields.io/badge/Storage-SQLite3%20History%20Vault-003B57?style=for-the-badge&logo=sqlite&logoColor=white)](https://github.com/Kamran5H/ArticlesDownloader)

**High-throughput scholarly literature search, bibliographic citation formatting, and automated multi-tier PDF retrieval engine across 9 major academic federations.**

[Key Features](#-key-features) • [Federated APIs](#-federated-academic-apis) • [System Architecture](#-system-architecture) • [Quickstart](#-quick-start) • [CLI & GUI](#-usage-guide) • [License](#-license)

</div>

---

## 🌟 Executive Overview

**Articles Downloader** is a research-grade Python engine designed for physical chemists, AI researchers, and academic scholars. It solves the fragmentation of scientific discovery by querying **9 major academic APIs simultaneously**—without requiring expensive API subscriptions or complex credentials—and scoring results using official **SCImago Journal Rank (SJR)** prestige tiers (Q1 through Q4).

Equipped with an automated multi-tier PDF retrieval cascade (Unpaywall → OpenAccess / PMC → Publisher Scraper → Sci-Hub), SQLite search history indexing, and instant BibTeX/RIS export, Articles Downloader turns hours of manual literature searching into seconds.

---

## 🚀 Key Features

- **🌐 9 Federated Academic Search Providers**: Parallel queries across biomedical, physical sciences, computer science, and general scholarly repositories.
- **🏆 SCImago Journal Prestige Badges**: Real-time matching against `scimago_ranks.csv` to flag papers published in top-quartile venues (Q1/Q2/Q3/Q4) and show their SJR score.
- **⚡ Multi-Tier PDF Download Waterfall**:
  1. OpenAccess direct DOI resolution via Unpaywall
  2. PubMed Central (PMC) full-text XML & PDF endpoints
  3. arXiv / bioRxiv / medRxiv raw e-print repositories
  4. Polite direct publisher scrapers with fallback to Sci-Hub mirrors
- **💾 Local SQLite History & FTS**: Never lose a paper. Every query, result, abstract, and download is indexed in a local SQLite database for instant full-text filtering.
- **📝 Automated Citation Synthesis**: Export search results directly into standard **BibTeX**, **RIS**, or formatted APA citations.
- **🖥️ Desktop & Terminal Ergonomics**: DPI-aware Windows GUI launcher (`run_articles.vbs`) and high-density terminal interface with color-coded quartile highlights.

---

## 🔍 Federated Academic APIs

| Provider | Subject Coverage | API Protocol | Auth |
| :--- | :--- | :--- | :--- |
| **arXiv** | Physics, Mathematics, Computer Science, Quant-Bio | arXiv REST API | No Key Needed |
| **PubMed / PMC** | Biomedicine, Life Sciences, Nanomaterials | NCBI E-Utilities | No Key Needed |
| **CrossRef** | Cross-publisher registry (140M+ DOIs) | CrossRef REST API | Polite Pool |
| **Semantic Scholar** | AI, Computer Science, Multidisciplinary | S2 Graph API | No Key Needed |
| **OpenAlex** | Global bibliographic index (250M+ works) | OpenAlex REST | Polite Pool |
| **Europe PMC** | European life sciences & bio preprints | Europe PMC REST | No Key Needed |
| **bioRxiv / medRxiv**| Biological, Chemical & Medical Preprints | CSHL Content API | No Key Needed |
| **DuckDuckGo Scholar**| Unindexed whitepapers & institutional preprints| HTML Parser | No Key Needed |
| **Sci-Hub Engine** | Emergency research retrieval fallback | Multi-mirror scraper | No Key Needed |

---

## 🏗️ System Architecture

```mermaid
flowchart TD
    A[Search Query: Keywords / Author / DOI] --> B[Federated API Dispatcher]
    
    subgraph Federated Query Engine
        B --> C1[arXiv]
        B --> C2[PubMed / PMC]
        B --> C3[CrossRef]
        B --> C4[Semantic Scholar]
        B --> C5[OpenAlex]
        B --> C6[Europe PMC]
        B --> C7[bioRxiv]
    end
    
    Federated Query Engine --> D[Deduplication & Normalization]
    D --> E[SCImago Rank Engine: Match ISSN/Journal]
    E --> F[Display Results: Q1/Q2/Q3/Q4 Badges]
    F --> G{User Action}
    G -->|Export Citations| H[BibTeX / RIS / CSV]
    G -->|Download PDF| I[Waterfall PDF Retriever]
    G -->|Auto-Archive| J[(Local SQLite3 Database)]
```

---

## 📁 Repository Structure

```text
ArticlesDownloader/
├── Articles_v2.py              # Main federated search engine, GUI & CLI interface
├── scimago_ranks.csv           # SCImago Journal Rank database (SJR, Quartiles, ISSNs)
├── search_ddg.py               # DuckDuckGo fallback research crawler
├── process_cv.py               # Curriculum Vitae & publication list parser
├── ascii_cv.py                 # Academic ASCII resume generator
├── run_articles.vbs            # Silent Windows desktop launcher
├── FIX_NETWORK_RUN_AS_ADMIN.bat # Windows DNS/SSL network repair utility
├── articles.ico                # High-res academic application icon
├── .gitignore                  # Python runtime exclusions
└── LICENSE                     # Open-source MIT License
```

---

## ⚡ Quick Start

### 1. Installation
```bash
git clone https://github.com/Kamran5H/ArticlesDownloader.git
cd ArticlesDownloader

# Setup virtual environment
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
source .venv/bin/activate

# Install required dependencies
pip install requests beautifulsoup4 urllib3
```

### 2. Run Interactive Research Console
```bash
# Launch interactive search CLI
python Articles_v2.py

# Query a specific research topic directly
python Articles_v2.py --query "Quantum ESPRESSO density functional theory" --max 20

# Search by DOI and download full PDF
python Articles_v2.py --doi "10.1039/D1TA00000A" --download
```

---

## 📜 License

This project is open-source and released under the [MIT License](LICENSE).  
Copyright (c) 2024-2026 **Kamran Ashraf**.
