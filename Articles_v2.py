"""
Articles_v2.py — Research PDF Downloader  ██ v10 Ultra Pro ██
================================================================================
Developed by Kamran Ashraf.

Searches 9 scholarly APIs (no keys required, polite rate-limiting),
filters papers with a hybrid title+abstract relevance gate (minimum 60% match),
eliminates cross-disciplinary collisions (e.g. urology vs battery acronyms),
suppresses errata and conference meeting abstracts,
ranks papers by journal quartile (Scimago dual-index with abbreviation expansion)
and citation count, downloads full-text PDFs with stealth session rotation and
multi-tier cascading fallback (Direct PDF -> Open Access Repos -> Unpaywall ->
Publisher Scraper -> Sci-Hub), and writes ready-to-import citations
(.bib, .ris, APA 7th, CSV with relevance %, JSON corpus metadata).

  PHASE 1 — HARVEST (Hybrid Relevance Gate Across 9 Scholarly Engines)
    1. OpenAlex       — 250 M+ works; abstracts reconstructed, concepts indexed
    2. Crossref       — 150 M+ works; bibliographic query targeting & DOI resolution
    3. Europe PMC     — Direct full-text PDFs & PMC open-access repository
    4. PubMed / PMC   — NCBI E-Utilities API (36 M+ biomedical/materials/energy)
    5. Semantic Scholar — AI-indexed open-access research papers
    6. DOAJ           — Directory of Open Access Journals
    7. arXiv          — STEM preprints with direct PDF streaming
    8. CORE.ac.uk     — Open repository harvester
    9. BASE           — Bielefeld Academic Search Engine

  PHASE 2 — DEDUPLICATE & RANK
    Multi-tier deduplication (Normalized DOI + Title Hash + URL)
    -> Scimago Journal Ranking (ISSN + Title + Abbreviation Expansion)
    -> Configurable Quartile Filtering (Q1+Q2, Q1-Q4, or All)
    -> Sort by Quartile/Citations, Citation Count, or Publication Year

  PHASE 3 — DOWNLOAD (Stealth Session Concurrency, Multi-Tier Fallback)
    Candidate URLs -> Synthesized Direct PDF -> OA Repos -> Scraper -> Sci-Hub
    Q1/Q2 -> "Q1_Q2/",  Q3/Q4 / Preprints -> "Q3_Q4/"

  PHASE 4 — CITATIONS & MEMORY
    references.bib (LaTeX-safe, braced) / references.ris / references_APA.txt (APA 7)
    results.csv (Excel-friendly UTF-8 BOM with Relevance %) / corpus_metadata.json
    SQLite history enables fresh vs incremental search per topic

  CLI / AUTOMATION:
    python Articles_v2.py --cli --keywords "zinc air battery" --max 25 --folder ./pdfs --min-relevance 0.60
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
import hashlib
import html
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import subprocess
import sys
import ctypes
import warnings

def enable_high_dpi_awareness():
    """Enable Windows Per-Monitor High-DPI v2 scaling for razor-sharp 4K/HD rendering."""
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass

enable_high_dpi_awareness()

# Suppress urllib3 and requests dependency mismatch warnings cleanly
warnings.filterwarnings("ignore", message=".*urllib3.*")
warnings.filterwarnings("ignore", message=".*RequestsDependencyWarning.*")
warnings.filterwarnings("ignore", category=UserWarning, module="requests")


# Ensure standard UTF-8 console output for Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import threading
import time
import urllib3
import xml.etree.ElementTree as ET

# Suppress insecure request warnings
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
MAX_WORKERS     = 12          # Concurrency: gentle on scholarly APIs
REQUEST_TIMEOUT = 45          # Timeout for PDFs and slow CDNs
MAX_REQUESTS_PER_ARTICLE = 10 # Candidate link trials per article
MAX_BACKOFF_S   = 25          # Maximum back-off sleep time
DEFAULT_MIN_RELEVANCE = 0.60  # Default 60% minimum relevance matching

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
]

PROXIES: list[str] = []

SCRIPT_DIR    = Path(__file__).resolve().parent
SCIMAGO_CACHE = SCRIPT_DIR / "scimago_ranks.csv"
HISTORY_DB    = SCRIPT_DIR / "research_history.db"
SCIMAGO_URL   = "https://www.scimagojr.com/journalrank.php?out=xls"

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
    relevance_score: float = 0.0
    concepts: list[str] = field(default_factory=list)

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

import queue
_sse_subscribers: list[queue.Queue] = []
_subscribers_lock = threading.Lock()

def _broadcast_sse(event_data: dict):
    with _subscribers_lock:
        dead = []
        for q in _sse_subscribers:
            try:
                q.put_nowait(event_data)
            except Exception:
                dead.append(q)
        for q in dead:
            if q in _sse_subscribers:
                _sse_subscribers.remove(q)

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

def _log(msg: str, run_id: int | None = None, tag: str | None = None, color: str | None = None):
    if run_id is not None:
        with lock_run_id:
            if run_id != active_run_id:
                return
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        try:
            print(msg.encode("ascii", errors="replace").decode("ascii"), flush=True)
        except Exception:
            pass

    if log_file_path:
        try:
            with open(log_file_path, "a", encoding="utf-8") as lf:
                lf.write(msg + "\n")
        except Exception:
            pass

    active_tag = tag or color
    if not active_tag:
        if "✅" in msg or "🏆" in msg:
            active_tag = "success"
        elif "❌" in msg or "🛑" in msg:
            active_tag = "error"
        elif any(sym in msg for sym in ["📡", "🎯", "🚀", "📖", "🔬", "🔎"]):
            active_tag = "info"
        elif any(sym in msg for sym in ["⏳", "⚠️", "⚡"]):
            active_tag = "warning"

    _broadcast_sse({"type": "log", "message": msg, "tag": active_tag})

    if log_widget:
        try:
            def _insert():
                try:
                    log_widget.configure(state="normal")
                    if active_tag:
                        log_widget.insert("end", msg + "\n", active_tag)
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
#  CLEANING & TEXT PARSING
# ══════════════════════════════════════════════════════════════════════════════

_SUB_SUPER_MAP = str.maketrans({
    "₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4",
    "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9",
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
    "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
    "⁺": "+", "⁻": "-", "₋": "-", "₊": "+",
})

def clean_title(raw_title: str) -> str:
    """Unescape HTML entities, strip XML/HTML tags, normalize subscripts/superscripts, and collapse spaces."""
    if not raw_title:
        return "Untitled"
    t = re.sub(r'</?[a-zA-Z][a-zA-Z0-9:\-_]*[^>]*>', '', raw_title)
    t = html.unescape(html.unescape(t))
    t = t.translate(_SUB_SUPER_MAP)
    t = re.sub(r"\s+", " ", t).strip()
    return t or "Untitled"

def _reconstruct_abstract(inv_idx: dict[str, list[int]] | None) -> str:
    """Reconstruct human-readable text from OpenAlex inverted abstract index."""
    if not inv_idx or not isinstance(inv_idx, dict):
        return ""
    pos_map: list[tuple[int, str]] = []
    for word, positions in inv_idx.items():
        for pos in positions:
            pos_map.append((pos, word))
    pos_map.sort(key=lambda x: x[0])
    return " ".join(w for _, w in pos_map)

def _parse_author_name(name: str) -> tuple[str, str]:
    """Parse author name into (Last_Name, Initials)."""
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
    return ", ".join(out[:19]) + ", ... " + out[-1]

def _escape_bibtex(s: str) -> str:
    """Escape LaTeX-fragile characters in human text (title/journal/author/note)."""
    if not s:
        return ""
    # Backslash first so we don't double-escape the escapes we add below.
    s = s.replace("\\", r"\textbackslash{}")
    for char in ["{", "}", "%", "$", "&", "_", "#"]:
        s = s.replace(char, f"\\{char}")
    s = s.replace("~", r"\textasciitilde{}").replace("^", r"\textasciicircum{}")
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

def _format_bibtex_entry(p: Paper) -> str:
    """Format an individual Paper object into a complete, pristine BibTeX entry."""
    key = _bib_key(p, set())
    lines = [f"@article{{{key},"]
    if p.authors:
        lines.append(f"  author  = {{{' and '.join(_escape_bibtex(a) for a in p.authors)}}},")
    lines.append(f"  title   = {{{{{_escape_bibtex(clean_title(p.title))}}}}},")
    if p.journal:
        lines.append(f"  journal = {{{_escape_bibtex(p.journal)}}},")
    if p.year:
        lines.append(f"  year    = {{{p.year}}},")
    if p.doi:
        lines.append(f"  doi     = {{{p.doi}}},")
    if p.url:
        lines.append(f"  url     = {{{p.url}}},")
    note_parts = []
    if p.quartile:
        note_parts.append(p.quartile)
    if p.citations:
        note_parts.append(f"cited-by: {p.citations}")
    if p.relevance_score > 0:
        note_parts.append(f"relevance: {int(p.relevance_score * 100)}%")
    if note_parts:
        lines.append(f"  note    = {{{', '.join(note_parts)}}},")
    lines.append("}")
    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════════════════════
#  RELEVANCE ENGINE & DISCIPLINARY COLLISION GUARD (60% MINIMUM)
# ══════════════════════════════════════════════════════════════════════════════

RELEVANCE_STOPWORDS = {
    "a","an","the","and","or","of","in","for","to","with","on","at",
    "by","from","as","is","are","was","were","be","been","being",
    "this","that","these","those","its","it","we","our","their",
    "using","used","use","via","based","new","novel","study","studies",
    "recent","advances","review","research","paper","towards","toward",
    "journal","article","author","authors","vol","volume","issue",
    "comprehensive", "efficient", "efficiency", "high", "highly", "low",
    "performance", "enhanced", "improved", "improving", "effective", "facile",
    "direct", "rapid", "simple", "advanced", "modern", "general", "overview",
    "insight", "insights", "progress", "perspective", "perspectives", "role",
    "effect", "effects", "application", "applications", "approach", "approaches",
    "method", "methods", "analysis", "properties", "property", "characterization",
    "development", "design", "strategy", "strategies", "understanding",
    "future", "challenge", "challenges", "prospect", "prospects", "outlook",
    "trend", "trends", "opportunity", "opportunities", "current", "potential",
    # Academic meta/method words that should not dilute domain search
    "comparison", "comparisons", "comparative", "comparing", "compared",
    "versus", "vs", "investigation", "investigations", "investigating",
    "evaluation", "evaluations", "evaluating", "fabrication", "fabricating",
    "preparation", "preparing", "synthesis", "synthesizing",
    "experimental", "experiment", "experiments",
}

ALLOWED_SHORT_TOKENS = {
    "li", "zn", "na", "mg", "ca", "k", "al", "fe", "co", "ni", "cu", "mn", "v",
    "mo", "w", "pt", "pd", "au", "ag", "ru", "ir", "ti", "sn", "pb", "bi", "sb",
    "si", "ge", "b", "c", "n", "p", "s", "o", "f", "cl", "br", "i", "se", "te",
    "h2", "o2", "co2", "n2", "nh3", "ch4", "2d", "3d", "1d", "0d", "ai", "ml", "dft",
    "latp", "llzo", "lagp", "nasicon", "zab", "lib", "ssb", "assb", "oer", "orr", "her",
    "sei", "nmc", "ncm", "lfp", "lco", "peo", "pvdf", "pan", "pmma",
}

CHEMICAL_SYNONYMS = {
    # Common user typos in materials/battery search
    "menbranes": "membrane", "menbrane": "membrane",
    "electrolite": "electrolyte", "electrolites": "electrolyte",
    "seperator": "separator", "seperators": "separator",
    "cathod": "cathode", "anod": "anode",
    # Polymer types -> polymer
    "pvdf": "polymer", "peo": "polymer", "pan": "polymer", "pmma": "polymer",
    "ppc": "polymer", "pcl": "polymer", "ptfe": "polymer", "nafion": "polymer",
    "polymeric": "polymer", "polymers": "polymer",
    # Ceramic types -> ceramic
    "ceramics": "ceramic", "inorganic": "ceramic",
    # Separator / membrane synonyms
    "membranes": "membrane", "separator": "membrane", "separators": "membrane",
    "films": "membrane", "film": "membrane",
    # Elements & metals
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
    "ti": "titanium", "titanium": "titanium",
    "sn": "tin", "tin": "tin",
    "cu": "copper", "copper": "copper",
    "zabs": "zab", "libs": "lib", "ssbs": "ssb", "assbs": "assb",
}

ACRONYM_EXPANSIONS = {
    "latp": ["lithium", "aluminum", "titanium", "phosphate", "nasicon", "ceramic", "solid", "electrolyte"],
    "llzo": ["lithium", "lanthanum", "zirconate", "garnet", "ceramic", "solid", "electrolyte"],
    "lagp": ["lithium", "aluminum", "germanium", "phosphate", "nasicon", "ceramic", "solid", "electrolyte"],
    "nasicon": ["sodium", "super", "ionic", "conductor", "phosphate", "ceramic", "solid", "electrolyte"],
    "zab": ["zinc", "air", "battery"],
    "lib": ["lithium", "ion", "battery"],
    "ssb": ["solid", "state", "battery"],
    "assb": ["all", "solid", "state", "battery"],
    "pvdf": ["polyvinylidene", "fluoride", "polymer"],
    "peo": ["polyethylene", "oxide", "polymer"],
    "oer": ["oxygen", "evolution", "reaction"],
    "orr": ["oxygen", "reduction", "reaction"],
    "her": ["hydrogen", "evolution", "reaction"],
    "dft": ["density", "functional", "theory"],
    "sei": ["solid", "electrolyte", "interphase"],
}

NON_RESEARCH_PATTERNS = [
    r"^correction\s*:",
    r"^correction\s+to\b",
    r"^author\s+correction\b",
    r"^publisher\s+correction\b",
    r"^corrigendum\s*:",
    r"^corrigendum\s+to\b",
    r"^erratum\s*:",
    r"^erratum\s+to\b",
    r"^retraction\s*:",
    r"^retraction\s+to\b",
    r"^retraction\s+note\b",
    r"^reply\s+to\b",
    r"^response\s+to\b",
    r"^comment\s+on\b",
    r"^(?:[A-Z]{1,3}\d{2,5}[-\s]|\d{3,5}\s+|[A-Z]\d{3,4}\s+)[A-Z].*$",  # e.g. P040 Diagnostic..., MP30-18...
]

DISCIPLINARY_NEGATIVE_TERMS = {
    "prostate", "biopsy", "transperineal", "anaesthetic", "anesthetic", "anaesthesia", "anesthesia",
    "prostatic", "urology", "urological", "oncology", "oncological", "carcinoma", "tumor", "tumour",
    "prognostic", "patient", "patients", "clinical", "hospital", "pediatric", "paediatric", "surgical",
    "surgery", "antibiotic", "antibiotics", "mri", "mpmri", "pi-rads", "pathology", "histology",
    "diagnostic", "diagnosis", "therapy", "disease",
    "instructors", "teaching", "curriculum", "pedagogy", "lexical", "classroom",
}

TECHNICAL_DOMAINS = {
    "battery", "batteries", "electrolyte", "electrolytes", "nasicon", "latp", "llzo", "lagp",
    "anode", "cathode", "lithium", "zinc", "sodium", "catalyst", "electrocatalyst", "supercapacitor",
    "solar", "photovoltaic", "fuel cell", "perovskite", "graphene", "polymer", "solid state",
    "interphase", "sei", "conductivity", "impedance", "voltammetry", "dft", "oer", "orr",
}

def _stem(w: str) -> str:
    w = w.lower()
    if len(w) <= 3:
        return w
    for sfx, rep in [
        ("electrocatalysis", "electrocataly"), ("electrocatalysts", "electrocataly"),
        ("electrocatalyst", "electrocataly"), ("electrocatalytic", "electrocataly"),
        ("electrochemical", "electrochem"), ("electrochemistry", "electrochem"),
        ("catalysis", "cataly"), ("catalysts", "cataly"), ("catalyst", "cataly"),
        ("catalytic", "cataly"), ("batteries", "batter"), ("battery", "batter"),
        ("electrolytes", "electrolyt"), ("electrolyte", "electrolyt"),
        ("synthesis", "synthe"), ("synthetic", "synthe"),
        ("synthesizing", "synthe"), ("synthesized", "synthe"),
    ]:
        if w == sfx or w.endswith(sfx):
            return w[:-len(sfx)] + rep
    if w.endswith("ization") or w.endswith("isation"): return w[:-7]
    if w.endswith("ations"): return w[:-5]
    if w.endswith("ation"): return w[:-4]
    if w.endswith("ingly"): return w[:-5]
    if w.endswith("ities"): return w[:-4] + "y"
    if w.endswith("ity"): return w[:-3]
    if w.endswith("ment"): return w[:-4]
    if w.endswith("ness"): return w[:-4]
    if w.endswith("able") or w.endswith("ible"): return w[:-4]
    if w.endswith("ing") and len(w) > 5: return w[:-3]
    if w.endswith("ed") and len(w) > 4: return w[:-2]
    if w.endswith("ies") and len(w) > 4: return w[:-3] + "y"
    if w.endswith(("ses", "xes", "zes", "ches", "shes")): return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3: return w[:-1]
    return w

def _clean_tokens(s: str) -> set[str]:
    raw = [w.lower() for w in re.split(r"\W+", clean_title(s) or "") if w]
    tokens = set()
    for w in raw:
        if w in RELEVANCE_STOPWORDS:
            continue
        canon = CHEMICAL_SYNONYMS.get(w, w)
        if len(canon) >= 2 or canon in ALLOWED_SHORT_TOKENS:
            tokens.add(_stem(canon))
    return tokens

def check_paper_relevance(
    title: str,
    abstract: str = "",
    keywords: str = "",
    focus: str = "",
    min_ratio: float = 0.60,
    concepts: list[str] | None = None,
) -> tuple[bool, float, str]:
    """Check paper relevance with 60% minimum match & disciplinary collision prevention."""
    c_title = clean_title(title)
    c_abstract = (abstract or "").strip()
    c_title_lower = c_title.lower()

    # 1. Non-research / Errata / Meeting abstract filter
    for pat in NON_RESEARCH_PATTERNS:
        if re.search(pat, c_title_lower):
            return False, 0.0, f"Filtered: non-research or erratum ('{pat}')"

    # 2. Clean keywords and focus tokens
    kw_raw = (keywords or "").strip()
    focus_raw = (focus or "").strip()

    # If focus is identical to keywords, disregard focus to avoid duplicate penalty
    if focus_raw.lower() == kw_raw.lower():
        focus_raw = ""

    kw_tokens = _clean_tokens(kw_raw)
    focus_tokens = _clean_tokens(focus_raw) if focus_raw else set()
    combined_query_tokens = kw_tokens | focus_tokens

    if not combined_query_tokens:
        return True, 1.0, "Empty query"

    # Inspect disciplinary context across raw query terms
    all_query_str = f"{kw_raw} {focus_raw}".strip()
    raw_query_words = [w.lower() for w in re.split(r"\W+", all_query_str) if w]
    query_has_medical = any(w in DISCIPLINARY_NEGATIVE_TERMS for w in raw_query_words)
    query_is_technical = any(w in TECHNICAL_DOMAINS for w in raw_query_words) or any(
        t in ALLOWED_SHORT_TOKENS for t in combined_query_tokens
    )

    # 3. Disciplinary Collision Guard
    # If the user query is technical/battery/chemistry and NOT medical, reject medical/teaching papers
    if query_is_technical and not query_has_medical:
        title_words = set(re.findall(r"[a-z]+", c_title_lower))
        colls = title_words & DISCIPLINARY_NEGATIVE_TERMS
        if colls:
            return False, 0.0, f"Filtered: cross-disciplinary collision ({', '.join(colls)})"

        if concepts:
            med_concepts = {"Medicine", "Internal medicine", "Urology", "Oncology", "Pathology", "Surgery"}
            if any(c in med_concepts for c in concepts) and not any(
                c in {"Materials science", "Chemistry", "Chemical engineering", "Physics"} for c in concepts
            ):
                return False, 0.0, "Filtered: medical discipline concept"

    # 4. Token extraction from paper
    title_tokens = _clean_tokens(c_title)
    abstract_tokens = _clean_tokens(c_abstract) if c_abstract else set()
    paper_tokens = title_tokens | abstract_tokens

    # Helper to calculate match score for a token set
    def _score_tokens(target_tokens: set[str]) -> tuple[float, float]:
        if not target_tokens:
            return 1.0, 1.0
        matched = target_tokens & paper_tokens
        base_r = len(matched) / len(target_tokens)
        weighted = sum(
            1.0 if tok in title_tokens else (0.6 if tok in abstract_tokens else 0.0)
            for tok in target_tokens
        )
        sc = weighted / len(target_tokens)
        return sc, base_r

    # Acronym Concept Expansion Support
    expanded_support = set()
    for w in raw_query_words:
        if w in ACRONYM_EXPANSIONS:
            expanded_support.update([_stem(x) for x in ACRONYM_EXPANSIONS[w]])

    # Exact phrase in title
    flat_title = re.sub(r"\W+", " ", c_title_lower).strip()
    flat_kw = re.sub(r"\W+", " ", kw_raw.lower()).strip()
    exact_phrase_in_title = bool(flat_kw and flat_kw in flat_title)

    # Acronym expansion match bonus
    acronym_bonus = 0.0
    if expanded_support and (expanded_support & paper_tokens):
        acronym_bonus = min(0.3, len(expanded_support & paper_tokens) * 0.1)

    # Score calculation
    kw_sc, kw_base = _score_tokens(kw_tokens) if kw_tokens else (0.0, 0.0)
    focus_sc, focus_base = _score_tokens(focus_tokens) if focus_tokens else (0.0, 0.0)
    combined_sc, combined_base = _score_tokens(combined_query_tokens)

    if kw_tokens and focus_tokens:
        # A paper is relevant if it strongly matches keywords, OR matches focus, OR has a high combined match
        primary_score = max(kw_sc, combined_sc, 0.4 * kw_sc + 0.6 * focus_sc)
        base_ratio = max(kw_base, combined_base)
        # Bonus for comparison papers matching BOTH keyword & focus sets!
        if (kw_tokens & paper_tokens) and (focus_tokens & paper_tokens):
            primary_score = min(1.0, primary_score + 0.15)
    elif kw_tokens:
        primary_score = kw_sc
        base_ratio = kw_base
    else:
        primary_score = focus_sc
        base_ratio = focus_base

    if exact_phrase_in_title:
        primary_score = max(primary_score, 1.0 if len(combined_query_tokens) <= 3 else primary_score + 0.2)

    primary_score = min(1.0, max(0.0, primary_score + acronym_bonus))

    is_rel = (primary_score >= min_ratio) and (base_ratio >= (min_ratio * 0.75))
    return is_rel, primary_score, "Relevant" if is_rel else f"Score {primary_score:.2f} < {min_ratio:.2f}"

def _title_is_relevant(title: str, keywords: str, min_ratio: float = 0.60) -> bool:
    """Backward-compatible wrapper for title-only relevance checking."""
    is_rel, _, _ = check_paper_relevance(title=title, abstract="", keywords=keywords, min_ratio=min_ratio)
    return is_rel

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
        if all(w.lower() in RELEVANCE_STOPWORDS for w in ws):
            return False
        if len(ws) == 1:
            w = ws[0].lower()
            if w in RELEVANCE_STOPWORDS:
                return False
            # ONLY allow single-word phrases if it is a specific recognized acronym or element
            return w in ACRONYM_EXPANSIONS or w in ALLOWED_SHORT_TOKENS
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
    
    cleaned_keywords = []
    for p in keywords:
        clean_p = " ".join([CHEMICAL_SYNONYMS.get(w.lower(), w) for w in p.split()])
        cleaned_keywords.append(clean_p)
    return cleaned_keywords or [text]

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

def jitter(lo: float = 0.2, hi: float = 0.5):
    time.sleep(random.uniform(lo, hi))

def normalise_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")

def _is_real_http_url(url: str) -> bool:
    return bool(url) and url.startswith(("http://", "https://"))

def _extract_doi(text: str) -> str:
    m = re.search(r'10\.\d{4,9}/[-._;()/:A-Za-z0-9]+', text)
    if m:
        d = m.group(0).rstrip('.,;)]}>"')
        d = re.sub(r'\.(?:pdf|html|htm|epub)$', '', d, flags=re.I)
        return d
    return ""

def open_stealth_session(profile: str = "chrome120"):
    if HAS_CFFI:
        return cffi_requests.Session(impersonate=profile)
    s = requests.Session()
    s.headers.update(api_headers())
    return s

API_TIMEOUT = 10  # Fast failover timeout for metadata harvest APIs

def safe_get(url: str, ctx: DownloadContext | None = None, **kwargs) -> requests.Response | None:
    req_headers = api_headers()
    if "headers" in kwargs:
        req_headers.update(kwargs.pop("headers"))
    timeout_val = kwargs.pop("timeout", API_TIMEOUT)
    max_retries = kwargs.pop("retries", 2)

    for attempt in range(1, max_retries + 1):
        if is_cancelled(ctx):
            return None
        try:
            r = requests.get(url, headers=req_headers,
                             timeout=timeout_val,
                             proxies=rand_proxy(), **kwargs)
            if r.status_code == 429:
                retry_header = r.headers.get("Retry-After")
                if retry_header and retry_header.isdigit():
                    wait = min(float(retry_header), 10.0)
                else:
                    wait = min(2 ** attempt + random.uniform(0.5, 1.5), 6.0)
                _log(f"    ⏳ Rate limited (429). Waiting {wait:.1f}s...")
                if sleep_check_cancel(wait, ctx):
                    return None
                continue
            return r
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            _log(f"    ⏱ {type(e).__name__} (attempt {attempt}/{max_retries}): {url[:60]}")
            if attempt < max_retries:
                if sleep_check_cancel(1.0, ctx):
                    return None
        except Exception as e:
            _log(f"    ⚠️  Request error: {e}")
            break
    return None

# ══════════════════════════════════════════════════════════════════════════════
#  SCIMAGO JOURNAL QUARTILE RANKING (ISSN + TITLE + ABBREVIATION DUAL INDEX)
# ══════════════════════════════════════════════════════════════════════════════

_scimago_issn_map: dict[str, str] | None = None
_scimago_title_map: dict[str, str] | None = None
_scimago_lock = threading.Lock()

JOURNAL_ABBREV_MAP = {
    "j": "journal", "appl": "applied", "mater": "materials", "chem": "chemistry",
    "phys": "physics", "eng": "engineering", "int": "international", "lett": "letters",
    "rev": "review", "adv": "advanced", "soc": "society", "am": "american",
    "nat": "nature", "commun": "communications", "electrochim": "electrochimica",
    "acta": "acta", "sol": "solid", "state": "state", "ion": "ionics",
    "sci": "science", "technol": "technology", "res": "research", "energ": "energy",
    "acs": "acs", "rsc": "rsc", "ieee": "ieee", "iop": "iop",
    "ann": "annual", "rep": "reports", "proc": "proceedings", "trans": "transactions",
    "mol": "molecular", "biol": "biological", "biophys": "biophysical", "biochem": "biochemical",
    "nanotechnol": "nanotechnology", "nano": "nano", "micro": "micro", "spectrosc": "spectroscopy",
    "struct": "structural", "environ": "environmental", "sust": "sustainable", "renew": "renewable",
    "comput": "computational", "theor": "theoretical", "exper": "experimental",
    "catal": "catalysis", "polym": "polymer", "macromol": "macromolecular",
    "inorg": "inorganic", "org": "organic", "anal": "analytical", "colloid": "colloids",
    "interf": "interface", "interfaces": "interfaces", "part": "part", "syst": "systems",
    "electr": "electronic", "power": "power", "storage": "storage", "sources": "sources",
    "gener": "generation", "front": "frontiers", "curr": "current", "opin": "opinion",
    "trends": "trends", "perspect": "perspectives", "nanoscale": "nanoscale",
}

def _norm_issn(s: str) -> str:
    return re.sub(r"[^0-9Xx]", "", s or "").upper().zfill(8)

JOURNAL_CONNECTOR_STOPWORDS = {"and", "of", "the", "for", "in", "on", "to", "with", "a", "an"}

def _norm_journal_variants(name: str) -> list[str]:
    raw_input = (name or "").strip()
    if not raw_input:
        return []
    candidates = [raw_input]
    if ":" in raw_input:
        candidates.append(raw_input.split(":", 1)[0].strip())
    if " - " in raw_input:
        candidates.append(raw_input.split(" - ", 1)[0].strip())
    if " – " in raw_input:
        candidates.append(raw_input.split(" – ", 1)[0].strip())

    variants: list[str] = []
    for cand in candidates:
        raw = re.sub(r"\W+", " ", cand.lower()).strip()
        if not raw:
            continue
        if raw not in variants:
            variants.append(raw)
        tokens = raw.split()
        expanded = [JOURNAL_ABBREV_MAP.get(t, t) for t in tokens]
        exp_str = " ".join(expanded)
        if exp_str not in variants:
            variants.append(exp_str)
        no_stop = [t for t in expanded if t not in JOURNAL_CONNECTOR_STOPWORDS]
        no_stop_str = " ".join(no_stop)
        if no_stop_str and no_stop_str not in variants:
            variants.append(no_stop_str)
    return variants


def load_scimago_quartiles() -> tuple[dict[str, str], dict[str, str]]:
    """Load Scimago quartiles indexed by ISSN and Journal Title (with abbreviation expansion)."""
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
                    # 2. Index Journal Title & variants
                    for var in _norm_journal_variants(row.get("Title") or ""):
                        if var and (var not in title_map or q < title_map[var]):
                            title_map[var] = q
            except Exception as e:
                _log(f"  ⚠️  Scimago parse error: {e}")

        _scimago_issn_map = issn_map
        _scimago_title_map = title_map
        if issn_map or title_map:
            _log(f"  🗂  Scimago loaded: {len(issn_map):,} ISSNs and {len(title_map):,} Journal Titles")
        return _scimago_issn_map, _scimago_title_map

def quartile_for(issns: list[str], journal: str = "") -> str:
    global _scimago_issn_map, _scimago_title_map
    if _scimago_issn_map is None or _scimago_title_map is None:
        load_scimago_quartiles()
    issn_map, title_map = _scimago_issn_map or {}, _scimago_title_map or {}
    # Primary: check ISSN
    for iss in issns:
        ni = _norm_issn(iss)
        if ni in issn_map:
            return issn_map[ni]
    # Fallback: check normalized journal title and variants
    if journal:
        for var in _norm_journal_variants(journal):
            if var in title_map:
                return title_map[var]
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
def harvest_openalex(keywords: str, y1: str, y2: str, max_res: int = 250, ctx=None,
                     focus: str = "", min_relevance: float = 0.60) -> list[Paper]:
    found: list[Paper] = []
    _log("  📖 OpenAlex: searching works…")
    base = "https://api.openalex.org/works"
    cursor = "*"
    per_page = min(200, max_res)
    clean_kw = re.sub(r"[,|:]+", " ", keywords).strip()
    page = 0

    while len(found) < max_res and not is_cancelled(ctx) and page < 3:
        page += 1
        params = {
            "filter": f"title_and_abstract.search:{clean_kw},from_publication_date:{y1}-01-01,to_publication_date:{y2}-12-31",
            "per-page": per_page,
            "cursor": cursor,
            "select": "id,doi,title,authorships,publication_year,cited_by_count,primary_location,best_oa_location,locations,open_access,abstract_inverted_index,concepts,type",
            "mailto": "chkam.dev@gmail.com",
        }
        r = safe_get(base, ctx=ctx, params=params, headers={"User-Agent": "ResearchLiteratureHarvester/10.0 (mailto:chkam.dev@gmail.com)"})
        if r is None or r.status_code != 200:
            break
        data = r.json()
        results = data.get("results", [])
        if not results:
            break

        for item in results:
            item_type = str(item.get("type") or "").lower()
            if item_type in ("erratum", "editorial", "letter", "paratext"):
                continue

            title = clean_title(item.get("title") or "")
            abstract = _reconstruct_abstract(item.get("abstract_inverted_index"))
            concepts = [c.get("display_name") for c in item.get("concepts", []) if c.get("score", 0) > 0.35]

            is_rel, rel_score, reason = check_paper_relevance(
                title=title, abstract=abstract, keywords=keywords, focus=focus,
                min_ratio=min_relevance, concepts=concepts
            )
            if not is_rel:
                continue

            doi = _extract_doi(item.get("doi") or "")
            best = item.get("best_oa_location") or {}
            pl = item.get("primary_location") or {}
            oa = item.get("open_access") or {}

            cand_urls = []
            for loc in item.get("locations") or []:
                pdf_u = loc.get("pdf_url")
                land_u = loc.get("landing_page_url")
                if pdf_u and _is_real_http_url(pdf_u) and pdf_u not in cand_urls:
                    cand_urls.append(pdf_u)
                if land_u and _is_real_http_url(land_u) and land_u not in cand_urls:
                    cand_urls.append(land_u)

            url = (best.get("pdf_url") or pl.get("pdf_url") or oa.get("oa_url")
                   or (cand_urls[0] if cand_urls else "")
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
                      journal=journal, issns=issns, citations=cits, abstract=abstract,
                      concepts=concepts, candidate_urls=cand_urls, relevance_score=rel_score,
                      source="OpenAlex")
            add_paper_candidate(found, p)

        cursor = data.get("meta", {}).get("next_cursor")
        if not cursor or len(results) < per_page:
            break
        jitter(0.2, 0.4)

    _log(f"  ✅ OpenAlex harvested {len(found)} candidates")
    return found

# ── 2. Crossref ───────────────────────────────────────────────────────────────
def harvest_crossref(keywords: str, y1: str, y2: str, max_res: int = 250, ctx=None,
                     focus: str = "", min_relevance: float = 0.60) -> list[Paper]:
    found: list[Paper] = []
    _log("  🔗 Crossref: searching journal literature…")
    base = "https://api.crossref.org/works"
    cursor = "*"
    rows = min(100, max_res)
    year_filter = f"from-pub-date:{y1}-01-01,until-pub-date:{y2}-12-31,type:journal-article"
    page = 0

    while len(found) < max_res and not is_cancelled(ctx) and page < 2:
        page += 1
        params = {
            "query.bibliographic": keywords,
            "filter": year_filter,
            "rows": rows,
            "cursor": cursor,
            "select": "DOI,title,author,container-title,ISSN,published,published-print,published-online,is-referenced-by-count,link,resource,type",
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
            is_rel, rel_score, _ = check_paper_relevance(title=title, abstract="", keywords=keywords,
                                                        focus=focus, min_ratio=min_relevance)
            if not is_rel:
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

            cand_urls = []
            for l in item.get("link", []):
                u = l.get("URL", "")
                if u and _is_real_http_url(u) and u not in cand_urls:
                    cand_urls.append(u)

            url = cand_urls[0] if cand_urls else (f"https://doi.org/{doi}" if doi else "")
            if not _is_real_http_url(url):
                continue

            p = Paper(url=url, title=title, doi=doi, authors=authors, year=year,
                      journal=journal, issns=issns, citations=cits, candidate_urls=cand_urls,
                      relevance_score=rel_score, source="Crossref")
            add_paper_candidate(found, p)

        cursor = msg.get("next-cursor")
        if not cursor or len(items) < rows:
            break
        jitter(0.2, 0.4)

    _log(f"  ✅ Crossref harvested {len(found)} candidates")
    return found

# ── 3. Europe PMC ─────────────────────────────────────────────────────────────
def harvest_europepmc(keywords: str, y1: str, y2: str, max_res: int = 150, ctx=None,
                      focus: str = "", min_relevance: float = 0.60) -> list[Paper]:
    found: list[Paper] = []
    _log("  🧬 Europe PMC: searching full-text papers…")
    base = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    page_size = min(100, max_res)
    cursor = "*"
    query = f"({keywords}) AND (PUB_YEAR:[{y1} TO {y2}]) AND (HAS_FT:Y OR OPEN_ACCESS:y)"
    page = 0

    while len(found) < max_res and not is_cancelled(ctx) and page < 2:
        page += 1
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
            pub_type = str(item.get("pubType") or "").lower()
            if "correction" in pub_type or "retraction" in pub_type:
                continue

            title = clean_title(item.get("title") or "")
            abstract = item.get("abstractText") or ""
            is_rel, rel_score, _ = check_paper_relevance(title=title, abstract=abstract, keywords=keywords,
                                                        focus=focus, min_ratio=min_relevance)
            if not is_rel:
                continue

            doi = item.get("doi") or ""
            pmcid = item.get("pmcid") or ""

            cand_urls = []
            for u in item.get("fullTextUrlList", {}).get("fullTextUrl", []):
                cand_u = u.get("url", "")
                if cand_u and _is_real_http_url(cand_u) and cand_u not in cand_urls:
                    cand_urls.append(cand_u)

            if pmcid:
                pmc_pdf = f"https://europepmc.org/backend/ptpmcrender.fcgi?accid={pmcid}&blobtype=pdf"
                if pmc_pdf not in cand_urls:
                    cand_urls.insert(0, pmc_pdf)

            url = cand_urls[0] if cand_urls else (f"https://doi.org/{doi}" if doi else "")
            if not _is_real_http_url(url):
                continue

            authors = [a.strip() for a in (item.get("authorString") or "").split(",") if a.strip()]
            journal = item.get("journalTitle") or (item.get("journalInfo") or {}).get("journal", {}).get("title") or ""
            issn = item.get("journalIssn") or ""
            issns = [issn] if issn else []
            year = str(item.get("pubYear") or "")
            cits = item.get("citedByCount") or 0

            p = Paper(url=url, title=title, doi=doi, authors=authors, year=year,
                      journal=journal, issns=issns, citations=cits, pmcid=pmcid,
                      abstract=abstract, candidate_urls=cand_urls, relevance_score=rel_score,
                      source="EuropePMC")
            add_paper_candidate(found, p)

        cursor = r.json().get("nextCursorMark", "")
        if not cursor or cursor == "*" or len(results) < page_size:
            break
        jitter(0.2, 0.4)

    _log(f"  ✅ Europe PMC harvested {len(found)} candidates")
    return found

# ── 4. PubMed / NCBI E-Utilities ──────────────────────────────────────────────
def harvest_pubmed(keywords: str, y1: str, y2: str, max_res: int = 150, ctx=None,
                   focus: str = "", min_relevance: float = 0.60) -> list[Paper]:
    found: list[Paper] = []
    _log("  🏥 PubMed / PMC: searching biomedical & materials repository…")
    base_search = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    base_summary = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

    query = f"({keywords}) AND ({y1}/01/01[pdat] : {y2}/12/31[pdat])"
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
            is_rel, rel_score, _ = check_paper_relevance(title=title, abstract="", keywords=keywords,
                                                        focus=focus, min_ratio=min_relevance)
            if not is_rel:
                continue

            doi = _extract_doi(item.get("doi") or "") or item.get("doi") or ""
            authors = [a.get("name") for a in item.get("authors", []) if a.get("name")]
            journal = item.get("source") or item.get("fulljournalname") or ""
            pubdate = item.get("pubdate") or ""
            year = str(pubdate[:4]) if pubdate else ""

            pmcid = f"PMC{pid}"
            pdf_url = f"https://europepmc.org/backend/ptpmcrender.fcgi?accid={pmcid}&blobtype=pdf"
            cand_urls = [pdf_url, f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/"]

            p = Paper(url=pdf_url, title=title, doi=doi, authors=authors, year=year,
                      journal=journal, pmcid=pmcid, candidate_urls=cand_urls,
                      relevance_score=rel_score, source="PubMed/PMC")
            add_paper_candidate(found, p)
        jitter(0.2, 0.4)

    _log(f"  ✅ PubMed/PMC harvested {len(found)} candidates")
    return found

# ── 5. Semantic Scholar ───────────────────────────────────────────────────────
def harvest_semantic_scholar(keywords: str, y1: str, y2: str, max_res: int = 150, ctx=None,
                             focus: str = "", min_relevance: float = 0.60) -> list[Paper]:
    found: list[Paper] = []
    _log("  🔬 Semantic Scholar: searching open-access papers…")
    base = "https://api.semanticscholar.org/graph/v1/paper/search"
    limit = min(50, max_res)

    try:
        r = requests.get(base, headers=api_headers(), params={
            "query": keywords,
            "fields": "title,openAccessPdf,year,authors,venue,citationCount,externalIds,abstract",
            "limit": limit, "offset": 0, "year": f"{y1}-{y2}",
        }, timeout=15)
        if r.status_code == 429:
            _log("    ⏳ Semantic Scholar rate-limited; continuing with remaining harvesters.")
            return found
        if r.status_code != 200:
            return found
        items = r.json().get("data", [])
    except Exception:
        return found

    for paper in items:
        title = clean_title(paper.get("title") or "")
        abstract = paper.get("abstract") or ""
        is_rel, rel_score, _ = check_paper_relevance(title=title, abstract=abstract, keywords=keywords,
                                                    focus=focus, min_ratio=min_relevance)
        if not is_rel:
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
                  journal=journal, citations=cits, abstract=abstract,
                  relevance_score=rel_score, source="SemanticScholar")
        add_paper_candidate(found, p)

    _log(f"  ✅ Semantic Scholar harvested {len(found)} candidates")
    return found

# ── 6. DOAJ ───────────────────────────────────────────────────────────────────
def harvest_doaj(keywords: str, y1: str, y2: str, max_res: int = 100, ctx=None,
                 focus: str = "", min_relevance: float = 0.60) -> list[Paper]:
    found: list[Paper] = []
    _log("  📚 DOAJ: searching open-access journals…")
    base = "https://doaj.org/api/search/articles"
    page = 1
    size = min(100, max_res)

    while len(found) < max_res and not is_cancelled(ctx) and page <= 3:
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
            abstract = bibjson.get("abstract") or ""
            is_rel, rel_score, _ = check_paper_relevance(title=title, abstract=abstract, keywords=keywords,
                                                        focus=focus, min_ratio=min_relevance)
            if not is_rel:
                continue

            year = str(bibjson.get("year") or "")
            if year.isdigit() and not (int(y1) <= int(year) <= int(y2)):
                continue

            cand_urls = []
            for link in bibjson.get("link", []):
                u = link.get("url", "")
                if u and _is_real_http_url(u) and u not in cand_urls:
                    cand_urls.append(u)

            doi = ""
            for ident in bibjson.get("identifier", []):
                if ident.get("type") == "doi":
                    doi = ident.get("id", "")
                    break
            url = cand_urls[0] if cand_urls else (f"https://doi.org/{doi}" if doi else "")
            if not _is_real_http_url(url):
                continue

            authors = [a.get("name") for a in bibjson.get("author", []) if a.get("name")]
            journal_obj = bibjson.get("journal", {})
            journal = journal_obj.get("title") or ""
            issns = list(journal_obj.get("issns") or [])

            p = Paper(url=url, title=title, doi=doi, authors=authors, year=year,
                      journal=journal, issns=issns, abstract=abstract, candidate_urls=cand_urls,
                      relevance_score=rel_score, source="DOAJ")
            add_paper_candidate(found, p)

        if len(results) < size:
            break
        page += 1
        jitter(0.2, 0.4)

    _log(f"  ✅ DOAJ harvested {len(found)} candidates")
    return found

# ── 7. arXiv ──────────────────────────────────────────────────────────────────
def harvest_arxiv(keywords: str, y1: str, y2: str, max_res: int = 150, ctx=None,
                  focus: str = "", min_relevance: float = 0.60) -> list[Paper]:
    found: list[Paper] = []
    _log("  📄 arXiv: searching STEM preprints…")
    base = "https://export.arxiv.org/api/query"
    batch = min(150, max_res)
    start = 0
    page = 0
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    while len(found) < max_res and not is_cancelled(ctx) and page < 3:
        page += 1
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
            abstract = entry.findtext("atom:summary", "", ns) or ""
            is_rel, rel_score, _ = check_paper_relevance(title=title, abstract=abstract, keywords=keywords,
                                                        focus=focus, min_ratio=min_relevance)
            if not is_rel:
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

            p = Paper(url=pdf_url, title=title, doi=doi, authors=authors, year=pub_raw,
                      journal="arXiv preprint", abstract=abstract, candidate_urls=[pdf_url],
                      relevance_score=rel_score, source="arXiv")
            add_paper_candidate(found, p)

        if len(entries) < batch:
            break
        start += batch
        jitter(0.4, 0.8)

    _log(f"  ✅ arXiv harvested {len(found)} candidates")
    return found

# ── 8. CORE.ac.uk ─────────────────────────────────────────────────────────────
def harvest_core(keywords: str, y1: str, y2: str, max_res: int = 100, ctx=None,
                 focus: str = "", min_relevance: float = 0.60) -> list[Paper]:
    found: list[Paper] = []
    _log("  🌐 CORE.ac.uk: searching repository works…")
    base = "https://api.core.ac.uk/v3/search/works"
    size = min(100, max_res)

    try:
        r = requests.get(base, headers=api_headers(), params={
            "q": f"{keywords} year:[{y1} TO {y2}]",
            "limit": size, "offset": 0, "exclude": "fullText",
        }, timeout=12)
        if r.status_code in (401, 403, 429):
            _log("    ⏳ CORE API busy or unauthenticated; continuing.")
            return found
        if r.status_code != 200:
            return found
        results = r.json().get("results", [])
    except Exception:
        return found

    for item in results:
        title = clean_title(item.get("title") or "")
        abstract = item.get("abstract") or ""
        is_rel, rel_score, _ = check_paper_relevance(title=title, abstract=abstract, keywords=keywords,
                                                    focus=focus, min_ratio=min_relevance)
        if not is_rel:
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
                  journal=journal, abstract=abstract, candidate_urls=urls,
                  relevance_score=rel_score, source="CORE")
        add_paper_candidate(found, p)

    _log(f"  ✅ CORE harvested {len(found)} candidates")
    return found

# ── 9. BASE ───────────────────────────────────────────────────────────────────
def harvest_base(keywords: str, y1: str, y2: str, max_res: int = 100, ctx=None,
                 focus: str = "", min_relevance: float = 0.60) -> list[Paper]:
    found: list[Paper] = []
    _log("  🅱️  BASE: searching academic documents…")
    base = "https://api.base-search.net/cgi-bin/BaseHttpSearchInterface.fcgi"
    hits = min(100, max_res)

    try:
        r = requests.get(base, headers=api_headers(), params={
            "func": "PerformSearch", "query": keywords, "format": "json",
            "hits": hits, "offset": 0,
        }, timeout=12)
        if r.status_code != 200:
            return found
        docs = (r.json().get("response", {}) or {}).get("docs", [])
    except Exception:
        return found

    for d in docs:
        title = clean_title(d.get("dctitle") or "")
        is_rel, rel_score, _ = check_paper_relevance(title=title, abstract="", keywords=keywords,
                                                    focus=focus, min_ratio=min_relevance)
        if not is_rel:
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
                  journal=journal, relevance_score=rel_score, source="BASE")
        add_paper_candidate(found, p)

    _log(f"  ✅ BASE harvested {len(found)} candidates")
    return found

# ══════════════════════════════════════════════════════════════════════════════
#  METADATA ENRICHMENT VIA OPENALEX BATCHES
# ══════════════════════════════════════════════════════════════════════════════

def enrich_with_openalex(papers: list[Paper], ctx: DownloadContext | None = None) -> list[Paper]:
    """Batch-fill metadata, abstracts, and candidate URLs for papers missing data."""
    to_enrich: list[Paper] = [p for p in papers if p.clean_doi() and (not p.issns or not p.citations or not p.candidate_urls)]
    if not to_enrich:
        return papers

    _log(f"  🔎 Enriching metadata for {len(to_enrich)} DOIs via OpenAlex…")
    by_doi = {p.clean_doi().lower(): p for p in to_enrich}
    dois = list(by_doi.keys())

    failed_batches = 0
    for i in range(0, len(dois), 50):
        if is_cancelled(ctx) or failed_batches >= 2:
            break
        batch = dois[i:i + 50]
        r = safe_get("https://api.openalex.org/works", ctx=ctx, params={
            "filter": "doi:" + "|".join(batch),
            "per-page": 50,
            "select": "doi,title,authorships,publication_year,cited_by_count,primary_location,locations,abstract_inverted_index,concepts",
            "mailto": "chkam.dev@gmail.com",
        })
        if r is None or r.status_code != 200:
            failed_batches += 1
            continue
        failed_batches = 0

        try:
            batch_results = r.json().get("results", [])
        except Exception:
            failed_batches += 1
            continue
        for item in batch_results:
            d = (_extract_doi(item.get("doi") or "") or "").lower()
            p = by_doi.get(d)
            if not p:
                continue
            p.citations = max(p.citations, item.get("cited_by_count") or 0)
            if not p.year:
                p.year = str(item.get("publication_year") or "")
            if not p.abstract:
                p.abstract = _reconstruct_abstract(item.get("abstract_inverted_index"))
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

            # Enrich candidate URLs
            for loc in item.get("locations") or []:
                pu = loc.get("pdf_url")
                lu = loc.get("landing_page_url")
                if pu and _is_real_http_url(pu) and pu not in p.candidate_urls:
                    p.candidate_urls.append(pu)
                if lu and _is_real_http_url(lu) and lu not in p.candidate_urls:
                    p.candidate_urls.append(lu)

        jitter(0.2, 0.4)
    return papers

# ══════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD ENGINE (STEALTH, PUBLISHER SCRAPERS, UNPAYWALL, SCI-HUB)
# ══════════════════════════════════════════════════════════════════════════════

# Tested responsive Sci-Hub mirrors
SCIHUB_MIRRORS = [
    "https://sci-hub.st",
    "https://sci-hub.ru",
    "https://sci-hub.ren",
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
    "zenodo.org", "hal.science", "osf.io",
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
        s += 120
    u = (url or "").lower()
    if any(h in u for h in _OA_REPO_HINTS):
        s += 50
    if _is_paywall_landing(url):
        s -= 100
    return s

def _synthesize_direct_pdf_urls(url: str, doi: str) -> list[str]:
    """Derive direct PDF endpoints for major academic open-access publishing platforms."""
    syn: list[str] = []
    u = (url or "").strip()
    d = (doi or "").strip()

    # MDPI
    if d.startswith("10.3390/") or "mdpi.com" in u:
        syn.append(f"https://www.mdpi.com/{d}/pdf")
        if u and not u.endswith("/pdf") and "mdpi.com" in u:
            syn.append(u.rstrip("/") + "/pdf")

    # Frontiers
    if d.startswith("10.3389/") or "frontiersin.org" in u:
        if d:
            syn.append(f"https://www.frontiersin.org/articles/{d}/pdf")
        if "frontiersin.org" in u:
            if u.endswith(("/full", "/abstract")):
                syn.append(re.sub(r"/(?:full|abstract)$", "/pdf", u))
            elif not u.endswith("/pdf"):
                syn.append(u.rstrip("/") + "/pdf")

    # Springer / Nature
    if d.startswith("10.1007/"):
        syn.append(f"https://link.springer.com/content/pdf/{d}.pdf")
    elif "link.springer.com/article/" in u and d:
        syn.append(f"https://link.springer.com/content/pdf/{d}.pdf")

    if d.startswith("10.1038/"):
        art_id = d.split("/")[-1]
        syn.append(f"https://www.nature.com/articles/{art_id}.pdf")
    elif "nature.com/articles/" in u:
        syn.append(u.split("?")[0] + ".pdf")

    # Wiley
    if d.startswith("10.1002/"):
        syn.append(f"https://onlinelibrary.wiley.com/doi/pdfdirect/{d}")
    elif "onlinelibrary.wiley.com/doi/" in u:
        syn.append(u.replace("/doi/abs/", "/doi/pdfdirect/").replace("/doi/full/", "/doi/pdfdirect/").replace("/doi/epdf/", "/doi/pdfdirect/"))

    # IOPscience
    if d.startswith("10.1088/"):
        syn.append(f"https://iopscience.iop.org/article/{d}/pdf")

    # RSC (Royal Society of Chemistry)
    if d.startswith("10.1039/"):
        art_id = d.split("/")[-1]
        syn.append(f"https://pubs.rsc.org/en/content/articlepdf/{art_id}")

    # PubMed Central / Europe PMC
    if "pmc" in u.lower() or "europepmc" in u.lower():
        m = re.search(r"PMC\d+", u, re.I)
        if m:
            pmc_id = m.group(0).upper()
            syn.append(f"https://europepmc.org/backend/ptpmcrender.fcgi?accid={pmc_id}&blobtype=pdf")
            syn.append(f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_id}/pdf/")

    if "europepmc.org/article/med/" in u and d:
        syn.append(f"https://doi.org/{d}")

    return [s for s in syn if _is_real_http_url(s)]

def _fetch_unpaywall_mirrors(doi: str) -> list[str]:
    if not doi:
        return []
    urls = []
    try:
        r = requests.get(
            f"https://api.unpaywall.org/v2/{doi}",
            params={"email": "chkam.dev@gmail.com"},
            headers=api_headers(), timeout=10,
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
                r = s.get(f"{d}/{doi}", timeout=10, verify=False)
            if r.status_code != 200 or not r.text or "not available" in r.text.lower() or "robot" in r.text.lower():
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
        meta_names = [
            "citation_pdf_url", "citation_fulltext_pdf", "eprints.document_url",
            "DC.identifier", "bepress_citation_pdf_url", "prism.url"
        ]
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
    url, title, doi, folder, ctx, keyword, extra_candidates = data
    result = {"url": url, "title": title, "success": False, "bytes": 0, "skipped": False}

    with ctx.lock:
        if ctx._at_capacity(keyword) or ctx.cancellation_event.is_set():
            result["skipped"] = True
            return result

    tried_unpaywall = False
    tried_scihub = False
    raw_candidates = []

    # 1. Synthesize publisher direct PDF endpoints
    raw_candidates.extend(_synthesize_direct_pdf_urls(url, doi))

    # 2. Add extra open access candidate links harvested from APIs
    if extra_candidates:
        raw_candidates.extend(extra_candidates)

    # 3. Add Unpaywall locations
    if doi:
        raw_candidates.extend(_fetch_unpaywall_mirrors(doi))
        tried_unpaywall = True

    # 4. Add original URL
    raw_candidates.append(url)

    # Dedup and prioritize candidate URLs
    seen_cands = set()
    candidate_urls = []
    for u in raw_candidates:
        if not _is_real_http_url(u):
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

            # Case 1: Got Valid Full-Text PDF
            if actual_pdf:
                if content_size < 8192:
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
                        if sleep_check_cancel(random.uniform(0.2, 0.5), ctx):
                            result["skipped"] = True
                            return result
                        continue

                extracted_doi = _extract_doi(current_url) or _extract_doi(final_url) or doi
                if extracted_doi and not tried_unpaywall:
                    tried_unpaywall = True
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

                # If landing page had no direct PDF link, trigger Sci-Hub check
                if extracted_doi and not tried_scihub:
                    tried_scihub = True
                    sh = _fetch_scihub_mirrors(extracted_doi)
                    for mm in sh:
                        norm_mm = normalise_url(mm)
                        if norm_mm not in visited_urls:
                            visited_urls.add(norm_mm)
                            candidate_urls.insert(0, mm)
                    if sh:
                        _log(f"    🔓 Trying Sci-Hub for DOI {extracted_doi}", run_id=ctx.run_id)
                        continue

                raise ValueError("HTML landing page without direct PDF")

            raise ValueError(f"Unsupported content-type: {ct}")

        except Exception as exc:
            err_str = str(exc)
            extracted_doi = _extract_doi(current_url) or doi
            if extracted_doi and not tried_unpaywall:
                tried_unpaywall = True
                mirrors = _fetch_unpaywall_mirrors(extracted_doi)
                for m in mirrors:
                    norm_m = normalise_url(m)
                    if norm_m not in visited_urls:
                        visited_urls.add(norm_m)
                        candidate_urls.append(m)
                if mirrors:
                    continue

            if extracted_doi and not tried_scihub and not candidate_urls:
                tried_scihub = True
                sh = _fetch_scihub_mirrors(extracted_doi)
                for mm in sh:
                    norm_mm = normalise_url(mm)
                    if norm_mm not in visited_urls:
                        visited_urls.add(norm_mm)
                        candidate_urls.append(mm)
                if sh:
                    _log(f"    🔓 Trying Sci-Hub for DOI {extracted_doi}", run_id=ctx.run_id)
                    continue

            if "Connection reset" in err_str or "10054" in err_str:
                wait = min(2 ** total_requests + random.uniform(0.5, 1.5), MAX_BACKOFF_S)
                if sleep_check_cancel(wait, ctx):
                    result["skipped"] = True
                    return result
            else:
                if candidate_urls and total_requests < max_requests:
                    if sleep_check_cancel(0.5 + random.uniform(0, 0.4), ctx):
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
    """Write BibTeX, RIS, APA 7, CSV, and JSON metadata including relevance scores."""
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
                f.write(f"  author  = {{{' and '.join(_escape_bibtex(a) for a in p.authors)}}},\n")
            f.write(f"  title   = {{{{{_escape_bibtex(clean_title(p.title))}}}}},\n")
            if p.journal:
                f.write(f"  journal = {{{_escape_bibtex(p.journal)}}},\n")
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
            if p.relevance_score > 0:
                note_parts.append(f"relevance: {int(p.relevance_score * 100)}%")
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
            rel_str = f", Match: {int(p.relevance_score * 100)}%" if p.relevance_score > 0 else ""
            if p.quartile:
                f.write(f"N1  - SJR Quartile: {p.quartile}, Citations: {p.citations}{rel_str}\n")
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
        w.writerow(["Quartile", "Relevance", "Citations", "Year", "Title", "Authors",
                    "Journal", "DOI", "Source", "PDF File", "Full Path"])
        for p in papers:
            rel_str = f"{int(p.relevance_score * 100)}%" if p.relevance_score > 0 else "N/A"
            w.writerow([
                p.quartile or "Unranked",
                rel_str,
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
                "relevance_score": round(p.relevance_score, 3),
                "source": p.source,
                "pdf_filename": Path(p.pdf_path).name if p.pdf_path else "",
                "pdf_path": p.pdf_path,
                "abstract": p.abstract,
                "concepts": p.concepts,
            }
            for p in papers
        ]
        json.dump(corpus, f, indent=2, ensure_ascii=False)

# ══════════════════════════════════════════════════════════════════════════════
#  SQLITE HISTORY & TOPIC MEMORY
# ══════════════════════════════════════════════════════════════════════════════

def _history_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(HISTORY_DB), timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
    except Exception:
        pass
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
    min_relevance: float = DEFAULT_MIN_RELEVANCE,
    ctx: DownloadContext | None = None,
    progress_callback=None,
    status_callback=None,
    paper_callback=None,
) -> list[Paper]:
    """Autonomous execution of literature harvest, ranking, download and citations."""
    global log_file_path

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
    global active_run_id
    with lock_run_id:
        active_run_id = run_id

    # Deduplicate terms between keywords and focus
    kw_words = keywords.strip().split()
    foc_words = focus.strip().split()
    dedup_words = []
    for w in kw_words + foc_words:
        if w.lower() not in [x.lower() for x in dedup_words]:
            dedup_words.append(w)
    combined_query = " ".join(dedup_words)
    query_norm = combined_query.lower()

    if status_callback:
        status_callback("Phase 1: Searching scholarly databases…", "#58a6ff")
    _log(f"\n{'═'*65}", run_id=run_id)
    _log("  🚀 RESEARCH PDF DOWNLOADER — v10 Ultra Pro", run_id=run_id)
    _log(f"  Query: {keywords} | Focus: {focus or 'None'} | Years: {year_start}-{year_end}", run_id=run_id)
    _log(f"  Target: {max_articles} PDFs | Filter: {quartile_filter} | Min Match: {int(min_relevance*100)}%", run_id=run_id)
    _log(f"  Folder: {folder}", run_id=run_id)
    _log(f"{'═'*65}\n", run_id=run_id)

    # ── Long-title keyword splitter & focus normalization ─────────────────────
    norm_kw = " ".join(re.findall(r"[A-Za-z0-9]+", keywords.lower()))
    norm_fc = " ".join(re.findall(r"[A-Za-z0-9]+", (focus or "").lower()))
    effective_focus = focus.strip() if (norm_fc and norm_fc != norm_kw) else ""

    phrases = extract_keywords(keywords)
    multi_kw = len(phrases) > 1 and max_articles >= 2
    if multi_kw:
        k = min(len(phrases), max_articles)
        base, rem = divmod(max_articles, k)
        groups = []
        for i, ph in enumerate(phrases[:k]):
            share = base + (1 if i < rem else 0)
            if effective_focus and effective_focus.lower() not in ph.lower() and len(effective_focus.split()) <= 3:
                target_q = f"{ph} {effective_focus}".strip()
            else:
                target_q = ph
            groups.append((ph, target_q, share))
        if ctx:
            ctx.kw_targets = {lbl: sh for lbl, _, sh in groups}
            ctx.kw_done    = {lbl: 0  for lbl, _, _ in groups}
        _log(f"🔑 Long title split into {len(groups)} keyword groups:", run_id=run_id)
        for lbl, _, sh in groups:
            _log(f"     • {lbl}  →  {sh} article(s)", run_id=run_id)
    else:
        groups = [(keywords, combined_query, max_articles)]

    # ── PHASE 1: Harvest ──────────────────────────────────────────────────────
    papers: list[Paper] = []
    pool_size = max(max_articles * 4, min(max_articles * 8, 200))

    HARVESTERS = [
        ("OpenAlex",         harvest_openalex,         max(200, pool_size)),
        ("Crossref",         harvest_crossref,         250),
        ("Europe PMC",       harvest_europepmc,        150),
        ("PubMed/PMC",       harvest_pubmed,           150),
        ("Semantic Scholar", harvest_semantic_scholar, 100),
        ("DOAJ",             harvest_doaj,             100),
        ("arXiv",            harvest_arxiv,            100),
        ("CORE",             harvest_core,             100),
        ("BASE",             harvest_base,             100),
    ]

    for label, search, share in groups:
        if is_cancelled(ctx):
            break
        group_pool = max(share * 8, 60) if multi_kw else max(pool_size, 60)
        group_start = len(papers)
        if multi_kw:
            _log(f"\n🔎 Keyword '{label}'…", run_id=run_id)

        sub_focus = effective_focus if (effective_focus and effective_focus.lower() not in label.lower()) else ""

        for name, fn, cap in HARVESTERS:
            if len(papers) - group_start >= group_pool or is_cancelled(ctx):
                break
            try:
                cand = fn(search, year_start, year_end, max_res=min(cap, group_pool), ctx=ctx,
                          focus=sub_focus, min_relevance=min_relevance)
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

    try:
        load_scimago_quartiles()
    except Exception as e:
        _log(f"  ⚠️  Scimago ranking unavailable ({e}); continuing unranked.", run_id=run_id)
    try:
        enrich_with_openalex(papers, ctx=ctx)
    except Exception as e:
        _log(f"  ⚠️  Metadata enrichment skipped ({e}); using harvested metadata.", run_id=run_id)

    for p in papers:
        try:
            p.quartile = quartile_for(p.issns, p.journal)
        except Exception:
            p.quartile = ""

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
        targets.append((p.url, clean_title(p.title), p.clean_doi(), dest, ctx, pk, p.candidate_urls))

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
                    pp = by_url.get(res["url"])
                    if res["success"]:
                        if pp:
                            pp.pdf_path = res.get("path", "")
                            if paper_callback:
                                try:
                                    paper_callback(pp, res)
                                except Exception:
                                    pass

                    with ctx.lock:
                        succ = ctx.successful_downloads

                    if progress_callback:
                        progress_callback(succ, max_articles)
                    if status_callback:
                        status_callback(f"Downloading: {succ}/{max_articles} saved (checked {done_count})", "#3fb950")

                    icon = "✅" if res["success"] else "❌"
                    qtag = pp.quartile if pp else ""
                    rel_tag = f"{int(pp.relevance_score * 100)}%" if pp and pp.relevance_score > 0 else "—"
                    detail = f"{res['bytes'] // 1024} KB" if res["success"] else res.get("error", "failed")[:45]
                    _log(f"  {icon} [{qtag or '—':<4} | {rel_tag:>4}] {res['title'][:50]:<50} {detail}", run_id=run_id)

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
             q_hi if p.quartile in ("Q1", "Q2") else q_lo, ctx, None, p.candidate_urls)
            for p in filtered if not p.pdf_path and p.url in skipped_urls
        ]
        if reclaim:
            _log(f"\n♻️  Filling {gap} leftover slot(s) from held-back candidates…", run_id=run_id)
            _drain(reclaim)

    # ── PHASE 4: Citations & Memory ───────────────────────────────────────────
    downloaded = [p for p in filtered if p.pdf_path]
    if downloaded:
        _log("\n📚 Writing references.bib, .ris, APA 7th, results.csv, corpus_metadata.json…", run_id=run_id)
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
        # Luxury Obsidian 4K Dark Glass Palette
        BG          = "#090d16"      # Deep obsidian space canvas
        CARD        = "#111827"      # Elevated glass card
        CARD_ALT    = "#0b0f19"      # Inset field/terminal background
        BORDER      = "#1f293d"      # Subtle card border
        BORDER_FOCUS= "#06b6d4"      # Neon Cyan focus ring

        TXT         = "#f8fafc"      # Crisp bright white
        TXT_MUTED   = "#94a3b8"      # Cool slate gray
        TXT_DIM     = "#64748b"      # Subdued hint text

        CYAN        = "#06b6d4"      # Electric Cyan
        EMERALD     = "#10b981"      # Emerald Green
        EMERALD_HI  = "#059669"      # Emerald Hover
        ROSE        = "#f43f5e"      # Rose Red
        ROSE_HI     = "#e11d48"      # Rose Hover
        AMBER       = "#f59e0b"      # Amber Gold
        BLUE        = "#38bdf8"      # Sky Blue
        INDIGO      = "#6366f1"      # Neon Indigo
        PURPLE      = "#8b5cf6"      # Deep Violet

        def __init__(self):
            self.root = tk.Tk()

            # 1. 4K High-DPI Scaling Engine
            try:
                dpi_inch = self.root.winfo_fpixels('1i')
                self.scale = max(1.0, dpi_inch / 96.0)
            except Exception:
                self.scale = 1.0

            self.root.title("Research Literature Harvester v10 Ultra Pro — 4K UHD Edition")
            self.root.configure(bg=self.BG)

            # Center window with high-DPI scaled dimensions
            win_w = int(1140 * self.scale)
            win_h = int(840 * self.scale)
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
            pos_x = max(0, (screen_w - win_w) // 2)
            pos_y = max(0, (screen_h - win_h) // 2)
            self.root.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
            self.root.minsize(int(920 * self.scale), int(680 * self.scale))

            # Set Window Icon
            ico_path = SCRIPT_DIR / "articles.ico"
            if ico_path.exists():
                try:
                    self.root.iconbitmap(default=str(ico_path))
                except Exception:
                    pass

            # 2. Variable Typography System
            f_disp = "Segoe UI Variable Display" if self._font_exists("Segoe UI Variable Display") else ("Segoe UI Semibold" if self._font_exists("Segoe UI Semibold") else "Segoe UI")
            f_text = "Segoe UI Variable Text" if self._font_exists("Segoe UI Variable Text") else "Segoe UI"
            f_mono = "Cascadia Code" if self._font_exists("Cascadia Code") else ("Cascadia Mono" if self._font_exists("Cascadia Mono") else "Consolas")

            self.title_font    = (f_disp, int(15 * self.scale), "bold")
            self.sub_font      = (f_text, int(8.5 * self.scale))
            self.header_font   = (f_disp, int(10.5 * self.scale), "bold")
            self.label_font    = (f_text, int(9.5 * self.scale))
            self.btn_font      = (f_disp, int(9.5 * self.scale), "bold")
            self.btn_sm_font   = (f_disp, int(8.5 * self.scale), "bold")
            self.mono_font     = (f_mono, int(9 * self.scale))
            self.stat_num_font = (f_disp, int(16 * self.scale), "bold")
            self.stat_lbl_font = (f_text, int(8 * self.scale), "bold")
            self.stat_sub_font = (f_text, int(7.5 * self.scale))

            # 3. Modern TTK Theme Configuration (Clam Engine)
            self.style = ttk.Style()
            self.style.theme_use("clam")

            # Notebook Tabs
            self.style.configure("TNotebook", background=self.BG, borderwidth=0)
            self.style.configure("TNotebook.Tab", background="#131b2e", foreground=self.TXT_MUTED,
                                 padding=[int(16 * self.scale), int(7 * self.scale)],
                                 font=self.header_font, borderwidth=0)
            self.style.map("TNotebook.Tab",
                           background=[("selected", self.CARD)],
                           foreground=[("selected", self.CYAN)])

            # Progressbar
            self.style.configure("G.Horizontal.TProgressbar",
                                 troughcolor=self.CARD_ALT, bordercolor=self.BORDER,
                                 background=self.EMERALD, lightcolor=self.EMERALD,
                                 darkcolor=self.EMERALD, thickness=int(11 * self.scale))

            # Combobox
            self.style.configure("TCombobox", fieldbackground=self.CARD_ALT, background=self.CARD,
                                 foreground=self.TXT, bordercolor=self.BORDER, arrowcolor=self.CYAN)
            self.style.map("TCombobox",
                           fieldbackground=[("readonly", self.CARD_ALT)],
                           selectbackground=[("readonly", self.CARD_ALT)],
                           selectforeground=[("readonly", self.TXT)])

            # Treeview
            self.style.configure("Treeview", background="#0b0f19", foreground=self.TXT,
                                 fieldbackground="#0b0f19", rowheight=int(30 * self.scale),
                                 font=self.label_font, borderwidth=0)
            self.style.configure("Treeview.Heading", background="#151f32", foreground=self.CYAN,
                                 font=self.header_font, borderwidth=1, relief="flat")
            self.style.map("Treeview",
                           background=[("selected", "#1e3a8a")],
                           foreground=[("selected", "#ffffff")])

            # State tracking
            self.is_running = False
            self.worker_thread = None
            self.downloaded_papers_list: list[Paper] = []
            self.tree_item_map: dict[str, Paper] = {}

            # Build UI Layout
            self._build_header()
            self._build_notebook()

            self.root.bind("<Return>", lambda e: self.start_download() if not self.is_running else None)
            self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        def _font_exists(self, name: str) -> bool:
            try:
                from tkinter import font as tkfont
                return name in tkfont.families()
            except Exception:
                return False

        def _build_header(self):
            header = tk.Frame(self.root, bg=self.BG)
            header.pack(fill="x", padx=int(22 * self.scale), pady=(int(14 * self.scale), int(6 * self.scale)))

            # Right Signature
            sig = tk.Frame(header, bg=self.BG)
            sig.pack(side="right", anchor="ne")
            tk.Label(sig, text="D E S I G N E D   &   E N G I N E E R E D   B Y", bg=self.BG, fg=self.CYAN,
                     font=("Segoe UI", int(7 * self.scale), "bold")).pack(anchor="e")
            tk.Label(sig, text="Kamran Ashraf", bg=self.BG, fg=self.TXT,
                     font=("Segoe UI Semibold", int(13 * self.scale))).pack(anchor="e", pady=(1, 0))
            tk.Frame(sig, bg=self.CYAN, height=int(2 * self.scale), width=int(125 * self.scale)).pack(anchor="e", pady=(2, 0))

            # Left Title & Badges
            htext = tk.Frame(header, bg=self.BG)
            htext.pack(side="left", anchor="w")

            title_row = tk.Frame(htext, bg=self.BG)
            title_row.pack(anchor="w")

            badge = tk.Label(title_row, text="⚡ 4K ULTRA HD", bg="#162638", fg=self.CYAN,
                             font=("Segoe UI", int(7.5 * self.scale), "bold"),
                             padx=int(6 * self.scale), pady=int(2 * self.scale),
                             highlightthickness=1, highlightbackground=self.CYAN)
            badge.pack(side="left", padx=(0, int(8 * self.scale)))

            tk.Label(title_row, text="Research Literature Harvester v10 Ultra Pro", bg=self.BG, fg=self.TXT,
                     font=self.title_font).pack(side="left")

            engine_str = "   •   curl_cffi active (stealth impersonation)" if HAS_CFFI else "   •   requests mode"
            engine_fg = self.EMERALD if HAS_CFFI else self.TXT_MUTED
            sub_row = tk.Frame(htext, bg=self.BG)
            sub_row.pack(anchor="w", pady=(int(2 * self.scale), 0))
            tk.Label(sub_row, text="60% Relevance Gate  ·  Discipline Collision Guard  ·  Multi-Tier 6-Layer Cascading",
                     bg=self.BG, fg=self.TXT_MUTED, font=self.sub_font).pack(side="left")
            tk.Label(sub_row, text=engine_str, bg=self.BG, fg=engine_fg, font=self.sub_font).pack(side="left")

        def _build_notebook(self):
            self.notebook = ttk.Notebook(self.root)
            self.notebook.pack(fill="both", expand=True, padx=int(20 * self.scale), pady=(int(6 * self.scale), int(14 * self.scale)))

            self.tab_dashboard = tk.Frame(self.notebook, bg=self.BG)
            self.tab_grid      = tk.Frame(self.notebook, bg=self.BG)
            self.tab_logs      = tk.Frame(self.notebook, bg=self.BG)

            self.notebook.add(self.tab_dashboard, text="⚡  HARVESTER DASHBOARD")
            self.notebook.add(self.tab_grid,      text="📚  DOWNLOADED PAPERS (LIVE GRID)")
            self.notebook.add(self.tab_logs,      text="🖥  TERMINAL CONSOLE")

            self._build_tab_dashboard()
            self._build_tab_grid()
            self._build_tab_logs()

        def _build_tab_dashboard(self):
            p = self.tab_dashboard

            # Top Metric Cards (4 Cards)
            stats_frame = tk.Frame(p, bg=self.BG)
            stats_frame.pack(fill="x", pady=(int(10 * self.scale), int(8 * self.scale)))
            for i in range(4):
                stats_frame.columnconfigure(i, weight=1)

            self.lbl_card_downloads_num, self.lbl_card_downloads_sub = self._metric_card(
                stats_frame, 0, "📥 DOWNLOADED", "0 / 50", "0% target achieved", self.EMERALD
            )
            self.lbl_card_data_num, self.lbl_card_data_sub = self._metric_card(
                stats_frame, 1, "⚡ DATA VOLUME", "0.00 MB", f"{MAX_WORKERS} parallel threads", self.CYAN
            )
            self.lbl_card_quality_num, self.lbl_card_quality_sub = self._metric_card(
                stats_frame, 2, "🏆 TOP IMPACT", "0 Q1  ·  0 Q2", "Scimago journal index", self.AMBER
            )
            self.lbl_card_relevance_num, self.lbl_card_relevance_sub = self._metric_card(
                stats_frame, 3, "🎯 PRECISION", "100% Avg", "Min match threshold: 60%", self.PURPLE
            )

            # Search & Filtration Settings Card
            card = self._card(p, "Search Parameters & Scholarly Filtration")
            card.pack(fill="x", pady=(int(4 * self.scale), int(6 * self.scale)))
            grid = tk.Frame(card, bg=self.CARD)
            grid.pack(fill="x", padx=int(16 * self.scale), pady=(int(4 * self.scale), int(12 * self.scale)))
            grid.columnconfigure(1, weight=1)
            grid.columnconfigure(3, weight=1)

            self.ent_keywords = self._field(grid, "Keywords / Title", 0, 0, colspan=3)
            self.ent_focus    = self._field(grid, "Focus (optional)", 1, 0, colspan=3)

            # Years Row with Quick Presets
            tk.Label(grid, text="Publication Years", bg=self.CARD, fg=self.TXT_MUTED,
                     font=self.label_font).grid(row=2, column=0, sticky="w", pady=(int(6 * self.scale), int(3 * self.scale)), padx=(0, int(8 * self.scale)))

            years_row = tk.Frame(grid, bg=self.CARD)
            years_row.grid(row=2, column=1, columnspan=3, sticky="we", pady=(int(6 * self.scale), int(3 * self.scale)))
            years_row.columnconfigure(1, weight=1)
            years_row.columnconfigure(3, weight=1)

            tk.Label(years_row, text="From", bg=self.CARD, fg=self.TXT_DIM, font=self.sub_font).grid(row=0, column=0, padx=(0, 6))
            self.ent_y1 = tk.Entry(years_row, bg=self.CARD_ALT, fg=self.TXT, insertbackground=self.TXT,
                                   relief="flat", font=self.label_font, width=6,
                                   highlightthickness=1, highlightbackground=self.BORDER, highlightcolor=self.BORDER_FOCUS)
            self.ent_y1.grid(row=0, column=1, sticky="w", ipady=int(4 * self.scale))
            self.ent_y1.insert(0, "2023")

            tk.Label(years_row, text="To", bg=self.CARD, fg=self.TXT_DIM, font=self.sub_font).grid(row=0, column=2, padx=(12, 6))
            self.ent_y2 = tk.Entry(years_row, bg=self.CARD_ALT, fg=self.TXT, insertbackground=self.TXT,
                                   relief="flat", font=self.label_font, width=6,
                                   highlightthickness=1, highlightbackground=self.BORDER, highlightcolor=self.BORDER_FOCUS)
            self.ent_y2.grid(row=0, column=3, sticky="w", ipady=int(4 * self.scale))
            self.ent_y2.insert(0, "2026")

            # Presets Frame
            p_frame = tk.Frame(years_row, bg=self.CARD)
            p_frame.grid(row=0, column=4, sticky="e", padx=(16, 0))
            self._button(p_frame, "⚡ 2024–2026", lambda: self._set_year_preset("2024", "2026"), kind="chip").pack(side="left", padx=2)
            self._button(p_frame, "📅 Past 5 Yrs", lambda: self._set_year_preset("2021", "2026"), kind="chip").pack(side="left", padx=2)
            self._button(p_frame, "🌐 All Time", lambda: self._set_year_preset("2000", "2026"), kind="chip").pack(side="left", padx=2)

            # Max Articles & Journal Filter
            self.ent_max = self._field(grid, "Max articles", 3, 0)
            self.ent_max.insert(0, "50")

            tk.Label(grid, text="Journal Filter", bg=self.CARD, fg=self.TXT_MUTED,
                     font=self.label_font).grid(row=3, column=2, sticky="w", pady=(int(6 * self.scale), int(3 * self.scale)), padx=(int(12 * self.scale), int(8 * self.scale)))
            self.cbo_quartile = ttk.Combobox(grid, values=["Q1 + Q2 (High Impact)", "Q1 to Q4 (All Ranked)", "All (Including Preprints)"],
                                             state="readonly", font=self.label_font)
            self.cbo_quartile.current(1)
            self.cbo_quartile.grid(row=3, column=3, sticky="we", pady=(int(6 * self.scale), int(3 * self.scale)))

            # Min Relevance Row
            tk.Label(grid, text="Min Relevance", bg=self.CARD, fg=self.TXT_MUTED,
                     font=self.label_font).grid(row=4, column=0, sticky="w", pady=(int(6 * self.scale), int(3 * self.scale)), padx=(0, int(8 * self.scale)))
            self.cbo_relevance = ttk.Combobox(grid, values=["60% (Strict - Recommended)", "70% (High Precision)", "80% (Exact Match)", "50% (Moderate)"],
                                              state="readonly", font=self.label_font)
            self.cbo_relevance.current(0)
            self.cbo_relevance.grid(row=4, column=1, sticky="we", pady=(int(6 * self.scale), int(3 * self.scale)))

            # Save Folder Row
            tk.Label(grid, text="Save Destination", bg=self.CARD, fg=self.TXT_MUTED,
                     font=self.label_font).grid(row=5, column=0, sticky="w", pady=(int(6 * self.scale), int(3 * self.scale)), padx=(0, int(8 * self.scale)))
            folder_row = tk.Frame(grid, bg=self.CARD)
            folder_row.grid(row=5, column=1, columnspan=3, sticky="we", pady=(int(6 * self.scale), int(3 * self.scale)))
            folder_row.columnconfigure(0, weight=1)

            default_dir = str(get_default_save_folder())
            self.ent_folder = tk.Entry(folder_row, bg=self.CARD_ALT, fg=self.TXT, insertbackground=self.TXT,
                                       relief="flat", font=self.label_font,
                                       highlightthickness=1, highlightbackground=self.BORDER,
                                       highlightcolor=self.BORDER_FOCUS)
            self.ent_folder.grid(row=0, column=0, sticky="we", ipady=int(4 * self.scale))
            self.ent_folder.insert(0, default_dir)
            self.btn_browse = self._button(folder_row, "📁 Browse", self._browse_folder, kind="ghost")
            self.btn_browse.grid(row=0, column=1, padx=(int(8 * self.scale), 0))
            self.btn_open_folder = self._button(folder_row, "📂 Open", self._open_download_folder, kind="ghost")
            self.btn_open_folder.grid(row=0, column=2, padx=(int(6 * self.scale), 0))

            # Controls Bar
            ctrl = tk.Frame(p, bg=self.BG)
            ctrl.pack(fill="x", pady=(int(8 * self.scale), int(4 * self.scale)))

            self.btn_start = self._button(ctrl, "▶  Start Harvesting", self.start_download, kind="primary")
            self.btn_start.pack(side="left")
            self.btn_cancel = self._button(ctrl, "■  Cancel", self.cancel_download, kind="danger")
            self.btn_cancel.pack(side="left", padx=(int(8 * self.scale), 0))
            self.btn_cancel.configure(state="disabled")

            self._button(ctrl, "📂 Open Folder", self._open_download_folder, kind="ghost").pack(side="left", padx=(int(8 * self.scale), 0))
            self._button(ctrl, "📋 Copy Citations", self._copy_citations_quick, kind="ghost").pack(side="left", padx=(int(8 * self.scale), 0))

            self._button(ctrl, "📊 View Papers Grid", lambda: self.notebook.select(self.tab_grid), kind="accent").pack(side="right")

            # Progress Bar & Phase Status
            prog_card = self._card(p, "Harvesting Pipeline Status & Execution Flow")
            prog_card.pack(fill="x", pady=(int(6 * self.scale), 0))
            prog_inner = tk.Frame(prog_card, bg=self.CARD)
            prog_inner.pack(fill="x", padx=int(16 * self.scale), pady=(int(4 * self.scale), int(12 * self.scale)))

            self.bar_var = tk.DoubleVar()
            self.bar = ttk.Progressbar(prog_inner, variable=self.bar_var, maximum=100,
                                       style="G.Horizontal.TProgressbar")
            self.bar.pack(fill="x")

            stat_row = tk.Frame(prog_inner, bg=self.CARD)
            stat_row.pack(fill="x", pady=(int(6 * self.scale), 0))

            self.lbl_status_phase = tk.Label(stat_row, text="[IDLE]", bg="#182335", fg=self.CYAN,
                                             font=("Segoe UI", int(8 * self.scale), "bold"),
                                             padx=int(6 * self.scale), pady=int(1 * self.scale))
            self.lbl_status_phase.pack(side="left", padx=(0, int(8 * self.scale)))

            self.lbl_progress = tk.Label(stat_row, text="Ready — configure topic parameters and press Start Harvesting",
                                         bg=self.CARD, fg=self.TXT_MUTED, font=self.label_font)
            self.lbl_progress.pack(side="left")

        def _build_tab_grid(self):
            p = self.tab_grid

            # Action bar at top of grid
            top_bar = tk.Frame(p, bg=self.BG)
            top_bar.pack(fill="x", pady=(int(8 * self.scale), int(6 * self.scale)))

            self.lbl_tree_count = tk.Label(top_bar, text="Showing 0 downloaded paper(s)  ·  Double-click any row to view full-text PDF",
                                           bg=self.BG, fg=self.TXT_MUTED, font=self.label_font)
            self.lbl_tree_count.pack(side="left")

            self._button(top_bar, "📄 Open PDF", self._open_selected_pdf, kind="accent").pack(side="right")
            self._button(top_bar, "📝 Copy APA 7", self._copy_selected_apa, kind="ghost").pack(side="right", padx=(0, int(6 * self.scale)))
            self._button(top_bar, "📦 Copy BibTeX", self._copy_selected_bib, kind="ghost").pack(side="right", padx=(0, int(6 * self.scale)))
            self._button(top_bar, "📊 Export CSV", self._export_tree_csv, kind="ghost").pack(side="right", padx=(0, int(6 * self.scale)))

            # Treeview Frame with dual scrollbars
            tree_frame = tk.Frame(p, bg=self.BG, highlightthickness=1, highlightbackground=self.BORDER)
            tree_frame.pack(fill="both", expand=True)

            cols = ("idx", "quartile", "rel", "cits", "year", "title", "journal", "size")
            self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", selectmode="browse")

            col_defs = [
                ("idx",      "#",           int(40 * self.scale),  "center"),
                ("quartile", "Quartile",    int(85 * self.scale),  "center"),
                ("rel",      "Match %",     int(80 * self.scale),  "center"),
                ("cits",     "Citations",   int(80 * self.scale),  "center"),
                ("year",     "Year",        int(65 * self.scale),  "center"),
                ("title",    "Paper Title", int(430 * self.scale), "w"),
                ("journal",  "Journal",     int(210 * self.scale), "w"),
                ("size",     "File Size",   int(90 * self.scale),  "e"),
            ]
            for c_id, c_name, c_width, c_align in col_defs:
                self.tree.heading(c_id, text=c_name, anchor=c_align)
                self.tree.column(c_id, width=c_width, anchor=c_align)

            # Color tags for quartiles
            self.tree.tag_configure("q1", foreground="#34d399")
            self.tree.tag_configure("q2", foreground="#60a5fa")
            self.tree.tag_configure("q3", foreground="#fbbf24")
            self.tree.tag_configure("q4", foreground="#94a3b8")
            self.tree.tag_configure("preprint", foreground="#c084fc")
            self.tree.tag_configure("unranked", foreground="#e2e8f0")

            vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
            hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
            self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

            vsb.pack(side="right", fill="y")
            hsb.pack(side="bottom", fill="x")
            self.tree.pack(side="left", fill="both", expand=True)

            self.tree.bind("<Double-1>", lambda e: self._open_selected_pdf())
            self.tree.bind("<Button-3>", self._show_tree_context_menu)

            # Context Menu
            self.tree_menu = tk.Menu(self.root, tearoff=0, bg=self.CARD, fg=self.TXT,
                                     activebackground=self.BORDER_FOCUS, activeforeground=self.TXT,
                                     font=self.label_font)
            self.tree_menu.add_command(label="📄 Open PDF File", command=self._open_selected_pdf)
            self.tree_menu.add_command(label="📂 Show in Windows Explorer", command=self._show_in_explorer)
            self.tree_menu.add_separator()
            self.tree_menu.add_command(label="🔗 Copy DOI to Clipboard", command=self._copy_selected_doi)
            self.tree_menu.add_command(label="📝 Copy APA 7 Citation", command=self._copy_selected_apa)
            self.tree_menu.add_command(label="📦 Copy BibTeX Citation", command=self._copy_selected_bib)

        def _build_tab_logs(self):
            p = self.tab_logs

            log_card = self._card(p, "Real-Time Terminal Execution Stream")
            log_card.pack(fill="both", expand=True, pady=(int(8 * self.scale), int(8 * self.scale)))

            log_body = tk.Frame(log_card, bg=self.CARD_ALT)
            log_body.pack(fill="both", expand=True, padx=int(12 * self.scale), pady=(0, int(10 * self.scale)))

            global log_widget
            log_widget = tk.Text(log_body, wrap="word", bg=self.CARD_ALT, fg="#adbac7",
                                 font=self.mono_font, insertbackground=self.TXT,
                                 relief="flat", state="disabled", padx=int(10 * self.scale), pady=int(8 * self.scale),
                                 highlightthickness=0, spacing1=int(2 * self.scale))
            log_widget.tag_configure("success", foreground=self.EMERALD)
            log_widget.tag_configure("error",   foreground=self.ROSE)
            log_widget.tag_configure("info",    foreground=self.BLUE)
            log_widget.tag_configure("warning", foreground=self.AMBER)
            log_widget.tag_configure("purple",  foreground=self.PURPLE)

            sb = ttk.Scrollbar(log_body, command=log_widget.yview)
            log_widget.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            log_widget.pack(side="left", fill="both", expand=True)

            bottom_bar = tk.Frame(p, bg=self.BG)
            bottom_bar.pack(fill="x", pady=(0, int(6 * self.scale)))
            self._button(bottom_bar, "📋 Copy Terminal Logs", self._copy_logs, kind="ghost").pack(side="left")
            self._button(bottom_bar, "🧹 Clear Console", self._clear_logs, kind="ghost").pack(side="left", padx=(int(8 * self.scale), 0))
            self._button(bottom_bar, "💾 Save Log to File", self._export_logs_file, kind="ghost").pack(side="left", padx=(int(8 * self.scale), 0))

        def _metric_card(self, parent, col, title, initial_val, sub_label, accent_color) -> tuple[tk.Label, tk.Label]:
            outer = tk.Frame(parent, bg=self.CARD, highlightthickness=1,
                             highlightbackground=self.BORDER, highlightcolor=self.BORDER)
            outer.grid(row=0, column=col, sticky="nsew", padx=int(4 * self.scale))

            top = tk.Frame(outer, bg=self.CARD)
            top.pack(fill="x", padx=int(12 * self.scale), pady=(int(8 * self.scale), 0))
            tk.Label(top, text=title, bg=self.CARD, fg=self.TXT_DIM, font=self.stat_lbl_font).pack(side="left")
            tk.Frame(top, bg=accent_color, width=int(8 * self.scale), height=int(8 * self.scale)).pack(side="right")

            num_lbl = tk.Label(outer, text=initial_val, bg=self.CARD, fg=self.TXT, font=self.stat_num_font)
            num_lbl.pack(anchor="w", padx=int(12 * self.scale), pady=(int(2 * self.scale), 0))

            sub_lbl = tk.Label(outer, text=sub_label, bg=self.CARD, fg=self.TXT_MUTED, font=self.stat_sub_font)
            sub_lbl.pack(anchor="w", padx=int(12 * self.scale), pady=(0, int(8 * self.scale)))

            return num_lbl, sub_lbl

        def _card(self, parent, title: str) -> tk.Frame:
            outer = tk.Frame(parent, bg=self.CARD, highlightthickness=1,
                             highlightbackground=self.BORDER, highlightcolor=self.BORDER)
            head_row = tk.Frame(outer, bg=self.CARD)
            head_row.pack(fill="x", padx=int(16 * self.scale), pady=(int(8 * self.scale), int(4 * self.scale)))
            tk.Label(head_row, text=title, bg=self.CARD, fg=self.CYAN, font=self.header_font).pack(side="left")
            return outer

        def _field(self, parent, label, row, col, colspan=1) -> tk.Entry:
            tk.Label(parent, text=label, bg=self.CARD, fg=self.TXT_MUTED,
                     font=self.label_font).grid(row=row, column=col, sticky="w",
                                                pady=(int(6 * self.scale), int(3 * self.scale)), padx=(0, int(8 * self.scale)))
            ent = tk.Entry(parent, bg=self.CARD_ALT, fg=self.TXT, insertbackground=self.TXT,
                           relief="flat", font=self.label_font,
                           highlightthickness=1, highlightbackground=self.BORDER,
                           highlightcolor=self.BORDER_FOCUS)
            ent.grid(row=row, column=col + 1, columnspan=colspan, sticky="we",
                     pady=(int(6 * self.scale), int(3 * self.scale)), ipady=int(4 * self.scale))
            return ent

        def _button(self, parent, text, command, kind="ghost") -> tk.Button:
            palette = {
                "primary": (self.EMERALD, self.EMERALD_HI, "#ffffff"),
                "danger":  (self.ROSE,    self.ROSE_HI,    "#ffffff"),
                "accent":  (self.CYAN,    self.BLUE,       "#090d16"),
                "ghost":   (self.CARD,    "#1c2638",       self.TXT),
                "chip":    (self.CARD_ALT,"#1c2638",       self.CYAN),
            }
            base, hover, fg = palette[kind]
            btn_font = self.btn_sm_font if kind == "chip" else self.btn_font
            px = int(10 * self.scale) if kind == "chip" else int(15 * self.scale)
            py = int(3 * self.scale) if kind == "chip" else int(6 * self.scale)

            btn = tk.Button(parent, text=text, command=command, bg=base, fg=fg,
                            font=btn_font, relief="flat", bd=0,
                            activebackground=hover, activeforeground=fg,
                            padx=px, pady=py, cursor="hand2")
            if kind in ("ghost", "chip"):
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

        def _set_year_preset(self, y1: str, y2: str):
            self.ent_y1.delete(0, "end")
            self.ent_y1.insert(0, y1)
            self.ent_y2.delete(0, "end")
            self.ent_y2.insert(0, y2)

        def _browse_folder(self):
            path = filedialog.askdirectory(initialdir=self.ent_folder.get() or str(Path.home()))
            if path:
                self.ent_folder.delete(0, "end")
                self.ent_folder.insert(0, path)

        def _open_download_folder(self):
            folder_str = self.ent_folder.get().strip()
            if not folder_str:
                return
            try:
                p = Path(folder_str)
                p.mkdir(parents=True, exist_ok=True)
                if sys.platform == "win32":
                    os.startfile(str(p))
                else:
                    subprocess.Popen(["xdg-open", str(p)])
            except Exception as e:
                messagebox.showerror("Open Folder", f"Could not open the folder:\n{e}")

        def _on_paper_downloaded(self, p: Paper, res: dict):
            def _ui_insert():
                idx = len(self.downloaded_papers_list) + 1
                self.downloaded_papers_list.append(p)
                size_kb = res.get("bytes", 0) // 1024
                size_str = f"{size_kb / 1024:.2f} MB" if size_kb > 1024 else f"{size_kb} KB"
                rel_str = f"{int(p.relevance_score * 100)}%"
                tag = (p.quartile or "unranked").lower()

                item_id = self.tree.insert("", "end", values=(
                    idx,
                    p.quartile or "Unranked",
                    rel_str,
                    p.citations,
                    p.year,
                    clean_title(p.title),
                    p.journal[:35] if p.journal else "—",
                    size_str,
                ), tags=(tag,))
                self.tree_item_map[item_id] = p
                self.lbl_tree_count.configure(
                    text=f"Showing {idx} downloaded paper(s)  ·  Double-click row to view full-text PDF"
                )
                self.update_stats()
            self.root.after(0, _ui_insert)

        def _get_selected_paper(self) -> Paper | None:
            sel = self.tree.selection()
            if sel:
                return self.tree_item_map.get(sel[0])
            return None

        def _open_selected_pdf(self):
            p = self._get_selected_paper()
            if p and p.pdf_path and Path(p.pdf_path).exists():
                try:
                    if sys.platform == "win32":
                        os.startfile(p.pdf_path)
                    else:
                        subprocess.Popen(["xdg-open", p.pdf_path])
                except Exception as e:
                    messagebox.showerror("Open PDF", f"Could not open the PDF:\n{e}")
            elif p and p.pdf_path:
                messagebox.showwarning("Open PDF", "The PDF file is no longer at its saved location.")
            else:
                messagebox.showinfo("Open PDF", "Please select a downloaded paper from the list first.")

        def _show_in_explorer(self):
            p = self._get_selected_paper()
            if p and p.pdf_path and Path(p.pdf_path).exists():
                try:
                    if sys.platform == "win32":
                        subprocess.Popen(f'explorer /select,"{p.pdf_path}"')
                    else:
                        subprocess.Popen(["xdg-open", str(Path(p.pdf_path).parent)])
                except Exception as e:
                    messagebox.showerror("Show in Explorer", f"Could not open the file location:\n{e}")

        def _copy_selected_doi(self):
            p = self._get_selected_paper()
            if p and p.doi:
                self.root.clipboard_clear()
                self.root.clipboard_append(f"https://doi.org/{p.clean_doi()}")
                self.set_status(f"Copied DOI: https://doi.org/{p.clean_doi()}", self.CYAN)
            else:
                messagebox.showinfo("Copy DOI", "Selected paper does not have a registered DOI.")

        def _copy_selected_apa(self):
            p = self._get_selected_paper()
            if p:
                auth = _authors_apa(p.authors) if p.authors else "Unknown"
                yr = f"({p.year})" if p.year else "(n.d.)"
                t = clean_title(p.title)
                j = f" {p.journal}." if p.journal else ""
                d = f" https://doi.org/{p.clean_doi()}" if p.clean_doi() else ""
                apa = f"{auth} {yr}. {t}.{j}{d}"
                self.root.clipboard_clear()
                self.root.clipboard_append(apa)
                self.set_status("Copied APA 7 citation to clipboard", self.CYAN)

        def _copy_selected_bib(self):
            p = self._get_selected_paper()
            if p:
                bib = _format_bibtex_entry(p)
                self.root.clipboard_clear()
                self.root.clipboard_append(bib)
                self.set_status("Copied BibTeX citation to clipboard", self.CYAN)

        def _copy_citations_quick(self):
            if not self.downloaded_papers_list:
                messagebox.showinfo("Copy Citations", "No papers downloaded yet in current run.")
                return
            lines = []
            for p in self.downloaded_papers_list:
                auth = _authors_apa(p.authors) if p.authors else "Unknown"
                yr = f"({p.year})" if p.year else "(n.d.)"
                t = clean_title(p.title)
                j = f" {p.journal}." if p.journal else ""
                d = f" https://doi.org/{p.clean_doi()}" if p.clean_doi() else ""
                lines.append(f"{auth} {yr}. {t}.{j}{d}")
            text = "\n\n".join(lines)
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.set_status(f"Copied {len(lines)} APA 7 citations to clipboard", self.CYAN)

        def _export_tree_csv(self):
            folder_str = self.ent_folder.get().strip()
            if folder_str:
                csv_path = Path(folder_str) / "results.csv"
                if csv_path.exists():
                    self.set_status(f"CSV available at: {csv_path}", self.EMERALD)
                    try:
                        if sys.platform == "win32":
                            os.startfile(str(csv_path))
                        else:
                            subprocess.Popen(["xdg-open", str(csv_path)])
                    except Exception as e:
                        messagebox.showerror("Export CSV", f"Could not open results.csv:\n{e}")
                    return
            messagebox.showinfo("Export CSV", "Harvesting must complete to generate results.csv.")

        def _show_tree_context_menu(self, event):
            item_id = self.tree.identify_row(event.y)
            if item_id:
                self.tree.selection_set(item_id)
                self.tree_menu.tk_popup(event.x_root, event.y_root)

        def _copy_logs(self):
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(log_widget.get("1.0", "end-1c"))
                self.set_status("Terminal logs copied to clipboard", self.CYAN)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to copy logs: {e}")

        def _clear_logs(self):
            log_widget.configure(state="normal")
            log_widget.delete("1.0", "end")
            log_widget.configure(state="disabled")

        def _export_logs_file(self):
            fpath = filedialog.asksaveasfilename(defaultextension=".log", filetypes=[("Log files", "*.log"), ("Text files", "*.txt")])
            if fpath:
                try:
                    Path(fpath).write_text(log_widget.get("1.0", "end-1c"), encoding="utf-8")
                    self.set_status(f"Logs exported to {Path(fpath).name}", self.EMERALD)
                except Exception as e:
                    messagebox.showerror("Error", f"Failed to save log: {e}")

        def set_status(self, text: str, fg: str = "#94a3b8"):
            phase = "[IDLE]"
            if "Phase 1" in text or "Searching" in text or "Harvest" in text:
                phase = "[PHASE 1: HARVESTING]"
            elif "Phase 2" in text or "Ranking" in text:
                phase = "[PHASE 2: RANKING]"
            elif "Phase 3" in text or "Downloading" in text:
                phase = "[PHASE 3: DOWNLOADING]"
            elif "Complete" in text or "saved" in text:
                phase = "[COMPLETE]"
            elif "Cancel" in text:
                phase = "[CANCELLED]"

            def _update():
                self.lbl_status_phase.configure(text=phase)
                self.lbl_progress.configure(text=text, fg=fg)
            self.root.after(0, _update)

        def set_progress(self, val: int, max_val: int):
            self.root.after(0, lambda: (self.bar.configure(maximum=max(max_val, 1)), self.bar_var.set(val)))

        def update_stats(self):
            if not hasattr(self, "ctx") or not self.ctx:
                return
            ctx = self.ctx
            elapsed = max(1, int(time.time() - self.start_time))

            with ctx.lock:
                mb = ctx.total_bytes / (1024 * 1024)
                succ = ctx.successful_downloads
                target = ctx.target_downloads

            pct = int((succ / max(target, 1)) * 100)
            self.lbl_card_downloads_num.configure(text=f"{succ} / {target}")
            self.lbl_card_downloads_sub.configure(text=f"{pct}% ({elapsed}s elapsed)")

            self.lbl_card_data_num.configure(text=f"{mb:.2f} MB")
            speed = (mb / elapsed) * 1024
            self.lbl_card_data_sub.configure(text=f"{speed:.1f} KB/s  ·  {MAX_WORKERS} threads")

            papers = self.downloaded_papers_list
            q1 = sum(1 for p in papers if (p.quartile or "").upper() == "Q1")
            q2 = sum(1 for p in papers if (p.quartile or "").upper() == "Q2")
            q_ratio = ((q1 + q2) / max(len(papers), 1)) * 100 if papers else 0
            self.lbl_card_quality_num.configure(text=f"{q1} Q1  ·  {q2} Q2")
            self.lbl_card_quality_sub.configure(text=f"{q_ratio:.0f}% high-impact ratio")

            avg_rel = (sum(p.relevance_score for p in papers) / max(len(papers), 1)) * 100 if papers else 100
            min_rel_pct = int(getattr(self, "min_rel", 0.60) * 100)
            self.lbl_card_relevance_num.configure(text=f"{avg_rel:.0f}% Avg")
            self.lbl_card_relevance_sub.configure(text=f"Threshold: {min_rel_pct}% match")

        def _stats_ticker(self, token: int | None = None):
            # Single self-perpetuating 1s refresh loop, guarded by a token so a new
            # run cancels any ticker left over from a previous run (no overlap).
            if token is None:
                self._stats_token = getattr(self, "_stats_token", 0) + 1
                token = self._stats_token
            if token != getattr(self, "_stats_token", 0):
                return
            self.update_stats()
            if self.is_running:
                self.root.after(1000, lambda: self._stats_ticker(token))

        def enable_inputs(self, enable=True):
            def _do():
                state = "normal" if enable else "disabled"
                for w in (self.ent_keywords, self.ent_focus, self.ent_y1, self.ent_y2, self.ent_max, self.ent_folder):
                    try:
                        w.configure(state=state)
                    except Exception:
                        pass
                if hasattr(self, "btn_browse"):
                    try: self.btn_browse.configure(state=state)
                    except Exception: pass
                if hasattr(self, "cbo_quartile"):
                    try: self.cbo_quartile.configure(state="readonly" if enable else "disabled")
                    except Exception: pass
                if hasattr(self, "cbo_relevance"):
                    try: self.cbo_relevance.configure(state="readonly" if enable else "disabled")
                    except Exception: pass
                if hasattr(self, "btn_start"):
                    try: self.btn_start.configure(state=state)
                    except Exception: pass
                if hasattr(self, "btn_cancel"):
                    try: self.btn_cancel.configure(state="normal" if not enable else "disabled")
                    except Exception: pass
            self.root.after(0, _do)

        def _finalize_run(self, run_id: int):
            with lock_run_id:
                if run_id != active_run_id:
                    return
            self.enable_inputs(True)
            self.is_running = False
            self.update_stats()

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
                self.set_status("Cancelled by user", self.ROSE)

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

            r_sel = self.cbo_relevance.get()
            if "50%" in r_sel:
                min_rel = 0.50
            elif "70%" in r_sel:
                min_rel = 0.70
            elif "80%" in r_sel:
                min_rel = 0.80
            else:
                min_rel = 0.60

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

            # Clear previous grid and memory
            self.downloaded_papers_list.clear()
            self.tree_item_map.clear()
            for item in self.tree.get_children():
                self.tree.delete(item)
            self.lbl_tree_count.configure(
                text="Showing 0 downloaded paper(s)  ·  Double-click any row to view full-text PDF"
            )

            self.min_rel = min_rel
            self.start_time = time.time()
            self.lbl_card_downloads_num.configure(text=f"0 / {max_val}")
            self.lbl_card_downloads_sub.configure(text="0% (0s elapsed)")
            self.lbl_card_data_num.configure(text="0.00 MB")
            self.lbl_card_data_sub.configure(text=f"0.0 KB/s  ·  {MAX_WORKERS} threads")
            self.lbl_card_quality_num.configure(text="0 Q1  ·  0 Q2")
            self.lbl_card_quality_sub.configure(text="0% high-impact ratio")
            self.lbl_card_relevance_num.configure(text="100% Avg")
            self.lbl_card_relevance_sub.configure(text=f"Threshold: {int(min_rel * 100)}% match")

            folder = Path(save_path)
            try:
                folder.mkdir(parents=True, exist_ok=True)
                probe = folder / ".write_test"
                probe.touch()
                probe.unlink()
            except Exception as e:
                messagebox.showerror(
                    "Save Folder Unavailable",
                    f"Cannot write to the selected folder:\n{save_path}\n\n{e}\n\n"
                    "Pick a different destination and try again.",
                )
                return

            self.ctx = DownloadContext(current_run_id, max_val, folder)
            self.is_running = True
            self.enable_inputs(False)
            self._stats_ticker()

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
                        min_relevance=min_rel,
                        ctx=self.ctx,
                        paper_callback=self._on_paper_downloaded,
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
#  NEXT-GEN WEB APPLICATION SERVER & NATIVE DESKTOP APP WINDOW
# ══════════════════════════════════════════════════════════════════════════════

try:
    from flask import Flask, request, jsonify, Response, send_file
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False

class ResearchWebController:
    def __init__(self):
        self.lock = threading.Lock()
        self.is_running = False
        self.worker_thread: threading.Thread | None = None
        self.ctx: DownloadContext | None = None
        self.downloaded_papers: list[dict] = []
        self.current_folder = str(get_default_save_folder())
        self.progress = 0.0
        self.phase = "idle"
        self.status_text = "Ready — configure parameters and click Start Literature Harvest"
        self.target_articles = 50
        self.downloaded_count = 0
        self.total_bytes = 0
        self.q1_count = 0
        self.q2_count = 0
        self.q3_count = 0
        self.q4_count = 0
        self.avg_relevance = 1.0

    def get_status_dict(self) -> dict:
        with self.lock:
            return {
                "is_running": self.is_running,
                "progress": round(self.progress, 1),
                "phase": self.phase,
                "status_text": self.status_text,
                "downloaded": self.downloaded_count,
                "target": self.target_articles,
                "bytes": self.total_bytes,
                "q1": self.q1_count,
                "q2": self.q2_count,
                "q3": self.q3_count,
                "q4": self.q4_count,
                "avg_relevance": round(self.avg_relevance, 2),
                "folder": self.current_folder,
                "papers": list(self.downloaded_papers),
            }

    def start(self, payload: dict) -> dict:
        with self.lock:
            if self.is_running:
                return {"status": "error", "message": "Harvest already in progress."}

            keywords = (payload.get("keywords") or "").strip()
            if not keywords:
                return {"status": "error", "message": "Keywords are required."}

            focus = (payload.get("focus") or "").strip()
            y1 = str(payload.get("year_start") or "2023").strip()
            y2 = str(payload.get("year_end") or "2026").strip()
            max_val = int(payload.get("max_articles") or 50)
            q_filter = str(payload.get("quartile_filter") or "all_ranked").strip()
            mode = str(payload.get("mode") or "fresh").strip()
            min_rel = float(payload.get("min_relevance") or DEFAULT_MIN_RELEVANCE)
            folder_str = (payload.get("save_folder") or "").strip()
            folder = Path(folder_str) if folder_str else get_default_save_folder()

            try:
                folder.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                return {"status": "error", "message": f"Cannot write to folder: {e}"}

            global active_run_id
            with lock_run_id:
                active_run_id += 1
                current_run_id = active_run_id

            self.current_folder = str(folder)
            self.target_articles = max_val
            self.downloaded_count = 0
            self.total_bytes = 0
            self.q1_count = 0
            self.q2_count = 0
            self.q3_count = 0
            self.q4_count = 0
            self.avg_relevance = 1.0
            self.downloaded_papers = []
            self.progress = 5.0
            self.phase = "harvesting"
            self.status_text = f"Harvesting across 9 scholarly APIs for '{keywords}'..."
            self.is_running = True
            self.ctx = DownloadContext(current_run_id, max_val, folder)

            def _progress_cb(pct: float, msg: str):
                with self.lock:
                    self.progress = pct
                    self.status_text = msg
                    if pct < 25:
                        self.phase = "harvesting"
                    elif pct < 35:
                        self.phase = "filtering"
                    elif pct < 45:
                        self.phase = "ranking"
                    elif pct < 90:
                        self.phase = "downloading"
                    else:
                        self.phase = "citations"
                _broadcast_sse({
                    "type": "progress",
                    "percent": pct,
                    "phase": self.phase,
                    "message": msg
                })

            def _status_cb(msg: str, color: str = ""):
                with self.lock:
                    self.status_text = msg
                _broadcast_sse({
                    "type": "progress",
                    "percent": self.progress,
                    "phase": self.phase,
                    "message": msg
                })

            def _paper_cb(p: Paper, res: dict):
                size_kb = res.get("bytes", 0) // 1024
                size_str = f"{size_kb / 1024:.2f} MB" if size_kb > 1024 else f"{size_kb} KB"
                q = (p.quartile or "Unranked").upper()
                with self.lock:
                    self.downloaded_count += 1
                    self.total_bytes += res.get("bytes", 0)
                    if q == "Q1":
                        self.q1_count += 1
                    elif q == "Q2":
                        self.q2_count += 1
                    elif q == "Q3":
                        self.q3_count += 1
                    elif q == "Q4":
                        self.q4_count += 1

                    p_dict = {
                        "title": clean_title(p.title),
                        "authors": p.authors,
                        "year": p.year,
                        "journal": p.journal,
                        "doi": p.clean_doi(),
                        "citations": p.citations,
                        "quartile": p.quartile or "Unranked",
                        "relevance_score": p.relevance_score,
                        "source": p.source,
                        "pdf_path": p.pdf_path,
                        "size_str": size_str,
                        "abstract": p.abstract or "",
                    }
                    self.downloaded_papers.append(p_dict)
                    total_rel = sum(x["relevance_score"] for x in self.downloaded_papers)
                    self.avg_relevance = total_rel / len(self.downloaded_papers) if self.downloaded_papers else 1.0

                _broadcast_sse({"type": "paper", "paper": p_dict})

            def _thread_worker():
                try:
                    execute_research_workflow(
                        keywords=keywords,
                        focus=focus,
                        year_start=y1,
                        year_end=y2,
                        max_articles=max_val,
                        save_folder=folder,
                        quartile_filter=q_filter,
                        mode=mode,
                        min_relevance=min_rel,
                        ctx=self.ctx,
                        paper_callback=_paper_cb,
                        progress_callback=_progress_cb,
                        status_callback=_status_cb,
                    )
                except Exception as e:
                    _log(f"❌ Execution error: {e}", run_id=current_run_id)
                finally:
                    with self.lock:
                        self.is_running = False
                        self.phase = "complete"
                        self.progress = 100.0
                        self.status_text = f"Harvest complete — {self.downloaded_count} PDFs saved to {self.current_folder}"
                    _broadcast_sse({"type": "complete", "status": self.get_status_dict()})

            self.worker_thread = threading.Thread(target=_thread_worker, daemon=True)
            self.worker_thread.start()
            return {"status": "ok", "message": "Harvest started"}

    def cancel(self) -> dict:
        with self.lock:
            if self.ctx:
                self.ctx.cancellation_event.set()
            self.is_running = False
            self.status_text = "Harvest cancelled by user."
        _broadcast_sse({"type": "progress", "percent": self.progress, "phase": "cancelled", "message": "Harvest cancelled by user."})
        return {"status": "ok", "message": "Harvest cancelled"}

web_controller = ResearchWebController()

def create_flask_app():
    if not HAS_FLASK:
        return None

    app = Flask("ArticlesDownloader", static_folder=None)
    app.config["JSON_AS_ASCII"] = False

    @app.after_request
    def add_headers(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return resp

    @app.route("/")
    def index():
        ui_file = SCRIPT_DIR / "articles_ui.html"
        if ui_file.exists():
            return ui_file.read_text(encoding="utf-8")
        return "<h1>Articles Downloader v10 Ultra Pro</h1><p>articles_ui.html not found.</p>"

    @app.route("/api/status")
    def api_status():
        return jsonify(web_controller.get_status_dict())

    @app.route("/api/start", methods=["POST"])
    def api_start():
        data = request.get_json(silent=True) or {}
        return jsonify(web_controller.start(data))

    @app.route("/api/cancel", methods=["POST"])
    def api_cancel():
        return jsonify(web_controller.cancel())

    @app.route("/api/papers")
    def api_papers():
        with web_controller.lock:
            return jsonify(list(web_controller.downloaded_papers))

    @app.route("/api/history")
    def api_history():
        items = []
        try:
            conn = _history_conn()
            rows = conn.execute("""
                SELECT query, COUNT(DISTINCT identifier) as cnt, MAX(date) as last_date
                FROM history
                WHERE query IS NOT NULL AND query != ''
                GROUP BY query
                ORDER BY MAX(date) DESC, cnt DESC
                LIMIT 40
            """).fetchall()
            conn.close()
            for r in rows:
                items.append({"query": r[0], "count": r[1], "date": r[2] or "Recent"})
        except Exception as e:
            items = []
        return jsonify(items)

    @app.route("/api/browse_folder", methods=["POST"])
    def api_browse_folder():
        folder = choose_folder_dialog(web_controller.current_folder)
        return jsonify({"folder": folder})

    @app.route("/api/open_folder", methods=["POST"])
    def api_open_folder():
        data = request.get_json(silent=True) or {}
        folder_str = data.get("folder") or web_controller.current_folder
        if folder_str and os.path.isdir(folder_str):
            try:
                if sys.platform == "win32":
                    os.startfile(folder_str)
                else:
                    subprocess.Popen(["xdg-open", folder_str])
                return jsonify({"status": "ok"})
            except Exception as e:
                return jsonify({"status": "error", "message": str(e)}), 500
        return jsonify({"status": "error", "message": "Directory does not exist"}), 400

    @app.route("/api/open_pdf", methods=["POST"])
    def api_open_pdf():
        data = request.get_json(silent=True) or {}
        path = data.get("path")
        if path and os.path.exists(path):
            try:
                if sys.platform == "win32":
                    os.startfile(path)
                else:
                    subprocess.Popen(["xdg-open", path])
                return jsonify({"status": "ok"})
            except Exception as e:
                return jsonify({"status": "error", "message": str(e)}), 500
        return jsonify({"status": "error", "message": "PDF file does not exist"}), 404

    @app.route("/api/reveal_pdf", methods=["POST"])
    def api_reveal_pdf():
        data = request.get_json(silent=True) or {}
        path = data.get("path")
        if path and os.path.exists(path):
            try:
                if sys.platform == "win32":
                    subprocess.Popen(f'explorer /select,"{path}"')
                else:
                    subprocess.Popen(["xdg-open", str(Path(path).parent)])
                return jsonify({"status": "ok"})
            except Exception as e:
                return jsonify({"status": "error", "message": str(e)}), 500
        return jsonify({"status": "error", "message": "File does not exist"}), 404

    @app.route("/api/pdf_file")
    def api_pdf_file():
        path = request.args.get("path", "")
        if path and os.path.exists(path) and path.lower().endswith(".pdf"):
            return send_file(path, mimetype="application/pdf")
        return "PDF file not found", 404

    @app.route("/api/export/<fmt>")
    def api_export(fmt):
        folder_str = request.args.get("folder") or web_controller.current_folder
        folder = Path(folder_str) if folder_str else get_default_save_folder()
        fname_map = {
            "bib": "references.bib",
            "ris": "references.ris",
            "apa": "references_APA.txt",
            "csv": "results.csv",
            "json": "corpus_metadata.json",
        }
        target_name = fname_map.get(fmt.lower())
        if target_name:
            file_path = folder / target_name
            if file_path.exists():
                return send_file(str(file_path), as_attachment=True, download_name=target_name)
        return "Requested export file does not exist in destination folder yet.", 404

    @app.route("/api/stream")
    def api_stream():
        def event_stream():
            q = queue.Queue(maxsize=1000)
            with _subscribers_lock:
                _sse_subscribers.append(q)
            try:
                init_event = {"type": "progress", "percent": web_controller.progress,
                              "phase": web_controller.phase, "message": web_controller.status_text}
                yield f"data: {json.dumps(init_event)}\n\n"
                while True:
                    try:
                        ev = q.get(timeout=25.0)
                        yield f"data: {json.dumps(ev)}\n\n"
                    except queue.Empty:
                        yield f": heartbeat\n\n"
            finally:
                with _subscribers_lock:
                    if q in _sse_subscribers:
                        _sse_subscribers.remove(q)

        return Response(event_stream(), mimetype="text/event-stream")

    return app

def choose_folder_dialog(initial_dir=""):
    """Native Windows folder browser dialog without leaving background windows."""
    init_path = initial_dir or str(get_default_save_folder())
    cmd = (
        'Add-Type -AssemblyName System.Windows.Forms;'
        '$f = New-Object System.Windows.Forms.FolderBrowserDialog;'
        '$f.Description = "Select Research PDF Save Directory";'
        f'$f.SelectedPath = "{init_path}";'
        'if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $f.SelectedPath }'
    )
    try:
        res = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=30)
        p = res.stdout.strip()
        if p and os.path.isdir(p):
            return p
    except Exception:
        pass
    if HAS_TKINTER:
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            folder = filedialog.askdirectory(initialdir=init_path)
            root.destroy()
            if folder and os.path.isdir(folder):
                return folder
        except Exception:
            pass
    return init_path

def find_available_port(start_port: int = 5080) -> int:
    import socket
    for port in range(start_port, start_port + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('127.0.0.1', port)) != 0:
                return port
    return start_port

def launch_native_window(url: str):
    """Launch the Web App in standalone native application window mode."""
    edge_paths = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    chrome_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for exe in edge_paths + chrome_paths:
        if os.path.exists(exe):
            try:
                subprocess.Popen([
                    exe,
                    f"--app={url}",
                    "--window-size=1360,920",
                    "--new-window",
                ])
                return True
            except Exception:
                pass
    import webbrowser
    webbrowser.open(url)
    return True

def run_web_app(port: int = 5080, open_window: bool = True):
    """Run local Flask server and launch the native desktop application window."""
    app = create_flask_app()
    if not app:
        print("Flask is not installed. Falling back to Tkinter GUI.")
        if HAS_TKINTER:
            ResearchAppDashboard().root.mainloop()
        return

    actual_port = find_available_port(port)
    url = f"http://127.0.0.1:{actual_port}"
    print(f"\n⚡ Articles Downloader v10 Ultra Pro — Obsidian 4K UI Server")
    print(f"  🔗 Local Loopback: {url}")

    if open_window:
        def _delayed_launch():
            time.sleep(0.8)
            launch_native_window(url)
        threading.Thread(target=_delayed_launch, daemon=True).start()

    # Run loopback server
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    app.run(host="127.0.0.1", port=actual_port, threaded=True, debug=False)

# ══════════════════════════════════════════════════════════════════════════════
#  CLI ARGUMENT PARSER & MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description="Articles Downloader v10 Ultra Pro — High-Performance Research Literature Harvester",
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
    parser.add_argument("--min-relevance", "-r", type=float, default=DEFAULT_MIN_RELEVANCE,
                        help="Minimum relevance match threshold between 0.0 and 1.0 (default: 0.60 for 60%%)")
    parser.add_argument("--cli", "--no-gui", action="store_true", help="Run in headless command-line mode without GUI")
    parser.add_argument("--tk", action="store_true", help="Run classic Tkinter GUI instead of modern Web App")
    parser.add_argument("--web", action="store_true", help="Run Web App in browser tab rather than app window")
    parser.add_argument("--port", type=int, default=5080, help="Local server port (default: 5080)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser/window automatically")
    return parser.parse_args()

def main():
    args = parse_args()

    # 1. Headless CLI mode
    if args.cli or (args.keywords and not (HAS_FLASK or HAS_TKINTER)):
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
            min_relevance=args.min_relevance,
            ctx=ctx,
        )
        return

    # 2. Classic Tkinter GUI mode (if explicitly requested with --tk)
    if args.tk and HAS_TKINTER:
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
        return

    # 3. Next-Gen Obsidian 4K Modern UI (Default for desktop launcher & interactive use)
    if HAS_FLASK:
        run_web_app(
            port=args.port,
            open_window=not args.no_browser and not args.web
        )
    elif HAS_TKINTER:
        app = ResearchAppDashboard()
        app.root.mainloop()
    else:
        print("Neither Flask nor Tkinter is available. Please run with --cli flag.")

if __name__ == "__main__":
    main()
