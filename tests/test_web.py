"""Flask API tests: local-only guards, validation, and a full preview -> select -> download flow."""
import time

import pytest

import Articles_v2 as art

flask = pytest.importorskip("flask")


@pytest.fixture
def client(monkeypatch):
    ctl = art.ResearchWebController()
    monkeypatch.setattr(art, "web_controller", ctl)
    app = art.create_flask_app()
    app.testing = True
    c = app.test_client()
    c.ctl = ctl
    return c


def post(c, path, body=None, **kw):
    return c.post(path, json=body or {}, **kw)


def wait_idle(c, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        st = c.get("/api/status").get_json()
        if not st["is_running"]:
            return st
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_index_and_config(client):
    r = client.get("/")
    assert r.status_code == 200 and b"Articles Downloader" in r.data and b"/api/stream" in r.data
    cfg = client.get("/api/config").get_json()
    assert cfg["version"] == art.APP_VERSION and len(cfg["sources"]) >= 2
    assert cfg["allow_scihub_default"] is False


def test_rejects_foreign_host_and_origin(client):
    assert client.get("/api/status", headers={"Host": "evil.example"}).status_code == 403
    assert post(client, "/api/cancel", headers={"Origin": "https://evil.example"}).status_code == 403
    assert post(client, "/api/cancel", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/cancel", data="x=1",
                       content_type="application/x-www-form-urlencoded").status_code == 415
    assert post(client, "/api/cancel", headers={"Origin": "http://127.0.0.1:5080"}).status_code == 200
    assert "Access-Control-Allow-Origin" not in client.get("/api/status").headers


def test_file_access_limited_to_download_folders(client, tmp_path):
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-1.4 secret")
    assert client.get("/api/pdf_file", query_string={"path": str(outside)}).status_code == 404
    assert post(client, "/api/open_pdf", {"path": str(outside)}).status_code == 404
    inside_dir = art.get_default_save_folder()
    inside_dir.mkdir(parents=True, exist_ok=True)
    inside = inside_dir / "ok.pdf"
    inside.write_bytes(b"%PDF-1.4 fine")
    assert client.get("/api/pdf_file", query_string={"path": str(inside)}).status_code == 200
    traversal = str(inside_dir / ".." / "secret.pdf")
    assert client.get("/api/pdf_file", query_string={"path": traversal}).status_code == 404


def test_start_validation(client):
    r = post(client, "/api/start", {}).get_json()
    assert r["status"] == "error" and "keywords" in r["message"].lower()
    r = post(client, "/api/start_dois", {"dois": "no dois here"}).get_json()
    assert r["status"] == "error"
    r = post(client, "/api/download_selected", {"ids": ["nope"]}).get_json()
    assert r["status"] == "error"
    r = post(client, "/api/cite", {"fmt": "docx"})
    assert r.status_code == 400


def test_search_flow_and_citations(client, fake_sources, tmp_path):
    folder = tmp_path / "web"
    r = post(client, "/api/start", {"keywords": "zinc air battery electrocatalyst", "max_articles": "5",
                                    "quartile_filter": "all", "save_folder": str(folder),
                                    "year_start": 2030, "year_end": 1999}).get_json()
    assert r["status"] == "ok"
    st = wait_idle(client)
    assert st["phase"] == "complete" and st["downloaded"] == 3 and st["q1"] == 2
    assert st["progress"] == 100 and st["bytes"] > 0 and len(st["papers"]) == 3
    cite = post(client, "/api/cite", {"fmt": "bib"}).get_json()
    assert cite["count"] == 3 and cite["text"].count("@") == 3
    one = st["papers"][0]
    assert post(client, "/api/cite", {"fmt": "apa", "ids": [one["id"]]}).get_json()["count"] == 1
    exp = client.get("/api/export/bib")
    assert exp.status_code == 200 and b"@" in exp.data
    assert client.get("/api/pdf_file", query_string={"path": one["pdf_path"]}).status_code == 200
    hist = client.get("/api/history").get_json()
    assert hist and hist[0]["count"] == 3
    papers = client.get("/api/history/papers", query_string={"query": hist[0]["query"]}).get_json()
    assert len(papers) == 3
    assert post(client, "/api/history/delete", {"query": hist[0]["query"]}).get_json()["deleted"] == 3


def test_preview_then_download_selected(client, fake_sources, tmp_path):
    r = post(client, "/api/start", {"keywords": "zinc air battery electrocatalyst", "preview": True,
                                    "quartile_filter": "all", "save_folder": str(tmp_path / "p")}).get_json()
    assert r["status"] == "ok"
    st = wait_idle(client)
    assert st["kind"] == "preview" and st["downloaded"] == 0 and st["candidates"] == 4
    cands = client.get("/api/candidates").get_json()
    assert not fake_sources["fetched"]
    pick = [c["id"] for c in cands if c["title"].startswith("Bifunctional")]
    r = post(client, "/api/download_selected", {"ids": pick}).get_json()
    assert r["status"] == "ok"
    st = wait_idle(client)
    assert st["downloaded"] == 1 and st["papers"][0]["title"].startswith("Bifunctional")
    cands = client.get("/api/candidates").get_json()
    assert sum(1 for c in cands if c["pdf_path"]) == 1


def test_only_one_job_at_a_time(client, fake_sources, monkeypatch, tmp_path):
    import threading
    gate = threading.Event()
    orig = art.harvest_and_rank

    def slow(*a, **k):
        gate.wait(5)
        return orig(*a, **k)

    monkeypatch.setattr(art, "harvest_and_rank", slow)
    body = {"keywords": "zinc air battery electrocatalyst", "save_folder": str(tmp_path / "x")}
    assert post(client, "/api/start", body).get_json()["status"] == "ok"
    assert post(client, "/api/start", body).get_json()["status"] == "error"
    assert post(client, "/api/cancel").get_json()["status"] == "ok"
    # still running until the worker actually exits -> a new start is refused
    assert post(client, "/api/start", body).get_json()["status"] == "error"
    gate.set()
    st = wait_idle(client)
    assert st["phase"] == "cancelled"
