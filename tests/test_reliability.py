"""Regression tests for download safety, rank order, live-progress delivery and input cleaning."""
import queue
import threading
import time

import pytest

import Articles_v2 as art
from conftest import FakeResponse, make_pdf


# ── PDF validation ────────────────────────────────────────────────────────────

def test_truncated_pdf_is_rejected():
    whole = make_pdf(20_000)
    assert art.pdf_problem(whole) == ""
    cut_off = whole[: len(whole) // 2]                     # transfer dropped half way
    assert "incomplete" in art.pdf_problem(cut_off)
    assert "too small" in art.pdf_problem(b"%PDF-1.7\n%%EOF\n")
    assert art.pdf_problem(b"<html>login</html>" * 1000) == "not a PDF"


def test_truncated_pdf_is_not_saved(tmp_path, monkeypatch):
    half = make_pdf(40_000)[:20_000]
    monkeypatch.setattr(art, "_fetch_document", lambda url, profile, ctx: FakeResponse(
        half, headers={"Content-Type": "application/pdf"}, url=url))
    ctx = art.DownloadContext(1, 1, tmp_path)
    res = art.download_article(("https://repo.example.org/a.pdf", "Cut off paper", "", tmp_path, ctx, None, []))
    assert not res["success"] and "incomplete" in res["error"]
    assert list(tmp_path.iterdir()) == []                   # nothing half-written left behind


# ── capped streaming ──────────────────────────────────────────────────────────

class _Stream:
    def __init__(self, chunks, headers=None):
        self.chunks, self.headers, self.status_code, self.url = chunks, headers or {}, 200, "https://x/y.pdf"
        self.served = 0
        self.closed = False

    def iter_content(self, chunk_size=0):
        for c in self.chunks:
            self.served += 1
            yield c

    def close(self):
        self.closed = True


def test_oversized_download_is_refused_before_it_fills_memory():
    declared = _Stream([b"x"], headers={"Content-Length": str(art.MAX_PDF_BYTES + 1)})
    with pytest.raises(art.DocumentTooLarge):
        art._read_capped(declared)
    assert declared.served == 0                              # refused from the header alone

    endless = _Stream([b"x" * 1024] * 100)                   # no Content-Length, keeps sending
    with pytest.raises(art.DocumentTooLarge):
        art._read_capped(endless, limit=10 * 1024)
    assert endless.served <= 11                              # stopped right after the limit

    assert art._read_capped(_Stream([b"ab", b"", b"cd"])) == b"abcd"


def test_fetch_reuses_one_session_per_thread_and_closes_responses(monkeypatch):
    opened, streams = [], []

    class Session:
        def get(self, url, **kwargs):
            assert kwargs["stream"] is True
            streams.append(_Stream([b"data"]))
            return streams[-1]

        def close(self):
            pass

    monkeypatch.setattr(art, "open_stealth_session", lambda profile="x": opened.append(profile) or Session())
    art._thread_local.download_sessions = {}
    for _ in range(3):
        doc = art._fetch_document("https://x/y.pdf", "chrome120", None)
        assert (doc.content, doc.status_code) == (b"data", 200)
    assert opened == ["chrome120"]                           # one handshake, not three
    assert all(s.closed for s in streams)


# ── saving ────────────────────────────────────────────────────────────────────

def test_identical_pdf_is_reused_instead_of_duplicated(tmp_path):
    pdf = make_pdf(20_000)
    first, reused_first = art.save_pdf(tmp_path, "Zinc: air battery?", pdf)
    again, reused_again = art.save_pdf(tmp_path, "Zinc: air battery?", pdf)
    assert (reused_first, reused_again) == (False, True) and first == again
    other, reused_other = art.save_pdf(tmp_path, "Zinc: air battery?", make_pdf(20_001))
    assert not reused_other and other != first               # different content keeps both
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Zinc_ air battery_.pdf", "Zinc_ air battery__1.pdf"]


def test_failed_write_leaves_no_partial_file(tmp_path, monkeypatch):
    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(art.os, "replace", boom)
    with pytest.raises(OSError):
        art.save_pdf(tmp_path, "Paper", make_pdf())
    assert list(tmp_path.iterdir()) == []


# ── rank-faithful downloading ─────────────────────────────────────────────────

def test_best_ranked_papers_win_even_when_lower_ranked_sites_are_faster(tmp_path, monkeypatch):
    """Rank 1-2 answer slowly, ranks 3-12 instantly. The two saved PDFs must still be ranks 1-2."""
    papers = [art.Paper(url=f"https://repo.example.org/ok/{i}.pdf", title=f"Ranked paper number {i}",
                        doi=f"10.1000/r{i}", quartile="Q1", citations=100 - i) for i in range(1, 13)]
    started = []

    def fake_fetch(url, profile, ctx):
        started.append(url)
        if url.endswith(("/1.pdf", "/2.pdf")):
            time.sleep(0.4)
        return FakeResponse(make_pdf(), headers={"Content-Type": "application/pdf"}, url=url)

    monkeypatch.setattr(art, "_fetch_document", fake_fetch)
    got = art.download_papers(papers, tmp_path, 2, ctx=art.DownloadContext(1, 2, tmp_path))
    assert sorted(p.title for p in got) == ["Ranked paper number 1", "Ranked paper number 2"]
    assert 2 < len(started) <= 2 + art.DOWNLOAD_LOOKAHEAD     # a few were fetched ahead for speed...
    assert len(list(tmp_path.rglob("*.pdf"))) == 2            # ...but only the two best were saved


def test_a_stuck_better_ranked_paper_cannot_block_the_run_forever(tmp_path, monkeypatch):
    papers = [art.Paper(url=f"https://repo.example.org/ok/{i}.pdf", title=f"Ranked paper number {i}",
                        quartile="Q1", citations=100 - i) for i in range(1, 4)]
    release = threading.Event()

    def fake_fetch(url, profile, ctx):
        if url.endswith("/1.pdf"):
            release.wait(5)                                   # rank 1 hangs
            raise ValueError("HTTP 504")
        return FakeResponse(make_pdf(), headers={"Content-Type": "application/pdf"}, url=url)

    monkeypatch.setattr(art, "_fetch_document", fake_fetch)
    monkeypatch.setattr(art, "RANK_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(art, "MAX_REQUESTS_PER_ARTICLE", 1)
    t0 = time.monotonic()
    got = art.download_papers(papers, tmp_path, 1, ctx=art.DownloadContext(1, 1, tmp_path))
    release.set()
    assert [p.title for p in got] == ["Ranked paper number 2"]
    assert time.monotonic() - t0 < 4


def test_lower_ranked_papers_fill_slots_when_better_ones_fail(tmp_path, monkeypatch):
    papers = [art.Paper(url=f"https://repo.example.org/{'bad' if i <= 3 else 'ok'}/{i}.pdf",
                        title=f"Ranked paper number {i}", quartile="Q1", citations=100 - i)
              for i in range(1, 9)]

    def fake_fetch(url, profile, ctx):
        if "/ok/" in url:
            return FakeResponse(make_pdf(), headers={"Content-Type": "application/pdf"}, url=url)
        return FakeResponse(b"<html>no</html>", status_code=403, headers={"Content-Type": "text/html"}, url=url)

    monkeypatch.setattr(art, "_fetch_document", fake_fetch)
    logs = []
    monkeypatch.setattr(art, "_log", lambda msg, *a, **k: logs.append(msg))
    got = art.download_papers(papers, tmp_path, 2, ctx=art.DownloadContext(1, 2, tmp_path))
    assert sorted(p.title for p in got) == ["Ranked paper number 4", "Ranked paper number 5"]
    assert any("Why papers failed" in m and "HTTP 403" in m for m in logs)


# ── live progress stream ──────────────────────────────────────────────────────

def test_slow_browser_tab_keeps_receiving_events(monkeypatch):
    q = queue.Queue(maxsize=3)
    monkeypatch.setattr(art, "_sse_subscribers", [q])
    for i in range(10):
        art._broadcast_sse({"n": i})
    assert q in art._sse_subscribers                         # still subscribed
    assert [q.get_nowait()["n"] for _ in range(3)] == [7, 8, 9]   # newest events kept


def test_cancellable_sleep_returns_immediately_on_cancel(tmp_path):
    ctx = art.DownloadContext(1, 1, tmp_path)
    threading.Timer(0.05, ctx.cancellation_event.set).start()
    t0 = time.monotonic()
    assert art.sleep_check_cancel(5.0, ctx) is True
    assert time.monotonic() - t0 < 1.0
    assert art.sleep_check_cancel(0.01, art.DownloadContext(2, 1, tmp_path)) is False


# ── input cleaning ────────────────────────────────────────────────────────────

def test_year_range_is_cleaned():
    y = art.CURRENT_YEAR
    assert art.normalize_years("2026", "2020") == ("2020", "2026")          # reversed
    assert art.normalize_years("abc", None) == (str(y - 3), str(y))         # junk -> defaults
    assert art.normalize_years(" 2021 ", "9999") == ("2021", str(y + 1))    # clamped


def test_bad_years_from_the_cli_do_not_break_the_sources(tmp_path, fake_sources):
    got = art.execute_research_workflow(keywords="zinc air battery electrocatalyst", year_start="latest",
                                        year_end="", max_articles=5, save_folder=tmp_path, quartile_filter="all")
    assert len(got) == 3


def test_query_syntax_characters_are_removed_for_query_language_apis():
    assert art.plain_query('Zinc (air) "battery": a [review]') == "Zinc air battery a review"


def test_paper_tolerates_bad_numbers_from_apis():
    p = art.Paper(title="T", citations="12", year=2024, doi=None)
    assert (p.citations, p.year, p.doi) == (12, "2024", "")
    assert art.Paper(title="T", citations="<img src=x>").citations == 0
    assert art.Paper(title="T", citations=None).citations == 0
    papers = [art.Paper(title="A", citations="3"), art.Paper(title="B", citations=None)]
    assert [p.title for p in art.rank_papers(papers, "citations")] == ["A", "B"]


def test_web_payload_never_carries_a_script_url():
    assert art.paper_to_dict(art.Paper(title="T", url="javascript:alert(1)"))["url"] == ""
    assert art.paper_to_dict(art.Paper(title="T", url="https://doi.org/10.1/x"))["url"] == "https://doi.org/10.1/x"


def test_readable_failure_reasons():
    assert art.short_error("Failed to perform, curl: (6) Could not resolve host: x.org") == \
        "site address could not be found"
    assert art.short_error("Failed to perform, curl: (28) Operation timed out after 45001 ms") == \
        "site did not answer in time"
    assert art.short_error(ValueError("HTTP 403")) == "HTTP 403"


def test_history_connection_is_closed_even_when_a_query_fails(monkeypatch):
    closed = []
    real = art._history_conn

    class Tracked:
        def __init__(self):
            self.conn = real()

        def execute(self, *a, **k):
            raise art.sqlite3.OperationalError("boom")

        def commit(self):
            self.conn.commit()

        def close(self):
            closed.append(True)
            self.conn.close()

    monkeypatch.setattr(art, "_history_conn", Tracked)
    assert art.query_seen_count("anything") == 0
    assert closed == [True]
