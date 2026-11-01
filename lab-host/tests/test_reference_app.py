"""The reference route: HMAC, 404 for unknown sessions, a cleaned terminal tail."""
import hashlib, hmac, os, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "broker"))


def test_route(tmp_path, monkeypatch):
    monkeypatch.setenv("TUTOR_SECRET", "s" * 32); monkeypatch.setenv("RUN_DIR", str(tmp_path))
    import importlib, reference_app
    importlib.reload(reference_app)
    from fastapi.testclient import TestClient
    c = TestClient(reference_app.app)
    sid = "0" * 32
    path = f"/internal/v1/sessions/{sid}/tutor"

    def get(p, secret="s" * 32):
        ts = str(int(time.time()))
        sig = hmac.new(secret.encode(), f"{ts}\nGET\n{p}\n".encode(), hashlib.sha256).hexdigest()
        return c.get(p, headers={"X-Timestamp": ts, "X-Signature": sig})

    assert c.get(path).status_code == 401 and get(path, "wrong").status_code == 401
    assert get(path).status_code == 404
    (tmp_path / sid / "term").mkdir(parents=True)
    (tmp_path / sid / "term" / "typescript").write_bytes(b"\x1b[1mcorvane-r3#\x1b[0mshow ip int brief\r\nEthernet0/2 up\r\n")
    d = get(path).json()
    assert d["terminal_tail"] == "corvane-r3#show ip int brief\nEthernet0/2 up" and d["capturing"] is True
