"""
Articles_v2.py — Research PDF Downloader  ██ v9 Pro ██
================================================================================
Developed by Kamran Ashraf.

Searches 9 scholarly APIs (no keys required, polite rate-limiting),
filters papers with a chemical-aware title relevance gate, ranks them by
journal quartile (Scimago ISSN + Journal Title dual indexing) and citation count,
downloads the PDFs with stealth session rotation, and writes ready-to-import
citations (.bib, .ris, APA 7th, CSV, JSON corpus metadata).

  PHASE 1 — HARVEST (Relevance-Gated Across 9 Scholarly Engines)
    1. OpenAlex       — 250 M+ works; rich author/journal/citation metadata
    2. Crossref       — 150 M+ works; DOI resolved to PDF via Unpaywall/Sci-Hub
    3. Europe PMC     — Direct full-text PDFs & PubMed Central open-access
    4. PubMed / PMC   — NCBI E-Utilities API (36 M+ biomedical/energy/materials)
    5. Semantic Scholar — AI-indexed open-access research papers
    6. DOAJ           — Directory of Open Access Journals
    7. arXiv          — STEM preprints with direct PDF streaming
    8. CORE.ac.uk     — Open repository harvester
    9. BASE           — 300 M+ OA docs (Bielefeld Academic Search Engine)

  PHASE 2 — DEDUPLICATE & RANK
    Multi-tier deduplication (Normalized DOI + Title Hash + URL)
    → Dual-index Scimago Journal Ranking (ISSN + Journal Title fallback)
    → Configurable Quartile Filtering (Q1+Q2, Q1–Q4, or All)
    → Sort by Quartile/Citations, Citation Count, or Publication Year

  PHASE 3 — DOWNLOAD (Stealth Session Concurrency, Target-Aware)
    Direct PDF → Repository OA → Unpaywall → Landing Scraper → Sci-Hub mirrors
    Q1/Q2 → "Q1_Q2/",  Q3/Q4 / Preprints → "Q3_Q4/"

  PHASE 4 — CITATIONS & MEMORY
    references.bib (LaTeX-safe, braced) / references.ris / references_APA.txt (APA 7)
    results.csv (UTF-8 BOM Excel-friendly) / corpus_metadata.json
    SQLite history enables fresh vs incremental search per topic

  CLI / AUTOMATION:
    python Articles_v2.py --cli --keywords "zinc air battery" --max 25 --folder ./pdfs

  Requires: requests, urllib3. Optional: curl_cffi (stealth), bs4 (better parsing).
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
import hashlib
import html
import json
import math
import os
from pathlib import Path
import random
import re
import sqlite3
import subprocess
import sys
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import threading
import time
import urllib.parse
import urllib3
import xml.etree.ElementTree as ET

# Suppress insecure request warnings (some mirrors/repositories have self-signed SSL)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import requests
from urllib.parse import urlparse, urljoin, quote

# Optional high-performance / stealth libraries
try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    cffi_requests = None
    HAS_CFFI = False

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

# Tkinter (lazy import / conditional for CLI mode)
try:
    import tkinter as tk
    from tkinter import messagebox, ttk, filedialog
    HAS_TKINTER = True
except ImportError:
    HAS_TKINTER = False

# ══════════════════════════════════════════════════════════════════════════════
#  CONSTANTS & CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

MAX_ARTICLES    = 500
MAX_WORKERS     = 12          # Balanced concurrency: gentle on scholarly APIs
REQUEST_TIMEOUT = 60          # Generous timeout for large PDFs and slow CDNs
MAX_REQUESTS_PER_ARTICLE = 8  # Candidate link trials per article
MAX_BACKOFF_S   = 30          # Maximum back-off sleep time

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

PROXIES: list[str] = []

SCRIPT_DIR    = Path(__file__).resolve().parent
SCIMAGO_CACHE = SCRIPT_DIR / "scimago_ranks.csv"
HISTORY_DB    = SCRIPT_DIR / "research_history.db"
SCIMAGO_URL   = "https://www.scimagojr.com/journalrank.php?out=xls"

# Auto-detect best default save folder (D:\ if present & writable, else ~/Downloads/Research_PDFs)
def get_default_save_folder() -> Path:
    d_drive = Path("D:/Research_PDFs")
    try:
        if Path("D:/").exists():
            return d_drive
    except Exception:
        pass
    return Path.home() / "Downloads" / "Research_PDFs"

# ══════════════════════════════════════════════════════════════════════════════
#  DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Paper:
    """One scholarly work with rich metadata for reviews and citations."""
    url: str = ""
    title: str = "Untitled"
    doi: str = ""
    authors: list[str] = field(default_factory=list)
    year: str = ""
    journal: str = ""
    issns: list[str] = field(default_factory=list)
    citations: int = 0
    quartile: str = ""      # Q1 / Q2 / Q3 / Q4 / "" (unranked)
    source: str = ""
    pdf_path: str = ""
    keyword: str = ""       # Extracted keyword group
    abstract: str = ""
    pmid: str = ""
    pmcid: str = ""
    candidate_urls: list[str] = field(default_factory=list)

    def clean_doi(self) -> str:
        if not self.doi:
            return ""
        return _extract_doi(self.doi) or self.doi.strip()

    def title_hash(self) -> str:
        norm = re.sub(r"\W+", " ", clean_title(self.title).lower()).strip()
        return hashlib.md5(norm.encode("utf-8")).hexdigest()[:16]

# ══════════════════════════════════════════════════════════════════════════════
#  SHARED THREAD STATE & CONTEXT
# ══════════════════════════════════════════════════════════════════════════════

lock_seen  = threading.Lock()
seen_urls: set[str] = set()
seen_dois: set[str] = set()
seen_titles: set[str] = set()

log_widget = None
cancellation_event = threading.Event()
log_file_path: Path | None = None

active_run_id = 0
lock_run_id = threading.Lock()

class DownloadContext:
    def __init__(self, run_id: int, target_downloads: int, save_folder: Path):
        self.run_id = run_id
        self.target_downloads = target_downloads
        self.save_folder = save_folder
        self.cancellation_event = threading.Event()
        self.successful_downloads = 0
        self.failed_downloads = 0
        self.total_bytes = 0
        self.lock = threading.Lock()
        self.kw_targets: dict[str, int] = {}
        self.kw_done:    dict[str, int] = {}

    def _at_capacity(self, keyword: str | None) -> bool:
        if self.successful_downloads >= self.target_downloads:
            return True
        if keyword is not None and self.kw_targets:
            if self.kw_done.get(keyword, 0) >= self.kw_targets.get(keyword, 0):
                return True
        return False

def _log(msg: str, run_id: int | None = None):
    if run_id is not None:
        with lock_run_id:
            if run_id != active_run_id:
                return
    try:
        print(msg)
    except UnicodeEncodeError:
        try:
            print(msg.encode("ascii", errors="replace").decode("ascii"))
        except Exception:
            pass

    if log_file_path:
        try:
            with open(log_file_path, "a", encoding="utf-8") as lf:
                lf.write(msg + "\n")
        except Exception:
            pass

    if log_widget:
        try:
            def _insert():
                try:
                    log_widget.configure(state="normal")
                    tag = None
                    if "✅" in msg or "🏆" in msg:
                        tag = "success"
                    elif "❌" in msg or "🛑" in msg:
                        tag = "error"
                    elif "📡" in msg or "🎯" in msg or "🚀" in msg or "📖" in msg or "🔬" in msg:
                        tag = "info"
                    elif "⏳" in msg or "⚠️" in msg or "⚡" in msg:
                        tag = "warning"
                    
                    if tag:
                        log_widget.insert("end", msg + "\n", tag)
                    else:
                        log_widget.insert("end", msg + "\n")
                    log_widget.see("end")
                    log_widget.configure(state="disabled")
                except Exception:
                    pass
            log_widget.after(0, _insert)
        except Exception:
            pass

def sleep_check_cancel(seconds: float, ctx: DownloadContext | None) -> bool:
    if not ctx:
        time.sleep(seconds)
        return False
    deadline = time.monotonic() + seconds
    while True:
        if ctx.cancellation_event.is_set():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.1, remaining))

def is_cancelled(ctx: DownloadContext | None = None) -> bool:
    if ctx is not None:
        return ctx.cancellation_event.is_set()
    return cancellation_event.is_set()

# ══════════════════════════════════════════════════════════════════════════════
#  CLEANING, PARSING & RELEVANCE
# ══════════════════════════════════════════════════════════════════════════════

def clean_title(raw_title: str) -> str:
    """Unescape HTML entities, strip XML/HTML tags, and collapse spaces."""
    if not raw_title:
        return "Untitled"
    # 1. Strip XML/HTML markup tags like <i>, <sub>, <sup>, <italic>, <bold>, etc.
    t = re.sub(r'</?[a-zA-Z][a-zA-Z0-9:\-_]*[^>]*>', '', raw_title)
    # 2. Unescape HTML entities (&amp;, &quot;, &lt;, &gt;, &ndash;, etc.)
    t = html.unescape(html.unescape(t))
    # 3. Collapse whitespace
    t = re.sub(r"\s+", " ", t).strip()
    return t or "Untitled"

def _parse_author_name(name: str) -> tuple[str, str]:
    """Parse author name into (Last_Name, Initials_Or_First).
    Correctly handles 'Last, First Middle', 'First Middle Last', and 'Last, F. M.'."""
    name = (name or "").strip().rstrip(".,;")
    if not name:
        return ("", "")
    if "," in name:
        parts = name.split(",", 1)
        last = parts[0].strip()
        first_rest = parts[1].strip()
        first_tokens = [w for w in re.split(r"[\s\.\-]+", first_rest) if w]
        initials = " ".join(f"{w[0].upper()}." for w in first_tokens)
        return (last, initials)
    else:
        tokens = [w for w in re.split(r"\s+", name) if w]
        if len(tokens) == 1:
            return (tokens[0], "")
        last = tokens[-1]
        initials = " ".join(f"{w[0].upper()}." for w in tokens[:-1])
        return (last, initials)

def _authors_apa(authors: list[str]) -> str:
    """Format authors list strictly according to APA 7th edition rules."""
    out: list[str] = []
    for a in authors:
        last, initials = _parse_author_name(a)
        if not last:
            continue
        if initials:
            out.append(f"{last}, {initials}")
        else:
            out.append(last)
    if not out:
        return ""
    if len(out) == 1:
        return out[0]
    if len(out) <= 20:
        return ", ".join(out[:-1]) + ", & " + out[-1]
    # APA 7 rule for > 20 authors: First 19, ellipsis, last author
    return ", ".join(out[:19]) + ", ... " + out[-1]

def _escape_bibtex(s: str) -> str:
    if not s:
        return ""
    for char in ["\\", "{", "}", "%", "$", "&", "_", "#"]:
        s = s.replace(char, f"\\{char}")
    return s

def _bib_key(p: Paper, used: set[str]) -> str:
    last = "anon"
    if p.authors:
        parsed_last, _ = _parse_author_name(p.authors[0])
        last = parsed_last or "anon"
    clean_last = re.sub(r"\W", "", last).lower() or "anon"
    key = f"{clean_last}{p.year or 'nd'}"
    base, n = key, 1
    while key in used:
        key = f"{base}{chr(96 + n)}"
        n += 1
    used.add(key)
    return key

# Suffix stemmer & token relevance guard
RELEVANCE_STOPWORDS = {
    "a","an","the","and","or","of","in","for","to","with","on","at",
    "by","from","as","is","are","was","were","be","been","being",
    "this","that","these","those","its","it","we","our","their",
    "using","used","use","via","based","new","novel","study","studies",
    "recent","advances","review","research","paper","towards","toward",
    "journal","article","author","authors","vol","volume","issue",
}

# Chemical symbols & significant short scientific tokens allowed
ALLOWED_SHORT_TOKENS = {
    "li", "zn", "na", "mg", "ca", "k", "al", "fe", "co", "ni", "cu", "mn", "v",
    "mo", "w", "pt", "pd", "au", "ag", "ru", "ir", "ti", "sn", "pb", "bi", "sb",
    "si", "ge", "b", "c", "n", "p", "s", "o", "f", "cl", "br", "i", "se", "te",
    "h2", "o2", "co2", "n2", "nh3", "ch4", "2d", "3d", "1d", "0d", "ai", "ml", "dft",
}

CHEMICAL_SYNONYMS = {
    "zn": "zinc", "zinc": "zinc",
    "li": "lithium", "lithium": "lithium",
    "na": "sodium", "sodium": "sodium",
    "mg": "magnesium", "magnesium": "magnesium",
    "k": "potassium", "potassium": "potassium",
    "co": "cobalt", "cobalt": "cobalt",
    "fe": "iron", "iron": "iron",
    "ni": "nickel", "nickel": "nickel",
    "mn": "manganese", "manganese": "manganese",
    "al": "aluminum", "aluminium": "aluminum", "aluminum": "aluminum",
}

def _stem(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("ses", "xes", "zes", "ches", "shes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w

def _topic_tokens(s: str) -> set[str]:
    raw = [w.lower() for w in re.split(r"\W+", clean_title(s) or "") if w]
    tokens = set()
    for w in raw:
        if w in RELEVANCE_STOPWORDS:
            continue
        canon = CHEMICAL_SYNONYMS.get(w, w)
        if len(canon) >= 2 or canon in ALLOWED_SHORT_TOKENS:
            tokens.add(_stem(canon))
    return tokens

def _title_is_relevant(title: str, keywords: str, min_ratio: float = 0.35) -> bool:
    kw_tokens = _topic_tokens(keywords)
    if not kw_tokens:
        return True
    c_title = clean_title(title)
    flat_title = re.sub(r"\W+", " ", c_title.lower()).strip()
    flat_kw    = re.sub(r"\W+", " ", (keywords or "").lower()).strip()
    if flat_kw and flat_kw in flat_title:
        return True
    title_tokens = _topic_tokens(c_title)
    matches = len(kw_tokens & title_tokens)
    need = max(1, math.ceil(len(kw_tokens) * min_ratio))
    return matches >= need

KEYWORD_NOISE = {
    "comprehensive", "efficient", "efficiency", "high", "highly", "low",
    "performance", "enhanced", "improved", "improving", "effective", "facile",
    "direct", "rapid", "simple", "advanced", "modern", "general", "overview",
    "insight", "insights", "progress", "perspective", "perspectives", "role",
    "effect", "effects", "application", "applications", "approach", "approaches",
    "method", "methods", "analysis", "properties", "property", "characterization",
    "development", "design", "strategy", "strategies", "understanding",
    "future", "challenge", "challenges", "prospect", "prospects", "outlook",
    "trend", "trends", "opportunity", "opportunities", "current", "potential",
}

def extract_keywords(text: str, max_keywords: int = 6) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]*", text)
    content = [w for w in words if w.lower() not in RELEVANCE_STOPWORDS]
    if len(words) <= 5 or len(content) <= 2:
        return [text]

    marked = re.sub(r"[^A-Za-z0-9\-\s]+", " \x00 ", text)
    phrases: list[list[str]] = []
    current: list[str] = []
    for tok in marked.split():
        if tok == "\x00" or tok.lower() in RELEVANCE_STOPWORDS:
            if current:
                phrases.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        phrases.append(current)

    def is_useful(ws: list[str]) -> bool:
        if not ws:
            return False
        if all(w.lower() in KEYWORD_NOISE for w in ws):
            return False
        if len(ws) == 1:
            w = ws[0]
            if w.lower() in KEYWORD_NOISE:
                return False
            return len(w) >= 3 or w.lower() in ALLOWED_SHORT_TOKENS or any(c.isdigit() for c in w)
        return True

    seen: set[str] = set()
    ranked: list[tuple[int, int, str]] = []
    for ws in phrases:
        if not is_useful(ws):
            continue
        phrase = " ".join(ws)
        key = phrase.lower()
        if key in seen:
            continue
        seen.add(key)
        ranked.append((len(ws), len(phrase), phrase))

    ranked.sort(key=lambda t: (t[0], t[1]), reverse=True)
    keywords = [p for _, _, p in ranked[:max_keywords]]
    return keywords or [text]

# ══════════════════════════════════════════════════════════════════════════════
#  NETWORKING & STEALTH SESSIONS
# ══════════════════════════════════════════════════════════════════════════════

def rand_ua() -> str:
    return random.choice(USER_AGENTS)

def api_headers() -> dict[str, str]:
    return {
        "User-Agent":      rand_ua(),
        "Accept":          "application/json, text/html, application/xml, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection":      "keep-alive",
    }

def rand_proxy() -> dict[str, str] | None:
    if not PROXIES:
        return None
    p = random.choice(PROXIES)
    return {"http": p, "https": p}

def jitter(lo: float = 0.2, hi: float = 0.6):
    time.sleep(random.uniform(lo, hi))

def normalise_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")

def _is_real_http_url(url: str) -> bool:
    return bool(url) and url.startswith(("http://", "https://"))

def _extract_doi(text: str) -> str:
    m = re.search(r'10\.\d{4,9}/[-._;()/:A-Za-z0-9]+', text)
    if m:
        doi = m.group(0).rstrip('.,;)]}>"')
        return doi
    return ""

def open_stealth_session(profile: str = "chrome120"):
    if HAS_CFFI:
        return cffi_requests.Session(impersonate=profile)
    s = requests.Session()
    s.headers.update(api_headers())
    return s

def safe_get(url: str, ctx: DownloadContext | None = None, **kwargs) -> requests.Response | None:
    for attempt in range(1, 4):
        if is_cancelled(ctx):
            return None
        try:
            r = requests.get(url, headers=api_headers(),
                             timeout=REQUEST_TIMEOUT,
                             proxies=rand_proxy(), **kwargs)
            if r.status_code == 429:
                wait = min(2 ** attempt + random.uniform(0.5, 1.5), MAX_BACKOFF_S)
                _log(f"    ⏳ Rate limited (429). Waiting {wait:.1f}s...")
                if sleep_check_cancel(wait, ctx):
                    return None
                continue
            return r
        except requests.exceptions.Timeout:
            _log(f"    ⏱ Timeout (attempt {attempt}/3): {url[:60]}")
            if sleep_check_cancel(min(2 ** attempt, MAX_BACKOFF_S), ctx):
                return None
        except Exception as e:
            _log(f"    ⚠️  Request error: {e}")
            break
    return None

# ══════════════════════════════════════════════════════════════════════════════
#  SCIMAGO JOURNAL QUARTILE RANKING (ISSN + TITLE DUAL INDEX)
# ══════════════════════════════════════════════════════════════════════════════

_scimago_issn_map: dict[str, str] | None = None
_scimago_title_map: dict[str, str] | None = None
_scimago_lock = threading.Lock()

def _norm_issn(s: str) -> str:
    return re.sub(r"[^0-9Xx]", "", s or "").upper().zfill(8)

def _norm_journal_title(s: str) -> str:
    return re.sub(r"\W+", " ", (s or "").lower()).strip()

def load_scimago_quartiles() -> tuple[dict[str, str], dict[str, str]]:
    """Load Scimago quartiles indexed by both ISSN and Journal Title."""
    global _scimago_issn_map, _scimago_title_map
    with _scimago_lock:
        if _scimago_issn_map is not None and _scimago_title_map is not None:
            return _scimago_issn_map, _scimago_title_map

        issn_map: dict[str, str] = {}
        title_map: dict[str, str] = {}

        if not SCIMAGO_CACHE.exists():
            _log("  🗂  Downloading Scimago journal rankings (one-time)…")
            data = None
            try:
                with open_stealth_session("chrome120") as s:
                    r = s.get(SCIMAGO_URL, timeout=120)
                    if r.status_code == 200 and b"Quartile" in r.content[:600]:
                        data = r.content
            except Exception:
                pass
            if data is None:
                try:
                    r = requests.get(SCIMAGO_URL, headers=api_headers(), timeout=120)
                    if r.status_code == 200 and b"Quartile" in r.content[:600]:
                        data = r.content
                except Exception:
                    pass
            if data:
                SCIMAGO_CACHE.write_bytes(data)

        if SCIMAGO_CACHE.exists():
            try:
                text = SCIMAGO_CACHE.read_text(encoding="utf-8", errors="ignore")
                delimiter = ";" if ";" in text[:500] else ","
                for row in csv.DictReader(text.splitlines(), delimiter=delimiter):
                    q = (row.get("SJR Best Quartile") or row.get("SJR Quartile") or "").strip()
                    if q not in ("Q1", "Q2", "Q3", "Q4"):
                        continue
                    # 1. Index ISSNs
                    for iss in (row.get("Issn") or "").split(","):
                        ni = _norm_issn(iss)
                        if len(ni) == 8 and (ni not in issn_map or q < issn_map[ni]):
                            issn_map[ni] = q
                    # 2. Index Journal Title
                    jtitle = _norm_journal_title(row.get("Title") or "")
                    if jtitle and (jtitle not in title_map or q < title_map[jtitle]):
                        title_map[jtitle] = q
            except Exception as e:
                _log(f"  ⚠️  Scimago parse error: {e}")

        _scimago_issn_map = issn_map
        _scimago_title_map = title_map
        if issn_map or title_map:
            _log(f"  🗂  Scimago loaded: {len(issn_map):,} ISSNs and {len(title_map):,} Journal Titles")
        return _scimago_issn_map, _scimago_title_map

def quartile_for(issns: list[str], journal: str = "") -> str:
    issn_map, title_map = _scimago_issn_map or {}, _scimago_title_map or {}
    # Primary: check ISSN
    for iss in issns:
        ni = _norm_issn(iss)
        if ni in issn_map:
            return issn_map[ni]
    # Fallback: check normalized journal title
    if journal:
        nj = _norm_journal_title(journal)
        if nj in title_map:
            return title_map[nj]
    return ""

# ══════════════════════════════════════════════════════════════════════════════
#  9 SCHOLARLY HARVESTERS
# ══════════════════════════════════════════════════════════════════════════════

def add_paper_candidate(papers: list[Paper], p: Paper) -> bool:
    """Thread-safe multi-tier deduplication by DOI, Title Hash, and URL."""
    p.title = clean_title(p.title)
    if not p.title or p.title == "Untitled":
        return False
    doi = p.clean_doi()
    thash = p.title_hash()
    norm_u = normalise_url(p.url) if p.url else ""

    with lock_seen:
        if doi and doi.lower() in seen_dois:
            return False
        if thash in seen_titles:
            return False
        if norm_u and norm_u in seen_urls:
            return False

        if doi:
            seen_dois.add(doi.lower())
        seen_titles.add(thash)
        if norm_u:
            seen_urls.add(norm_u)
        papers.append(p)
        return True

# ── 1. OpenAlex ───────────────────────────────────────────────────────────────
def harvest_openalex(keywords: str, y1: str, y2: str, max_res: int = 250, ctx=None) -> list[Paper]:
    found: list[Paper] = []
    _log("  📖 OpenAlex: searching works…")
    base = "https://api.openalex.org/works"
    cursor = "*"
    per_page = min(200, max_res)
    topic_q = re.sub(r"[,|:]+", " ", keywords).strip()

    while len(found) < max_res and not is_cancelled(ctx):
        params = {
            "filter": f"title_and_abstract.search:{topic_q},from_publication_date:{y1}-01-01,to_publication_date:{y2}-12-31",
            "per-page": per_page,
            "cursor": cursor,
            "select": "id,doi,title,authorships,publication_year,cited_by_count,primary_location,best_oa_location,open_access",
            "mailto": "chkam.dev@gmail.com",
        }
        r = safe_get(base, ctx=ctx, params=params)
        if r is None or r.status_code != 200:
            break
        data = r.json()
        results = data.get("results", [])
        if not results:
            break

        for item in results:
            title = clean_title(item.get("title") or "")
            if not _title_is_relevant(title, keywords):
                continue
            doi = _extract_doi(item.get("doi") or "")
            best = item.get("best_oa_location") or {}
            pl = item.get("primary_location") or {}
            oa = item.get("open_access") or {}
            url = (best.get("pdf_url") or pl.get("pdf_url") or oa.get("oa_url")
                   or best.get("landing_page_url") or pl.get("landing_page_url")
                   or (f"https://doi.org/{doi}" if doi else ""))
            if not _is_real_http_url(url):
                continue

            authors = [a.get("author", {}).get("display_name") for a in (item.get("authorships") or [])[:25]
                       if a.get("author", {}).get("display_name")]
            year = str(item.get("publication_year") or "")
            cits = item.get("cited_by_count") or 0
            src = pl.get("source") or best.get("source") or {}
            journal = src.get("display_name") or ""
            issns = list(src.get("issn") or [])
            if src.get("issn_l"):
                issns = [src["issn_l"]] + issns

            p = Paper(url=url, title=title, doi=doi, authors=authors, year=year,
                      journal=journal, issns=issns, citations=cits, source="OpenAlex")
            add_paper_candidate(found, p)

        cursor = data.get("meta", {}).get("next_cursor")
        if not cursor or len(results) < per_page:
            break
        jitter(0.2, 0.5)

    _log(f"  ✅ OpenAlex harvested {len(found)} candidates")
    return found

# ── 2. Crossref ───────────────────────────────────────────────────────────────
def harvest_crossref(keywords: str, y1: str, y2: str, max_res: int = 250, ctx=None) -> list[Paper]:
    found: list[Paper] = []
    _log("  🔗 Crossref: searching journal literature…")
    base = "https://api.crossref.org/works"
    cursor = "*"
    rows = min(100, max_res)
    year_filter = f"from-pub-date:{y1}-01-01,until-pub-date:{y2}-12-31,type:journal-article"

    while len(found) < max_res and not is_cancelled(ctx):
        params = {
            "query": keywords,
            "filter": year_filter,
            "rows": rows,
            "cursor": cursor,
            "select": "DOI,title,author,container-title,ISSN,published,published-print,published-online,is-referenced-by-count,link,resource",
            "mailto": "chkam.dev@gmail.com",
        }
        r = safe_get(base, ctx=ctx, params=params)
        if r is None or r.status_code != 200:
            break
        msg = r.json().get("message", {})
        items = msg.get("items", [])
        if not items:
            break

        for item in items:
            t_list = item.get("title") or []
            title = clean_title(t_list[0] if t_list else "")
            if not _title_is_relevant(title, keywords):
                continue
            doi = (item.get("DOI") or "").strip()

            authors = []
            for a in item.get("author", []):
                f, g = a.get("family", ""), a.get("given", "")
                if f and g:
                    authors.append(f"{f}, {g}")
                elif f:
                    authors.append(f)

            j_list = item.get("container-title") or []
            journal = (j_list[0] if j_list else "").strip()
            issns = list(item.get("ISSN") or [])
            cits = item.get("is-referenced-by-count") or 0

            pub = item.get("published-print") or item.get("published-online") or item.get("published") or {}
            dparts = pub.get("date-parts") or [[""]]
            year = str(dparts[0][0]) if dparts and dparts[0] else ""

            pdf_url = ""
            for l in item.get("link", []):
                u = l.get("URL", "")
                if l.get("content-type") == "application/pdf" or ".pdf" in u.lower():
                    pdf_url = u
                    break
            url = pdf_url or (f"https://doi.org/{doi}" if doi else "")
            if not _is_real_http_url(url):
                continue

            p = Paper(url=url, title=title, doi=doi, authors=authors, year=year,
                      journal=journal, issns=issns, citations=cits, source="Crossref")
            add_paper_candidate(found, p)

        cursor = msg.get("next-cursor")
        if not cursor or len(items) < rows:
            break
        jitter(0.2, 0.5)

    _log(f"  ✅ Crossref harvested {len(found)} candidates")
    return found

# ── 3. Europe PMC ─────────────────────────────────────────────────────────────
def harvest_europepmc(keywords: str, y1: str, y2: str, max_res: int = 150, ctx=None) -> list[Paper]:
    found: list[Paper] = []
    _log("  🧬 Europe PMC: searching full-text papers…")
    base = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    page_size = min(100, max_res)
    cursor = "*"
    query = f"{keywords} OPEN_ACCESS:Y PUB_YEAR:[{y1} TO {y2}] HAS_FT:Y"

    while len(found) < max_res and not is_cancelled(ctx):
        params = {
            "query": query, "format": "json", "resultType": "core",
            "pageSize": page_size, "cursorMark": cursor,
        }
        r = safe_get(base, ctx=ctx, params=params)
        if r is None or r.status_code != 200:
            break
        results = r.json().get("resultList", {}).get("result", [])
        if not results:
            break

        for item in results:
            title = clean_title(item.get("title") or "")
            if not _title_is_relevant(title, keywords):
                continue
            doi = item.get("doi") or ""
            pmcid = item.get("pmcid") or ""

            pdf_url = ""
            for u in item.get("fullTextUrlList", {}).get("fullTextUrl", []):
                if u.get("documentStyle") == "pdf":
                    pdf_url = u.get("url", "")
                    break
            if not pdf_url and pmcid:
                pdf_url = f"https://europepmc.org/backend/ptpmcrender.fcgi?accid={pmcid}&blobtype=pdf"
            if not pdf_url and doi:
                pdf_url = f"https://doi.org/{doi}"

            if not _is_real_http_url(pdf_url):
                continue

            authors = [a.strip() for a in (item.get("authorString") or "").split(",") if a.strip()]
            journal = item.get("journalTitle") or (item.get("journalInfo") or {}).get("journal", {}).get("title") or ""
            issn = item.get("journalIssn") or ""
            issns = [issn] if issn else []
            year = str(item.get("pubYear") or "")
            cits = item.get("citedByCount") or 0

            p = Paper(url=pdf_url, title=title, doi=doi, authors=authors, year=year,
                      journal=journal, issns=issns, citations=cits, pmcid=pmcid, source="EuropePMC")
            add_paper_candidate(found, p)

        cursor = r.json().get("nextCursorMark", "")
        if not cursor or cursor == "*" or len(results) < page_size:
            break
        jitter(0.2, 0.5)

    _log(f"  ✅ Europe PMC harvested {len(found)} candidates")
    return found

# ── 4. PubMed / NCBI E-Utilities ──────────────────────────────────────────────
def harvest_pubmed(keywords: str, y1: str, y2: str, max_res: int = 150, ctx=None) -> list[Paper]:
    found: list[Paper] = []
    _log("  🏥 PubMed / PMC: searching biomedical & materials repository…")
    base_search = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    base_summary = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

    query = f"{keywords} AND (open access[filter]) AND ({y1}[pdat] : {y2}[pdat])"
    r = safe_get(base_search, ctx=ctx, params={
        "db": "pmc", "term": query, "retmode": "json", "retmax": min(max_res, 100),
    })
    if r is None or r.status_code != 200:
        return found

    id_list = r.json().get("esearchresult", {}).get("idlist", [])
    if not id_list:
        return found

    # Batch summary lookup (50 at a time)
    for i in range(0, len(id_list), 50):
        if is_cancelled(ctx):
            break
        batch = id_list[i:i + 50]
        r_sum = safe_get(base_summary, ctx=ctx, params={
            "db": "pmc", "id": ",".join(batch), "retmode": "json",
        })
        if r_sum is None or r_sum.status_code != 200:
            continue
        res_data = r_sum.json().get("result", {})

        for pid in batch:
            item = res_data.get(pid, {})
            title = clean_title(item.get("title") or "")
            if not _title_is_relevant(title, keywords):
                continue
            doi = _extract_doi(item.get("doi") or "") or item.get("doi") or ""
            authors = [a.get("name") for a in item.get("authors", []) if a.get("name")]
            journal = item.get("source") or item.get("fulljournalname") or ""
            pubdate = item.get("pubdate") or ""
            year = str(pubdate[:4]) if pubdate else ""

            pmcid = f"PMC{pid}"
            pdf_url = f"https://europepmc.org/backend/ptpmcrender.fcgi?accid={pmcid}&blobtype=pdf"

            p = Paper(url=pdf_url, title=title, doi=doi, authors=authors, year=year,
                      journal=journal, pmcid=pmcid, source="PubMed/PMC")
            add_paper_candidate(found, p)
        jitter(0.3, 0.6)

    _log(f"  ✅ PubMed/PMC harvested {len(found)} candidates")
    return found

# ── 5. Semantic Scholar ───────────────────────────────────────────────────────
def harvest_semantic_scholar(keywords: str, y1: str, y2: str, max_res: int = 150, ctx=None) -> list[Paper]:
    found: list[Paper] = []
    _log("  🔬 Semantic Scholar: searching open-access papers…")
    base = "https://api.semanticscholar.org/graph/v1/paper/search"
    limit = min(100, max_res)
    offset = 0

    while len(found) < max_res and not is_cancelled(ctx):
        r = safe_get(base, ctx=ctx, params={
            "query": keywords,
            "fields": "title,openAccessPdf,year,authors,venue,citationCount,externalIds",
            "limit": limit, "offset": offset, "year": f"{y1}-{y2}",
        })
        if r is None or r.status_code != 200:
            break
        items = r.json().get("data", [])
        if not items:
            break

        for paper in items:
            title = clean_title(paper.get("title") or "")
            if not _title_is_relevant(title, keywords):
                continue
            oa = paper.get("openAccessPdf") or {}
            url = oa.get("url") or ""
            ext_ids = paper.get("externalIds") or {}
            doi = ext_ids.get("DOI") or ""
            if not url and doi:
                url = f"https://doi.org/{doi}"
            if not _is_real_http_url(url):
                continue

            authors = [a.get("name") for a in (paper.get("authors") or []) if a.get("name")]
            journal = paper.get("venue") or ""
            year = str(paper.get("year") or "")
            cits = paper.get("citationCount") or 0

            p = Paper(url=url, title=title, doi=doi, authors=authors, year=year,
                      journal=journal, citations=cits, source="SemanticScholar")
            add_paper_candidate(found, p)

        offset += limit
        if len(items) < limit:
            break
        jitter(0.3, 0.7)

    _log(f"  ✅ Semantic Scholar harvested {len(found)} candidates")
    return found

# ── 6. DOAJ ───────────────────────────────────────────────────────────────────
def harvest_doaj(keywords: str, y1: str, y2: str, max_res: int = 100, ctx=None) -> list[Paper]:
    found: list[Paper] = []
    _log("  📚 DOAJ: searching open-access journals…")
    base = "https://doaj.org/api/search/articles"
    page = 1
    size = min(100, max_res)

    while len(found) < max_res and not is_cancelled(ctx):
        r = safe_get(f"{base}/{quote(keywords, safe='')}", ctx=ctx, params={
            "page": page, "pageSize": size, "sort": "created_date:desc",
        })
        if r is None or r.status_code != 200:
            break
        results = r.json().get("results", [])
        if not results:
            break

        for item in results:
            bibjson = item.get("bibjson", {})
            title = clean_title(bibjson.get("title") or "")
            if not _title_is_relevant(title, keywords):
                continue
            year = str(bibjson.get("year") or "")
            if year.isdigit() and not (int(y1) <= int(year) <= int(y2)):
                continue

            pdf_url = ""
            for link in bibjson.get("link", []):
                if link.get("type") == "fulltext":
                    pdf_url = link.get("url", "")
                    break
            doi = ""
            for ident in bibjson.get("identifier", []):
                if ident.get("type") == "doi":
                    doi = ident.get("id", "")
                    break
            url = pdf_url or (f"https://doi.org/{doi}" if doi else "")
            if not _is_real_http_url(url):
                continue

            authors = [a.get("name") for a in bibjson.get("author", []) if a.get("name")]
            journal_obj = bibjson.get("journal", {})
            journal = journal_obj.get("title") or ""
            issns = list(journal_obj.get("issns") or [])

            p = Paper(url=url, title=title, doi=doi, authors=authors, year=year,
                      journal=journal, issns=issns, source="DOAJ")
            add_paper_candidate(found, p)

        if len(results) < size:
            break
        page += 1
        jitter(0.2, 0.5)

    _log(f"  ✅ DOAJ harvested {len(found)} candidates")
    return found

# ── 7. arXiv ──────────────────────────────────────────────────────────────────
def harvest_arxiv(keywords: str, y1: str, y2: str, max_res: int = 150, ctx=None) -> list[Paper]:
    found: list[Paper] = []
    _log("  📄 arXiv: searching STEM preprints…")
    base = "https://export.arxiv.org/api/query"
    batch = min(150, max_res)
    start = 0
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    while len(found) < max_res and not is_cancelled(ctx):
        r = safe_get(base, ctx=ctx, params={
            "search_query": f"all:{keywords}",
            "start": start, "max_results": batch,
            "sortBy": "submittedDate", "sortOrder": "descending",
        })
        if r is None or r.status_code != 200:
            break
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            break

        entries = root.findall("atom:entry", ns)
        if not entries:
            break

        for entry in entries:
            pub_raw = (entry.findtext("atom:published", "", ns) or "")[:4]
            if pub_raw.isdigit() and not (int(y1) <= int(pub_raw) <= int(y2)):
                continue
            title = clean_title(entry.findtext("atom:title", "", ns) or "")
            if not _title_is_relevant(title, keywords):
                continue

            pdf_url = ""
            for link in entry.findall("atom:link", ns):
                if link.get("type") == "application/pdf":
                    pdf_url = link.get("href", "")
                    break
            if not pdf_url:
                id_url = entry.findtext("atom:id", "", ns)
                if id_url:
                    pdf_url = id_url.replace("/abs/", "/pdf/") + ".pdf"

            if not _is_real_http_url(pdf_url):
                continue

            authors = [a.findtext("atom:name", "", ns) for a in entry.findall("atom:author", ns)
                       if a.findtext("atom:name", "", ns)]
            doi = entry.findtext("atom:doi", "", ns) or ""
            abstract = entry.findtext("atom:summary", "", ns) or ""

            p = Paper(url=pdf_url, title=title, doi=doi, authors=authors, year=pub_raw,
                      journal="arXiv preprint", abstract=abstract, source="arXiv")
            add_paper_candidate(found, p)

        if len(entries) < batch:
            break
        start += batch
        jitter(0.8, 1.5)

    _log(f"  ✅ arXiv harvested {len(found)} candidates")
    return found

# ── 8. CORE.ac.uk ─────────────────────────────────────────────────────────────
def harvest_core(keywords: str, y1: str, y2: str, max_res: int = 100, ctx=None) -> list[Paper]:
    found: list[Paper] = []
    _log("  🌐 CORE.ac.uk: searching repository works…")
    base = "https://api.core.ac.uk/v3/search/works"
    size = min(100, max_res)

    r = safe_get(base, ctx=ctx, params={
        "q": f"{keywords} year:[{y1} TO {y2}]",
        "limit": size, "offset": 0, "exclude": "fullText",
    })
    if r is None or r.status_code != 200:
        return found

    results = r.json().get("results", [])
    for item in results:
        title = clean_title(item.get("title") or "")
        if not _title_is_relevant(title, keywords):
            continue
        urls = item.get("sourceFulltextUrls") or []
        url = item.get("downloadUrl") or (urls[0] if urls else None)
        doi = item.get("doi") or ""
        if not url and doi:
            url = f"https://doi.org/{doi}"
        if not _is_real_http_url(url):
            continue

        authors = [a.get("name") for a in item.get("authors", []) if a.get("name")]
        year = str(item.get("yearPublished") or "")
        journal = (item.get("journals") or [{}])[0].get("title") or ""

        p = Paper(url=url, title=title, doi=doi, authors=authors, year=year,
                  journal=journal, source="CORE")
        add_paper_candidate(found, p)

    _log(f"  ✅ CORE harvested {len(found)} candidates")
    return found

# ── 9. BASE ───────────────────────────────────────────────────────────────────
def harvest_base(keywords: str, y1: str, y2: str, max_res: int = 100, ctx=None) -> list[Paper]:
    found: list[Paper] = []
    _log("  🅱️  BASE: searching academic documents…")
    base = "https://api.base-search.net/cgi-bin/BaseHttpSearchInterface.fcgi"
    hits = min(100, max_res)

    r = safe_get(base, ctx=ctx, params={
        "func": "PerformSearch", "query": keywords, "format": "json",
        "hits": hits, "offset": 0,
    })
    if r is None or r.status_code != 200:
        return found

    try:
        docs = (r.json().get("response", {}) or {}).get("docs", [])
    except Exception:
        return found

    for d in docs:
        title = clean_title(d.get("dctitle") or "")
        if not _title_is_relevant(title, keywords):
            continue
        yr = str(d.get("dcyear") or "")
        if yr.isdigit() and not (int(y1) <= int(yr) <= int(y2)):
            continue

        url = ""
        for key in ("dclink", "dcidentifier"):
            v = d.get(key)
            if isinstance(v, list):
                v = next((x for x in v if _is_real_http_url(x)), "")
            if isinstance(v, str):
                v = v.split(";")[0].strip()
            if v and _is_real_http_url(v):
                url = v
                break
        doi = ""
        dv = d.get("dcdoi")
        if isinstance(dv, list):
            dv = dv[0] if dv else ""
        if dv:
            doi = _extract_doi(str(dv)) or str(dv)
        if not url and doi:
            url = f"https://doi.org/{doi}"
        if not _is_real_http_url(url):
            continue

        authors = d.get("dccreator") or []
        if isinstance(authors, str):
            authors = [authors]
        journal = d.get("dcsource") or ""
        if isinstance(journal, list):
            journal = journal[0] if journal else ""

        p = Paper(url=url, title=title, doi=doi, authors=authors, year=yr,
                  journal=journal, source="BASE")
        add_paper_candidate(found, p)

    _log(f"  ✅ BASE harvested {len(found)} candidates")
    return found

# ══════════════════════════════════════════════════════════════════════════════
#  METADATA ENRICHMENT VIA OPENALEX BATCHES
# ══════════════════════════════════════════════════════════════════════════════

def enrich_with_openalex(papers: list[Paper], ctx: DownloadContext | None = None) -> list[Paper]:
    """Batch-fill metadata for papers missing journal, ISSN, or citation counts."""
    to_enrich: list[Paper] = [p for p in papers if p.clean_doi() and (not p.issns or not p.citations)]
    if not to_enrich:
        return papers

    _log(f"  🔎 Enriching metadata for {len(to_enrich)} DOIs via OpenAlex…")
    by_doi = {p.clean_doi().lower(): p for p in to_enrich}
    dois = list(by_doi.keys())

    for i in range(0, len(dois), 50):
        if is_cancelled(ctx):
            break
        batch = dois[i:i + 50]
        r = safe_get("https://api.openalex.org/works", ctx=ctx, params={
            "filter": "doi:" + "|".join(batch),
            "per-page": 50,
            "select": "doi,title,authorships,publication_year,cited_by_count,primary_location",
            "mailto": "chkam.dev@gmail.com",
        })
        if r is None or r.status_code != 200:
            continue

        for item in r.json().get("results", []):
            d = (_extract_doi(item.get("doi") or "") or "").lower()
            p = by_doi.get(d)
            if not p:
                continue
            p.citations = max(p.citations, item.get("cited_by_count") or 0)
            if not p.year:
                p.year = str(item.get("publication_year") or "")
            if not p.authors:
                auths = [(a.get("author") or {}).get("display_name")
                         for a in (item.get("authorships") or [])[:25]]
                p.authors = [a for a in auths if a]
            src = (item.get("primary_location") or {}).get("source") or {}
            if not p.journal:
                p.journal = src.get("display_name") or ""
            issns = list(src.get("issn") or [])
            if src.get("issn_l"):
                issns = [src["issn_l"]] + issns
            if issns:
                p.issns = issns
        jitter(0.2, 0.4)
    return papers

# ══════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD ENGINE (STEALTH, UNPAYWALL, LANDING SCRAPER, SCI-HUB)
# ══════════════════════════════════════════════════════════════════════════════

SCIHUB_MIRRORS = [
    "https://sci-hub.se", "https://sci-hub.st", "https://sci-hub.ru",
    "https://sci-hub.cat", "https://sci-hub.wf", "https://sci-hub.al",
    "https://sci-hub.ee", "https://sci-hub.ren",
]
_scihub_good_mirror: str | None = None
_scihub_lock = threading.Lock()

_PAYWALL_HOSTS = (
    "onlinelibrary.wiley.com", "pubs.acs.org", "sciencedirect.com",
    "link.springer.com", "pubs.rsc.org", "xlink.rsc.org", "iopscience.iop.org",
    "tandfonline.com", "dl.acm.org", "ieeexplore.ieee.org", "journals.aps.org",
    "pubs.aip.org", "science.org", "cell.com", "academic.oup.com",
)
_OA_REPO_HINTS = (
    "ncbi.nlm.nih.gov", "europepmc.org", "arxiv.org", "biorxiv.org", "chemrxiv",
    "mdpi.com", "frontiersin.org", "hindawi.com", "doaj.org", "osti.gov",
    "/bitstream", "repository", "eprint", "openalex", "semanticscholar.org",
)

def _is_pdf_link(url: str) -> bool:
    u = (url or "").lower()
    return (u.endswith(".pdf") or ".pdf?" in u or "/pdf/" in u
            or "/pdfft" in u or u.endswith("/pdf") or "blobtype=pdf" in u)

def _is_paywall_landing(url: str) -> bool:
    if _is_pdf_link(url):
        return False
    try:
        host = urlparse(url).netloc.lower()
        return any(h in host for h in _PAYWALL_HOSTS)
    except Exception:
        return False

def _candidate_score(url: str) -> int:
    s = 0
    if _is_pdf_link(url):
        s += 100
    u = (url or "").lower()
    if any(h in u for h in _OA_REPO_HINTS):
        s += 40
    if _is_paywall_landing(url):
        s -= 100
    return s

def _fetch_unpaywall_mirrors(doi: str) -> list[str]:
    if not doi:
        return []
    urls = []
    try:
        r = requests.get(
            f"https://api.unpaywall.org/v2/{doi}",
            params={"email": "chkam.dev@gmail.com"},
            headers=api_headers(), timeout=12,
        )
        if r.status_code == 200:
            data = r.json()
            best_loc = data.get("best_oa_location") or {}
            for key in ["url_for_pdf", "url", "url_for_landing_page"]:
                u = best_loc.get(key)
                if u and _is_real_http_url(u) and u not in urls:
                    urls.append(u)
            for loc in data.get("oa_locations", []):
                for key in ["url_for_pdf", "url", "url_for_landing_page"]:
                    u = loc.get(key)
                    if u and _is_real_http_url(u) and u not in urls:
                        urls.append(u)
    except Exception:
        pass
    return urls

def _fetch_scihub_mirrors(doi: str) -> list[str]:
    if not doi:
        return []
    with _scihub_lock:
        cached = _scihub_good_mirror
    domains = ([cached] if cached else []) + [m for m in SCIHUB_MIRRORS if m != cached]

    for d in domains:
        try:
            with open_stealth_session("chrome120") as s:
                r = s.get(f"{d}/{doi}", timeout=15, verify=False)
            if r.status_code != 200 or not r.text:
                continue
            html_text = r.text
            src = ""

            if HAS_BS4:
                soup = BeautifulSoup(html_text, "html.parser")
                node = (soup.find("iframe", id="pdf") or soup.find("embed", id="pdf")
                        or soup.find("iframe") or soup.find("embed"))
                if node and node.get("src"):
                    src = node["src"]
                if not src:
                    btn = soup.find("a", string=re.compile(r"download", re.I)) or soup.select_one("#buttons a, .download a")
                    if btn and btn.get("href"):
                        src = btn["href"]

            if not src:
                m = re.search(r'(?:src|href)\s*=\s*["\']([^"\']+\.pdf[^"\']*)["\']', html_text, re.I)
                if m:
                    src = m.group(1)

            if not src:
                continue

            src = src.split("#")[0].strip()
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = d + src
            elif not src.startswith("http"):
                src = f"{d}/{src.lstrip('/')}"

            if _is_real_http_url(src):
                with _scihub_lock:
                    globals()["_scihub_good_mirror"] = d
                return [src]
        except Exception:
            continue
    return []

def _scrape_pdf_from_html(html_text: str, base_url: str) -> str:
    blocked_substrings = [
        "citation", "ris", "bibtex", "share", "facebook", "twitter",
        "linkedin", "login", "register", "subscribe", "metrics", "history", "epdf"
    ]

    def is_valid(u: str) -> bool:
        if not _is_real_http_url(u):
            return False
        ul = u.lower()
        return not any(sub in ul for sub in blocked_substrings)

    if HAS_BS4:
        soup = BeautifulSoup(html_text, "html.parser")
        meta_names = ["citation_pdf_url", "citation_fulltext_pdf", "eprints.document_url", "DC.identifier"]
        for name in meta_names:
            for meta in soup.find_all("meta", attrs={"name": name}):
                content = meta.get("content", "").strip()
                if content:
                    candidate = urljoin(base_url, content)
                    if is_valid(candidate) and _is_pdf_link(candidate):
                        return candidate

        for link in soup.find_all("link", rel=re.compile(r"alternate|canonical", re.I), type="application/pdf"):
            href = link.get("href", "").strip()
            if href:
                candidate = urljoin(base_url, href)
                if is_valid(candidate):
                    return candidate

        for tag in soup.find_all("a", href=True):
            href = tag["href"].strip()
            if href.lower().startswith(("javascript:", "mailto:", "data:", "#")):
                continue
            text = (tag.get_text() + " " + " ".join(tag.get("class") or []) + " " + (tag.get("id") or "")).lower()
            href_lower = href.lower()
            if (".pdf" in href_lower or "/pdf/" in href_lower or "download" in href_lower or "pdf" in text):
                candidate = urljoin(base_url, href)
                if is_valid(candidate) and _is_pdf_link(candidate):
                    return candidate

    m = re.search(r'href=["\']([^"\']*\.pdf[^"\']*)["\']', html_text, re.I)
    if m:
        candidate = urljoin(base_url, m.group(1))
        if is_valid(candidate):
            return candidate
    return ""

_path_lock = threading.Lock()

def _make_path(folder: Path, title: str, ext: str) -> Path:
    clean = re.sub(r'[\\/*?":<>|]', "_", clean_title(title)).strip().rstrip(". ")[:80]
    if not clean:
        clean = hashlib.md5(title.encode()).hexdigest()[:16]
    with _path_lock:
        path = folder / f"{clean}{ext}"
        c = 1
        while path.exists():
            path = folder / f"{clean}_{c}{ext}"
            c += 1
        try:
            path.touch()
        except OSError:
            path = folder / f"{hashlib.md5(title.encode()).hexdigest()[:16]}{ext}"
            path.touch()
    return path

def download_article(data: tuple) -> dict:
    url, title, doi, folder, ctx, keyword = data
    result = {"url": url, "title": title, "success": False, "bytes": 0, "skipped": False}

    with ctx.lock:
        if ctx._at_capacity(keyword) or ctx.cancellation_event.is_set():
            result["skipped"] = True
            return result

    tried_unpaywall = False
    tried_scihub = False
    raw_candidates = []
    if doi:
        raw_candidates.extend(_fetch_unpaywall_mirrors(doi))
        tried_unpaywall = True
    raw_candidates.append(url)

    # Dedup and prioritize candidate URLs
    seen_cands = set()
    candidate_urls = []
    for u in raw_candidates:
        if not _is_real_http_url(u):
            continue
        if doi and _is_paywall_landing(u):
            continue
        n = normalise_url(u)
        if n not in seen_cands:
            seen_cands.add(n)
            candidate_urls.append(u)
    candidate_urls.sort(key=_candidate_score, reverse=True)
    if not candidate_urls and _is_real_http_url(url):
        candidate_urls = [url]
    visited_urls = {normalise_url(u) for u in candidate_urls}

    total_requests = 0
    max_requests = MAX_REQUESTS_PER_ARTICLE
    err_str = "Max requests reached"

    while total_requests < max_requests:
        with ctx.lock:
            if ctx._at_capacity(keyword) or ctx.cancellation_event.is_set():
                result["skipped"] = True
                return result

        if not candidate_urls:
            if doi and not tried_scihub:
                tried_scihub = True
                sh = _fetch_scihub_mirrors(doi)
                for mm in sh:
                    norm_mm = normalise_url(mm)
                    if norm_mm not in visited_urls:
                        visited_urls.add(norm_mm)
                        candidate_urls.append(mm)
                if candidate_urls:
                    _log(f"    🔓 Trying Sci-Hub for DOI {doi}", run_id=ctx.run_id)
                    continue
            break

        current_url = candidate_urls.pop(0)
        total_requests += 1

        IMPERSONATE_PROFILES = ["chrome120", "firefox133", "safari15_5"]
        profile = IMPERSONATE_PROFILES[total_requests % len(IMPERSONATE_PROFILES)]

        try:
            with open_stealth_session(profile) as session:
                resp = session.get(
                    current_url,
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=True,
                    verify=False,
                )

            ct = resp.headers.get("Content-Type", "").lower()
            final_url = resp.url

            if resp.status_code != 200:
                raise ValueError(f"HTTP {resp.status_code}")

            resp_content = resp.content
            content_size = len(resp_content)
            actual_pdf = b"%PDF" in resp_content[:1024]

            # Case 1: Got Valid PDF
            if actual_pdf:
                if content_size < 4096:
                    raise ValueError(f"PDF too small ({content_size} bytes)")

                with ctx.lock:
                    if ctx._at_capacity(keyword):
                        result["skipped"] = True
                        return result
                    ctx.successful_downloads += 1
                    if keyword is not None:
                        ctx.kw_done[keyword] = ctx.kw_done.get(keyword, 0) + 1

                path = None
                try:
                    path = _make_path(folder, title, ".pdf")
                    with open(path, "wb") as fh:
                        fh.write(resp_content)
                except Exception:
                    with ctx.lock:
                        ctx.successful_downloads -= 1
                        if keyword is not None:
                            ctx.kw_done[keyword] = max(0, ctx.kw_done.get(keyword, 0) - 1)
                    if path is not None:
                        try:
                            path.unlink()
                        except OSError:
                            pass
                    raise

                with ctx.lock:
                    ctx.total_bytes += content_size
                result["success"] = True
                result["bytes"] = content_size
                result["path"] = str(path)
                return result

            # Case 2: Got HTML page — scrape for PDF link
            is_html = ("html" in ct or b"<html" in resp_content[:2048].lower()
                       or b"<!doctype html" in resp_content[:2048].lower())
            if is_html:
                html_text = resp_content.decode("utf-8", errors="ignore")
                scraped = _scrape_pdf_from_html(html_text, final_url)
                if scraped:
                    norm_scraped = normalise_url(scraped)
                    if norm_scraped not in visited_urls:
                        visited_urls.add(norm_scraped)
                        candidate_urls.insert(0, scraped)
                        _log(f"    🔍 Scraped direct PDF link: {scraped[:60]}", run_id=ctx.run_id)
                        if sleep_check_cancel(random.uniform(0.3, 0.8), ctx):
                            result["skipped"] = True
                            return result
                        continue

                extracted_doi = _extract_doi(current_url) or _extract_doi(final_url)
                if extracted_doi and not tried_unpaywall:
                    tried_unpaywall = True
                    doi = doi or extracted_doi
                    mirrors = _fetch_unpaywall_mirrors(extracted_doi)
                    for m in mirrors:
                        norm_m = normalise_url(m)
                        if norm_m not in visited_urls:
                            visited_urls.add(norm_m)
                            candidate_urls.append(m)
                    if mirrors:
                        _log(f"    🔓 Unpaywall fallback for DOI {extracted_doi}: {len(mirrors)} mirrors",
                             run_id=ctx.run_id)
                        continue
                raise ValueError("HTML landing page without direct PDF")

            raise ValueError(f"Unsupported content-type: {ct}")

        except Exception as exc:
            err_str = str(exc)
            extracted_doi = _extract_doi(current_url)
            if extracted_doi and not tried_unpaywall:
                tried_unpaywall = True
                doi = doi or extracted_doi
                mirrors = _fetch_unpaywall_mirrors(extracted_doi)
                for m in mirrors:
                    norm_m = normalise_url(m)
                    if norm_m not in visited_urls:
                        visited_urls.add(norm_m)
                        candidate_urls.append(m)
                if mirrors:
                    continue

            if "Connection reset" in err_str or "10054" in err_str:
                wait = min(2 ** total_requests + random.uniform(1, 2), MAX_BACKOFF_S)
                if sleep_check_cancel(wait, ctx):
                    result["skipped"] = True
                    return result
            else:
                if candidate_urls and total_requests < max_requests:
                    if sleep_check_cancel(1.0 + random.uniform(0, 0.5), ctx):
                        result["skipped"] = True
                        return result

    if not result["success"] and not result["skipped"]:
        result["error"] = err_str[:60]
        with ctx.lock:
            ctx.failed_downloads += 1
    return result

# ══════════════════════════════════════════════════════════════════════════════
#  CITATION & CORPUS EXPORTS (.bib, .ris, APA 7, .csv, .json)
# ══════════════════════════════════════════════════════════════════════════════

def write_bibliography(papers: list[Paper], folder: Path):
    """Write BibTeX, RIS, APA 7, CSV, and JSON metadata for all downloaded papers."""
    papers = [p for p in papers if p.pdf_path]
    if not papers:
        return
    papers.sort(key=lambda p: (p.quartile or "Q9", -p.citations))
    used_keys: set[str] = set()

    # 1. BibTeX (references.bib)
    with open(folder / "references.bib", "w", encoding="utf-8") as f:
        for p in papers:
            key = _bib_key(p, used_keys)
            f.write(f"@article{{{key},\n")
            if p.authors:
                f.write(f"  author  = {{{' and '.join(p.authors)}}},\n")
            f.write(f"  title   = {{{{{clean_title(p.title)}}}}},\n")
            if p.journal:
                f.write(f"  journal = {{{p.journal}}},\n")
            if p.year:
                f.write(f"  year    = {{{p.year}}},\n")
            if p.doi:
                f.write(f"  doi     = {{{p.doi}}},\n")
            if p.url:
                f.write(f"  url     = {{{p.url}}},\n")
            note_parts = []
            if p.quartile:
                note_parts.append(p.quartile)
            if p.citations:
                note_parts.append(f"cited-by: {p.citations}")
            if note_parts:
                f.write(f"  note    = {{{', '.join(note_parts)}}},\n")
            f.write("}\n\n")

    # 2. RIS (references.ris)
    with open(folder / "references.ris", "w", encoding="utf-8") as f:
        for p in papers:
            f.write("TY  - JOUR\n")
            for a in p.authors:
                last, initials = _parse_author_name(a)
                f.write(f"AU  - {last}, {initials}\n" if initials else f"AU  - {last}\n")
            f.write(f"TI  - {clean_title(p.title)}\n")
            if p.journal:
                f.write(f"JO  - {p.journal}\n")
            if p.year:
                f.write(f"PY  - {p.year}\n")
            if p.doi:
                f.write(f"DO  - {p.doi}\n")
            if p.url:
                f.write(f"UR  - {p.url}\n")
            if p.pdf_path:
                f.write(f"L1  - {Path(p.pdf_path).name}\n")
            if p.quartile:
                f.write(f"N1  - SJR Quartile: {p.quartile}, Citations: {p.citations}\n")
            f.write("ER  - \n\n")

    # 3. APA 7th Edition (references_APA.txt)
    with open(folder / "references_APA.txt", "w", encoding="utf-8") as f:
        for p in papers:
            au = _authors_apa(p.authors)
            yr = f"({p.year})." if p.year else "(n.d.)."
            title = clean_title(p.title)
            jrn = f" {p.journal}." if p.journal else ""
            doi = f" https://doi.org/{p.doi}" if p.doi else (f" {p.url}" if p.url else "")
            f.write(f"{au} {yr} {title}.{jrn}{doi}\n\n".strip() + "\n\n")

    # 4. CSV Spreadsheet (results.csv with UTF-8 BOM for Excel)
    with open(folder / "results.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Quartile", "Citations", "Year", "Title", "Authors",
                    "Journal", "DOI", "Source", "PDF File", "Full Path"])
        for p in papers:
            w.writerow([
                p.quartile or "Unranked",
                p.citations,
                p.year,
                clean_title(p.title),
                "; ".join(p.authors),
                p.journal,
                p.doi,
                p.source,
                Path(p.pdf_path).name if p.pdf_path else "",
                p.pdf_path,
            ])

    # 5. Machine-readable JSON Corpus (corpus_metadata.json)
    with open(folder / "corpus_metadata.json", "w", encoding="utf-8") as f:
        corpus = [
            {
                "title": clean_title(p.title),
                "authors": p.authors,
                "year": p.year,
                "journal": p.journal,
                "doi": p.doi,
                "issns": p.issns,
                "citations": p.citations,
                "quartile": p.quartile,
                "source": p.source,
                "pdf_filename": Path(p.pdf_path).name if p.pdf_path else "",
                "pdf_path": p.pdf_path,
                "abstract": p.abstract,
            }
            for p in papers
        ]
        json.dump(corpus, f, indent=2, ensure_ascii=False)

# ══════════════════════════════════════════════════════════════════════════════
#  SQLITE HISTORY & TOPIC MEMORY
# ══════════════════════════════════════════════════════════════════════════════

def _history_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(HISTORY_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            query TEXT,
            identifier TEXT,
            title TEXT,
            quartile TEXT,
            year TEXT,
            journal TEXT,
            citations INTEGER,
            filename TEXT,
            date TEXT,
            PRIMARY KEY (query, identifier)
        )
    """)
    # Check for legacy schema and migrate missing columns
    try:
        cursor = conn.execute("PRAGMA table_info(history)")
        existing_cols = {row[1] for row in cursor.fetchall()}
        for col, col_type in [("identifier", "TEXT"), ("year", "TEXT"), ("journal", "TEXT"),
                              ("citations", "INTEGER"), ("filename", "TEXT")]:
            if col not in existing_cols:
                try:
                    conn.execute(f"ALTER TABLE history ADD COLUMN {col} {col_type}")
                except Exception:
                    pass
        if "identifier" in existing_cols and "doi" in existing_cols:
            conn.execute("UPDATE history SET identifier = doi WHERE identifier IS NULL OR identifier = ''")
            conn.commit()
    except Exception:
        pass
    return conn

def query_seen_count(query_norm: str) -> int:
    try:
        conn = _history_conn()
        n = conn.execute("SELECT COUNT(*) FROM history WHERE query=?", (query_norm,)).fetchone()[0]
        conn.close()
        return n
    except Exception:
        return 0

def history_identifiers(query_norm: str) -> set[str]:
    try:
        conn = _history_conn()
        rows = conn.execute("SELECT identifier FROM history WHERE query=?", (query_norm,)).fetchall()
        conn.close()
        return {r[0] for r in rows if r[0]}
    except Exception:
        return set()

def record_history(query_norm: str, papers: list[Paper]):
    try:
        conn = _history_conn()
        today = time.strftime("%Y-%m-%d")
        for p in papers:
            if p.pdf_path:
                ident = p.clean_doi() or p.title_hash()
                conn.execute(
                    """INSERT OR REPLACE INTO history
                       (query, identifier, title, quartile, year, journal, citations, filename, date)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (query_norm, ident, clean_title(p.title)[:200], p.quartile,
                     p.year, p.journal[:100], p.citations, Path(p.pdf_path).name, today)
                )
        conn.commit()
        conn.close()
    except Exception as e:
        _log(f"  ⚠️  History save error: {e}")

# ══════════════════════════════════════════════════════════════════════════════
#  CORE WORKFLOW ENGINE (HEADLESS & GUI REUSABLE)
# ══════════════════════════════════════════════════════════════════════════════

def execute_research_workflow(
    keywords: str,
    focus: str = "",
    year_start: str = "2023",
    year_end: str = "2026",
    max_articles: int = 50,
    save_folder: Path | str = "",
    quartile_filter: str = "all_ranked",  # 'q1_q2', 'all_ranked', 'all'
    sort_strategy: str = "quartile_cits",  # 'quartile_cits', 'citations', 'newest'
    mode: str = "fresh",
    ctx: DownloadContext | None = None,
    progress_callback=None,
    status_callback=None,
) -> list[Paper]:
    """Autonomous execution of literature harvest, ranking, download and citations."""
    global seen_urls, seen_dois, seen_titles, log_file_path

    folder = Path(save_folder) if save_folder else get_default_save_folder()
    q_hi = folder / "Q1_Q2"
    q_lo = folder / "Q3_Q4"
    for d in (folder, q_hi, q_lo):
        d.mkdir(parents=True, exist_ok=True)

    log_file_path = folder / "research_download.log"
    with lock_seen:
        seen_urls.clear()
        seen_dois.clear()
        seen_titles.clear()

    run_id = ctx.run_id if ctx else 1
    query_norm = f"{keywords} {focus}".strip().lower()

    if status_callback:
        status_callback("Phase 1: Searching scholarly databases…", "#58a6ff")
    _log(f"\n{'═'*65}", run_id=run_id)
    _log(f"  🚀 RESEARCH PDF DOWNLOADER — v9 Pro", run_id=run_id)
    _log(f"  Query: {keywords} | Focus: {focus or 'None'} | Years: {year_start}-{year_end}", run_id=run_id)
    _log(f"  Target: {max_articles} PDFs | Filter: {quartile_filter} | Sort: {sort_strategy}", run_id=run_id)
    _log(f"  Folder: {folder}", run_id=run_id)
    _log(f"{'═'*65}\n", run_id=run_id)

    # ── Long-title keyword splitter ───────────────────────────────────────────
    phrases = extract_keywords(keywords)
    multi_kw = len(phrases) > 1 and max_articles >= 2
    if multi_kw:
        k = min(len(phrases), max_articles)
        base, rem = divmod(max_articles, k)
        groups = []
        for i, ph in enumerate(phrases[:k]):
            share = base + (1 if i < rem else 0)
            groups.append((ph, f"{ph} {focus}".strip(), share))
        if ctx:
            ctx.kw_targets = {lbl: sh for lbl, _, sh in groups}
            ctx.kw_done    = {lbl: 0  for lbl, _, _ in groups}
        _log(f"🔑 Long title split into {len(groups)} keyword groups:", run_id=run_id)
        for lbl, _, sh in groups:
            _log(f"     • {lbl}  →  {sh} article(s)", run_id=run_id)
    else:
        query_term = f"{keywords} {focus}".strip()
        groups = [(query_term, query_term, max_articles)]

    # ── PHASE 1: Harvest ──────────────────────────────────────────────────────
    papers: list[Paper] = []
    pool_size = max(max_articles * 6, 200)

    HARVESTERS = [
        ("OpenAlex",         harvest_openalex,         max(200, pool_size)),
        ("Crossref",         harvest_crossref,         250),
        ("Europe PMC",       harvest_europepmc,        150),
        ("PubMed/PMC",       harvest_pubmed,           150),
        ("Semantic Scholar", harvest_semantic_scholar, 150),
        ("DOAJ",             harvest_doaj,             100),
        ("arXiv",            harvest_arxiv,            100),
        ("CORE",             harvest_core,             100),
        ("BASE",             harvest_base,             100),
    ]

    for label, search, share in groups:
        if is_cancelled(ctx):
            break
        group_pool = max(share * 8, 60) if multi_kw else pool_size
        group_start = len(papers)
        if multi_kw:
            _log(f"\n🔎 Keyword '{label}'…", run_id=run_id)

        for name, fn, cap in HARVESTERS:
            if len(papers) - group_start >= group_pool or is_cancelled(ctx):
                break
            try:
                cand = fn(search, year_start, year_end, max_res=min(cap, group_pool), ctx=ctx)
                for p in cand:
                    p.keyword = label if multi_kw else ""
                    papers.append(p)
            except Exception as e:
                _log(f"  ⚠️ {name} harvest error: {e}", run_id=run_id)

    if is_cancelled(ctx) or not papers:
        return []

    # ── PHASE 2: Rank & Filter ────────────────────────────────────────────────
    if status_callback:
        status_callback("Phase 2: Ranking by journal quartile & citations…", "#58a6ff")
    _log(f"\n📈 Phase 2: Processing {len(papers)} candidate papers…", run_id=run_id)

    load_scimago_quartiles()
    enrich_with_openalex(papers, ctx=ctx)

    for p in papers:
        p.quartile = quartile_for(p.issns, p.journal)

    # Apply user quartile filter
    if quartile_filter == "q1_q2":
        filtered = [p for p in papers if p.quartile in ("Q1", "Q2")]
    elif quartile_filter == "all_ranked":
        filtered = [p for p in papers if p.quartile in ("Q1", "Q2", "Q3", "Q4")]
    else:  # 'all'
        filtered = papers
        for p in filtered:
            if not p.quartile:
                p.quartile = "Preprint" if "arxiv" in p.journal.lower() or "arxiv" in p.source.lower() else "Unranked"

    if mode == "incremental":
        already = history_identifiers(query_norm)
        before = len(filtered)
        filtered = [p for p in filtered if (p.clean_doi() not in already and p.title_hash() not in already)]
        _log(f"  ♻️  Incremental: skipped {before - len(filtered)} already-downloaded, {len(filtered)} new", run_id=run_id)

    if not filtered:
        _log("  ⚠️  No papers passed the quartile & relevance filters.", run_id=run_id)
        return []

    # Sorting
    if sort_strategy == "citations":
        filtered.sort(key=lambda p: -p.citations)
    elif sort_strategy == "newest":
        filtered.sort(key=lambda p: (-(int(p.year) if p.year.isdigit() else 0), -p.citations))
    else:  # 'quartile_cits'
        q_order = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "Preprint": 5, "Unranked": 6, "": 7}
        filtered.sort(key=lambda p: (q_order.get(p.quartile, 9), -p.citations))

    q1 = sum(1 for p in filtered if p.quartile == "Q1")
    q2 = sum(1 for p in filtered if p.quartile == "Q2")
    q3 = sum(1 for p in filtered if p.quartile == "Q3")
    q4 = sum(1 for p in filtered if p.quartile == "Q4")
    unranked = len(filtered) - (q1 + q2 + q3 + q4)
    _log(f"\n🎯 Ranked Pool: {len(filtered)} papers (Q1:{q1} | Q2:{q2} | Q3:{q3} | Q4:{q4} | Other:{unranked})", run_id=run_id)

    # ── PHASE 3: Download ─────────────────────────────────────────────────────
    if status_callback:
        status_callback(f"Phase 3: Downloading PDFs (0/{max_articles})…", "#3fb950")
    _log(f"🚀 Downloading top {max_articles} papers with {MAX_WORKERS} threads…\n", run_id=run_id)

    if not ctx:
        ctx = DownloadContext(run_id, max_articles, folder)

    by_url: dict[str, Paper] = {}
    targets = []
    for p in filtered:
        dest = q_hi if p.quartile in ("Q1", "Q2") else q_lo
        by_url[p.url] = p
        pk = p.keyword or None
        targets.append((p.url, clean_title(p.title), p.clean_doi(), dest, ctx, pk))

    done_count = 0
    skipped_urls: set[str] = set()

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _drain(items):
        nonlocal done_count
        if not items:
            return
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(download_article, item): item for item in items}
            try:
                for future in as_completed(futures):
                    if ctx.cancellation_event.is_set():
                        for f in futures: f.cancel()
                        break
                    try:
                        res = future.result()
                    except Exception as e:
                        _log(f"  ❌ Thread error: {e}", run_id=run_id)
                        continue
                    if res.get("skipped"):
                        skipped_urls.add(res["url"])
                        continue
                    skipped_urls.discard(res["url"])

                    done_count += 1
                    if res["success"]:
                        p = by_url.get(res["url"])
                        if p:
                            p.pdf_path = res.get("path", "")

                    with ctx.lock:
                        succ = ctx.successful_downloads

                    if progress_callback:
                        progress_callback(succ, max_articles)
                    if status_callback:
                        status_callback(f"Downloading: {succ}/{max_articles} saved (checked {done_count})", "#3fb950")

                    icon = "✅" if res["success"] else "❌"
                    pp = by_url.get(res["url"])
                    qtag = pp.quartile if pp else ""
                    detail = f"{res['bytes'] // 1024} KB" if res["success"] else res.get("error", "failed")[:45]
                    _log(f"  {icon} [{qtag or '—':<8}] {res['title'][:54]:<54} {detail}", run_id=run_id)

                    with ctx.lock:
                        if ctx.successful_downloads >= ctx.target_downloads:
                            break
            finally:
                for f in futures: f.cancel()

    # Pass 1: Balanced across keywords
    _drain(targets)

    # Pass 2: Reclaim leftover global quota from held-back items
    with ctx.lock:
        need_more = ctx.successful_downloads < ctx.target_downloads and not ctx.cancellation_event.is_set()
        gap = ctx.target_downloads - ctx.successful_downloads
    if multi_kw and need_more and skipped_urls:
        reclaim = [
            (p.url, clean_title(p.title), p.clean_doi(),
             q_hi if p.quartile in ("Q1", "Q2") else q_lo, ctx, None)
            for p in filtered if not p.pdf_path and p.url in skipped_urls
        ]
        if reclaim:
            _log(f"\n♻️  Filling {gap} leftover slot(s) from held-back candidates…", run_id=run_id)
            _drain(reclaim)

    # ── PHASE 4: Citations & Memory ───────────────────────────────────────────
    downloaded = [p for p in filtered if p.pdf_path]
    if downloaded:
        _log(f"\n📚 Writing references.bib, .ris, APA 7th, results.csv, corpus_metadata.json…", run_id=run_id)
        try:
            write_bibliography(downloaded, folder)
        except Exception as e:
            _log(f"  ⚠️  Citation export failed: {e}", run_id=run_id)
        record_history(query_norm, downloaded)

    with ctx.lock:
        succ = ctx.successful_downloads
        hi = sum(1 for p in downloaded if p.quartile in ("Q1", "Q2"))
        lo = len(downloaded) - hi

    _log(f"\n{'═'*65}", run_id=run_id)
    _log(f"  🏆 COMPLETE — {succ} PDFs saved   (Q1_Q2: {hi}  |  Q3_Q4/Preprint: {lo})", run_id=run_id)
    _log(f"  📁 Output Directory: {folder}", run_id=run_id)
    _log(f"{'═'*65}\n", run_id=run_id)

    if status_callback:
        status_callback(f"Complete — {succ} PDFs (Q1_Q2: {hi}, Q3_Q4: {lo})", "#3fb950")
    return downloaded

# ══════════════════════════════════════════════════════════════════════════════
#  TKINTER GUI DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

if HAS_TKINTER:
    class ResearchAppDashboard:
        BG        = "#0d1117"
        CARD      = "#161b22"
        CARD_HI   = "#1c2431"
        BORDER    = "#30363d"
        TXT       = "#e6edf3"
        TXT_DIM   = "#8b949e"
        ACCENT    = "#58a6ff"
        GREEN     = "#3fb950"
        GREEN_HI  = "#2ea043"
        RED       = "#f85149"
        RED_HI    = "#da3633"

        def __init__(self):
            self.root = tk.Tk()
            self.root.title("Research PDF Downloader v9 Pro — Developed by Kamran Ashraf")
            self.root.geometry("920x760")
            self.root.minsize(740, 620)
            self.root.configure(bg=self.BG)

            self.title_font  = ("Segoe UI Semibold", 17)
            self.sub_font    = ("Segoe UI", 9)
            self.header_font = ("Segoe UI Semibold", 10)
            self.label_font  = ("Segoe UI", 10)
            self.btn_font    = ("Segoe UI Semibold", 10)
            self.mono_font   = ("Cascadia Mono", 9) if self._font_exists("Cascadia Mono") else ("Consolas", 9)

            self.style = ttk.Style()
            self.style.theme_use("default")
            self.style.configure("G.Horizontal.TProgressbar",
                                 troughcolor=self.CARD, bordercolor=self.CARD,
                                 background=self.GREEN, lightcolor=self.GREEN,
                                 darkcolor=self.GREEN, thickness=10)
            self.style.configure("TCombobox", fieldbackground=self.BG, background=self.CARD,
                                 foreground=self.TXT, bordercolor=self.BORDER)

            # Header
            header = tk.Frame(self.root, bg=self.BG)
            header.pack(fill="x", padx=24, pady=(18, 6))

            sig = tk.Frame(header, bg=self.BG)
            sig.pack(side="right", anchor="ne")
            tk.Label(sig, text="D E V E L O P E D   B Y", bg=self.BG, fg=self.TXT_DIM,
                     font=("Segoe UI", 7, "bold")).pack(anchor="e")
            tk.Label(sig, text="Kamran Ashraf", bg=self.BG, fg=self.ACCENT,
                     font=("Segoe UI Semibold", 14)).pack(anchor="e", pady=(1, 0))
            tk.Frame(sig, bg=self.ACCENT, height=2, width=118).pack(anchor="e", pady=(3, 0))

            htext = tk.Frame(header, bg=self.BG)
            htext.pack(side="left", anchor="w")
            tk.Label(htext, text="Research PDF Downloader v9 Pro", bg=self.BG, fg=self.TXT,
                     font=self.title_font).pack(anchor="w")
            engine = "   •   curl_cffi active (stealth)" if HAS_CFFI else "   •   requests mode"
            tk.Label(htext, text="9 APIs  ·  Scimago dual-indexed  ·  BibTeX / RIS / APA 7 / CSV / JSON" + engine,
                     bg=self.BG, fg=self.TXT_DIM, font=self.sub_font).pack(anchor="w", pady=(2, 0))

            # Settings Card
            card = self._card(self.root, "Search & Filtration Settings")
            card.pack(fill="x", padx=24, pady=(8, 6))
            grid = tk.Frame(card, bg=self.CARD)
            grid.pack(fill="x", padx=16, pady=(2, 12))
            grid.columnconfigure(1, weight=1)
            grid.columnconfigure(3, weight=1)

            self.ent_keywords = self._field(grid, "Keywords / Title", 0, 0, colspan=3)
            self.ent_focus    = self._field(grid, "Focus (optional)", 1, 0, colspan=3)

            self.ent_y1       = self._field(grid, "Start year", 2, 0)
            self.ent_y1.insert(0, "2023")
            self.ent_y2       = self._field(grid, "End year", 2, 2)
            self.ent_y2.insert(0, "2026")

            self.ent_max      = self._field(grid, "Max articles", 3, 0)
            self.ent_max.insert(0, "50")

            # Quartile & Sort options
            tk.Label(grid, text="Journal Filter", bg=self.CARD, fg=self.TXT_DIM,
                     font=self.label_font).grid(row=3, column=2, sticky="w", pady=(8, 4), padx=(0, 10))
            self.cbo_quartile = ttk.Combobox(grid, values=["Q1 + Q2 (High Impact)", "Q1 to Q4 (All Ranked)", "All (Including Preprints)"],
                                             state="readonly", font=self.label_font)
            self.cbo_quartile.current(1)
            self.cbo_quartile.grid(row=3, column=3, sticky="we", pady=(8, 4))

            # Save folder row
            tk.Label(grid, text="Save folder", bg=self.CARD, fg=self.TXT_DIM,
                     font=self.label_font).grid(row=4, column=0, sticky="w", pady=(8, 4), padx=(0, 10))
            folder_row = tk.Frame(grid, bg=self.CARD)
            folder_row.grid(row=4, column=1, columnspan=3, sticky="we", pady=(8, 4))
            folder_row.columnconfigure(0, weight=1)

            default_dir = str(get_default_save_folder())
            self.ent_folder = tk.Entry(folder_row, bg=self.BG, fg=self.TXT, insertbackground=self.TXT,
                                       relief="flat", font=self.label_font,
                                       highlightthickness=1, highlightbackground=self.BORDER,
                                       highlightcolor=self.ACCENT)
            self.ent_folder.grid(row=0, column=0, sticky="we", ipady=5)
            self.ent_folder.insert(0, default_dir)
            self.btn_browse = self._button(folder_row, "Browse", self._browse_folder, kind="ghost")
            self.btn_browse.grid(row=0, column=1, padx=(8, 0))

            # Controls
            ctrl = tk.Frame(self.root, bg=self.BG)
            ctrl.pack(fill="x", padx=24, pady=(6, 4))
            self.btn_start  = self._button(ctrl, "▶  Start download", self.start_download, kind="primary")
            self.btn_start.pack(side="left")
            self.btn_cancel = self._button(ctrl, "■  Cancel", self.cancel_download, kind="danger")
            self.btn_cancel.pack(side="left", padx=(8, 0))
            self.btn_cancel.configure(state="disabled")

            self.btn_open_folder = self._button(ctrl, "📂 Open Folder", self._open_download_folder, kind="ghost")
            self.btn_open_folder.pack(side="left", padx=(8, 0))

            self.btn_copy = self._button(ctrl, "Copy logs", self._copy_logs, kind="ghost")
            self.btn_copy.pack(side="right")

            # Stats Pills
            stats = tk.Frame(self.root, bg=self.BG)
            stats.pack(fill="x", padx=24, pady=(6, 2))
            self.lbl_stats_time    = self._pill(stats, "⏱  0s")
            self.lbl_stats_data    = self._pill(stats, "⬇  0.00 MB")
            self.lbl_stats_success = self._pill(stats, "✓  0%  (0/0)")

            # Progress Bar
            prog = tk.Frame(self.root, bg=self.BG)
            prog.pack(fill="x", padx=24, pady=(8, 2))
            self.bar_var = tk.DoubleVar()
            self.bar = ttk.Progressbar(prog, variable=self.bar_var, maximum=100,
                                       style="G.Horizontal.TProgressbar")
            self.bar.pack(fill="x")
            self.lbl_progress = tk.Label(prog, text="Idle — configure settings and click Start",
                                         bg=self.BG, fg=self.TXT_DIM, font=self.sub_font)
            self.lbl_progress.pack(anchor="w", pady=(6, 0))

            # Live Log
            log_card = self._card(self.root, "Live Terminal Output")
            log_card.pack(fill="both", expand=True, padx=24, pady=(8, 16))
            log_body = tk.Frame(log_card, bg=self.BG)
            log_body.pack(fill="both", expand=True, padx=12, pady=(0, 12))

            global log_widget
            log_widget = tk.Text(log_body, wrap="word", bg=self.BG, fg="#adbac7",
                                 font=self.mono_font, insertbackground=self.TXT,
                                 relief="flat", state="disabled", padx=10, pady=8,
                                 highlightthickness=0, spacing1=1)
            log_widget.tag_configure("success", foreground=self.GREEN)
            log_widget.tag_configure("error",   foreground=self.RED)
            log_widget.tag_configure("info",    foreground=self.ACCENT)
            log_widget.tag_configure("warning", foreground="#d29922")

            sb = ttk.Scrollbar(log_body, command=log_widget.yview)
            log_widget.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            log_widget.pack(side="left", fill="both", expand=True)

            self.root.bind("<Return>", lambda e: self.start_download() if not self.is_running else None)
            self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

            self.is_running = False
            self.worker_thread = None

        def _font_exists(self, name: str) -> bool:
            try:
                from tkinter import font as tkfont
                return name in tkfont.families()
            except Exception:
                return False

        def _card(self, parent, title: str) -> tk.Frame:
            outer = tk.Frame(parent, bg=self.CARD, highlightthickness=1,
                             highlightbackground=self.BORDER, highlightcolor=self.BORDER)
            tk.Label(outer, text=title, bg=self.CARD, fg=self.ACCENT,
                     font=self.header_font).pack(anchor="w", padx=16, pady=(10, 4))
            return outer

        def _field(self, parent, label, row, col, colspan=1) -> tk.Entry:
            tk.Label(parent, text=label, bg=self.CARD, fg=self.TXT_DIM,
                     font=self.label_font).grid(row=row, column=col, sticky="w",
                                                pady=(8, 4), padx=(0, 10))
            ent = tk.Entry(parent, bg=self.BG, fg=self.TXT, insertbackground=self.TXT,
                           relief="flat", font=self.label_font,
                           highlightthickness=1, highlightbackground=self.BORDER,
                           highlightcolor=self.ACCENT)
            ent.grid(row=row, column=col + 1, columnspan=colspan, sticky="we",
                     pady=(8, 4), ipady=5)
            return ent

        def _button(self, parent, text, command, kind="ghost") -> tk.Button:
            palette = {
                "primary": (self.GREEN, self.GREEN_HI, "white"),
                "danger":  (self.RED,   self.RED_HI,   "white"),
                "ghost":   (self.CARD,  self.BORDER,   self.TXT),
            }
            base, hover, fg = palette[kind]
            btn = tk.Button(parent, text=text, command=command, bg=base, fg=fg,
                            font=self.btn_font, relief="flat", bd=0,
                            activebackground=hover, activeforeground=fg,
                            padx=16, pady=7, cursor="hand2")
            if kind == "ghost":
                btn.configure(highlightthickness=1, highlightbackground=self.BORDER)

            def on_enter(_):
                if str(btn["state"]) != "disabled":
                    btn.configure(bg=hover)
            def on_leave(_):
                if str(btn["state"]) != "disabled":
                    btn.configure(bg=base)
            btn.bind("<Enter>", on_enter)
            btn.bind("<Leave>", on_leave)
            return btn

        def _pill(self, parent, text: str) -> tk.Label:
            lbl = tk.Label(parent, text=text, bg=self.CARD, fg=self.TXT,
                           font=self.label_font, padx=12, pady=4)
            lbl.pack(side="left", padx=(0, 8))
            return lbl

        def _browse_folder(self):
            path = filedialog.askdirectory(initialdir=self.ent_folder.get() or str(Path.home()))
            if path:
                self.ent_folder.delete(0, "end")
                self.ent_folder.insert(0, path)

        def _open_download_folder(self):
            folder_str = self.ent_folder.get().strip()
            if folder_str:
                p = Path(folder_str)
                p.mkdir(parents=True, exist_ok=True)
                if sys.platform == "win32":
                    os.startfile(str(p))
                else:
                    subprocess.Popen(["xdg-open", str(p)])

        def _copy_logs(self):
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(log_widget.get("1.0", "end-1c"))
                self.set_status("Logs copied to clipboard", self.ACCENT)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to copy logs: {e}")

        def set_status(self, text: str, fg: str = "#8b949e"):
            self.root.after(0, lambda: self.lbl_progress.configure(text=text, fg=fg))

        def set_progress(self, val: int, max_val: int):
            self.root.after(0, lambda: (self.bar.configure(maximum=max(max_val, 1)), self.bar_var.set(val)))

        def update_stats(self):
            if not self.is_running or not hasattr(self, "ctx"):
                return
            ctx = self.ctx
            elapsed = int(time.time() - self.start_time)
            self.lbl_stats_time.configure(text=f"⏱  {elapsed}s")

            with ctx.lock:
                mb = ctx.total_bytes / (1024 * 1024)
                succ = ctx.successful_downloads
                fail = ctx.failed_downloads

            self.lbl_stats_data.configure(text=f"⬇  {mb:.2f} MB")
            total = succ + fail
            rate = int((succ / total) * 100) if total > 0 else 0
            self.lbl_stats_success.configure(text=f"✓  {rate}%  ({succ}/{total})")
            self.root.after(1000, self.update_stats)

        def enable_inputs(self, enable=True):
            def _do():
                state = "normal" if enable else "disabled"
                self.ent_keywords.configure(state=state)
                self.ent_focus.configure(state=state)
                self.ent_y1.configure(state=state)
                self.ent_y2.configure(state=state)
                self.ent_max.configure(state=state)
                self.ent_folder.configure(state=state)
                self.btn_browse.configure(state=state)
                self.cbo_quartile.configure(state="readonly" if enable else "disabled")
                self.btn_start.configure(state=state)
                self.btn_cancel.configure(state="normal" if not enable else "disabled")
            self.root.after(0, _do)

        def _finalize_run(self, run_id: int):
            with lock_run_id:
                if run_id != active_run_id:
                    return
            self.enable_inputs(True)
            self.is_running = False

        def on_closing(self):
            if self.is_running:
                if messagebox.askokcancel("Quit", "Downloading is in progress. Cancel and quit?"):
                    self.cancel_download()
                    self.root.destroy()
            else:
                self.root.destroy()

        def cancel_download(self):
            if self.is_running and hasattr(self, "ctx"):
                _log("\n🛑 Cancellation requested. Stopping workers...", run_id=self.ctx.run_id)
                self.ctx.cancellation_event.set()
                self.btn_cancel.configure(state="disabled")
                self.enable_inputs(True)
                self.is_running = False
                self.set_status("Cancelled by user", "#da3637")

        def start_download(self):
            kw = self.ent_keywords.get().strip()
            focus = self.ent_focus.get().strip()
            y1 = self.ent_y1.get().strip()
            y2 = self.ent_y2.get().strip()
            max_str = self.ent_max.get().strip()
            save_path = self.ent_folder.get().strip()

            if self.is_running:
                return

            if not all([kw, y1, y2, save_path]):
                messagebox.showerror("Error", "Keywords, years, and save folder are required.")
                return

            try:
                iy1, iy2 = int(y1), int(y2)
                if not (1900 <= iy1 <= 2100 and 1900 <= iy2 <= 2100):
                    raise ValueError
            except ValueError:
                messagebox.showerror("Error", "Years must be numbers between 1900 and 2100.")
                return
            if iy1 > iy2:
                y1, y2 = str(iy2), str(iy1)

            try:
                max_val = int(max_str or "50")
                max_val = max(1, min(max_val, MAX_ARTICLES))
            except ValueError:
                messagebox.showerror("Error", f"Max articles must be between 1 and {MAX_ARTICLES}.")
                return

            q_sel = self.cbo_quartile.get()
            q_filter = "q1_q2" if "Q1 + Q2" in q_sel else ("all_ranked" if "Q1 to Q4" in q_sel else "all")

            query_norm = f"{kw} {focus}".strip().lower()
            mode = "fresh"
            seen = query_seen_count(query_norm)
            if seen:
                ans = messagebox.askyesnocancel(
                    "Topic Searched Before",
                    f"You have already downloaded {seen} paper(s) for this topic in history.\n\n"
                    f"YES    → Fresh mode: search from zero (may re-download).\n"
                    f"NO     → Incremental mode: skip papers you already have.\n"
                    f"CANCEL → Abort.",
                )
                if ans is None:
                    return
                mode = "fresh" if ans else "incremental"

            log_widget.configure(state="normal")
            log_widget.delete("1.0", "end")
            log_widget.configure(state="disabled")

            global active_run_id
            with lock_run_id:
                active_run_id += 1
                current_run_id = active_run_id

            folder = Path(save_path)
            self.ctx = DownloadContext(current_run_id, max_val, folder)
            self.is_running = True
            self.enable_inputs(False)

            self.start_time = time.time()
            self.lbl_stats_time.configure(text="⏱  0s")
            self.lbl_stats_data.configure(text="⬇  0.00 MB")
            self.lbl_stats_success.configure(text="✓  0%  (0/0)")
            self.update_stats()

            def _thread_worker():
                try:
                    execute_research_workflow(
                        keywords=kw,
                        focus=focus,
                        year_start=y1,
                        year_end=y2,
                        max_articles=max_val,
                        save_folder=folder,
                        quartile_filter=q_filter,
                        mode=mode,
                        ctx=self.ctx,
                        progress_callback=self.set_progress,
                        status_callback=self.set_status,
                    )
                except Exception as e:
                    _log(f"❌ Execution exception: {e}", run_id=current_run_id)
                finally:
                    self._finalize_run(current_run_id)

            self.worker_thread = threading.Thread(target=_thread_worker, daemon=True)
            self.worker_thread.start()

# ══════════════════════════════════════════════════════════════════════════════
#  CLI ARGUMENT PARSER & MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description="Articles Downloader v9 Pro — High-Performance Research Literature Harvester",
    )
    parser.add_argument("--keywords", "-k", type=str, help="Research topic or long title to harvest")
    parser.add_argument("--focus", "-f", type=str, default="", help="Sub-focus or specific methodology")
    parser.add_argument("--start-year", "-y1", type=str, default="2023", help="Start publication year")
    parser.add_argument("--end-year", "-y2", type=str, default="2026", help="End publication year")
    parser.add_argument("--max", "-m", type=int, default=50, help="Maximum number of PDFs to download")
    parser.add_argument("--folder", "-o", type=str, default="", help="Destination directory for downloads")
    parser.add_argument("--quartiles", "-q", choices=["q1_q2", "all_ranked", "all"], default="all_ranked",
                        help="Quartile filter: q1_q2, all_ranked (Q1-Q4), or all")
    parser.add_argument("--sort", "-s", choices=["quartile_cits", "citations", "newest"], default="quartile_cits",
                        help="Sorting strategy")
    parser.add_argument("--mode", choices=["fresh", "incremental"], default="fresh", help="Fresh or Incremental search")
    parser.add_argument("--cli", "--no-gui", action="store_true", help="Run in headless command-line mode without GUI")
    return parser.parse_args()

def main():
    args = parse_args()

    # If CLI mode requested or keywords provided via command line without GUI
    if args.cli or (args.keywords and not HAS_TKINTER):
        if not args.keywords:
            print("Error: --keywords is required when running in CLI mode.")
            sys.exit(1)
        save_dir = Path(args.folder) if args.folder else get_default_save_folder()
        ctx = DownloadContext(1, args.max, save_dir)
        execute_research_workflow(
            keywords=args.keywords,
            focus=args.focus,
            year_start=args.start_year,
            year_end=args.end_year,
            max_articles=args.max,
            save_folder=save_dir,
            quartile_filter=args.quartiles,
            sort_strategy=args.sort,
            mode=args.mode,
            ctx=ctx,
        )
        return

    # Default to GUI
    if HAS_TKINTER:
        app = ResearchAppDashboard()
        if args.keywords:
            app.ent_keywords.delete(0, "end")
            app.ent_keywords.insert(0, args.keywords)
        if args.focus:
            app.ent_focus.delete(0, "end")
            app.ent_focus.insert(0, args.focus)
        if args.folder:
            app.ent_folder.delete(0, "end")
            app.ent_folder.insert(0, args.folder)
        app.root.mainloop()
    else:
        print("Tkinter is not available. Please run with --cli flag or install python3-tk.")

if __name__ == "__main__":
    main()
