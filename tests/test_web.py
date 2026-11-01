import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from lab_tutor.config import Settings
from lab_tutor.muse import MuseError, stream_chat
from lab_tutor.web import RateLimiter

from .conftest import SID, make_app

HINT_BITS = ["send_config_set", "save_config", "os.makedirs", "Same script, second device"]


def events(text):
    out = []
    for blk in text.strip().split("\n\n"):
        ev, data = None, None
        for l in blk.split("\n"):
            if l.startswith("event: "):
                ev = l[7:]
            elif l.startswith("data: "):
                data = json.loads(l[6:])
        out.append((ev, data))
    return out


def test_state_has_no_hints(client):
    r = client.get("/tutor/b1/state")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    j = r.json()
    assert j["status"] == "ok" and j["score"] == 0 and j["max"] == 7 and j["mock"] is True
    assert len(j["checks"]) == 7 and set(j["checks"][0]) == {"name", "ok"}
    for k in ("terminal_updated_at", "capturing", "generated_at", "lab_id", "passed"):
        assert k in j
    assert "hint" not in r.text
    for bit in HINT_BITS:
        assert bit not in r.text


def test_page(client):
    r = client.get("/tutor/b1")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-src 'self'" in csp
    nonce = csp.split("script-src 'nonce-")[1].split("'")[0]
    assert r.text.count(f'nonce="{nonce}"') >= 2
    assert 'src="/term"' in r.text and "innerHTML" not in r.text
    for bit in HINT_BITS:
        assert bit not in r.text
    assert "SIMULATED (mock mode)" in r.text
    assert client.get("/tutor/zzz").status_code == 404
    assert client.get("/tutor/zzz/state").status_code == 404
    assert client.post("/tutor/zzz/ask", json={"action": "hint"}).status_code == 404


def test_page_escapes_guac_url(settings):
    from fastapi import FastAPI
    from lab_tutor.web import TutorTarget, make_router

    async def resolve(req, b):
        return TutorTarget(SID, "x", "y", '/t"><script>alert(1)</script>')

    app = FastAPI()
    app.include_router(make_router(settings, resolve))
    t = TestClient(app).get("/tutor/x").text
    assert "<script>alert(1)" not in t


def test_ask_streams_mock(client):
    r = client.post("/tutor/b1/ask", json={"action": "hint"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    ev = events(r.text)
    assert ev[-1][0] == "done" and ev[-1][1]["level"] == 1
    text = "".join(d["text"] for e, d in ev if e == "delta")
    assert text.startswith("[Hint 1]")
    r = client.post("/tutor/b1/ask", json={"action": "hint"})
    assert events(r.text)[-1][1]["level"] == 2
    t2 = "".join(d["text"] for e, d in events(r.text) if e == "delta")
    assert t2.startswith("[Hint 2]")
    for a, q in (("what_went_wrong", None), ("explain_output", None), ("ask", "why?")):
        r = client.post("/tutor/b1/ask", json={"action": a, "question": q})
        t = "".join(d["text"] for e, d in events(r.text) if e == "delta")
        assert t.startswith("[Hint") and "send_config_set" not in t and "`" not in t


def test_ask_mock_all_pass_says_done(client):
    client.router_obj.mock_broker.set_variant(2)
    r = client.post("/tutor/b1/ask", json={"action": "what_went_wrong"})
    t = "".join(d["text"] for e, d in events(r.text) if e == "delta")
    assert t.startswith("[Done]")


@pytest.mark.parametrize("body", [{}, {"action": "nope"}, {"action": "ask"}, {"action": "ask", "question": 5}])
def test_ask_bad_request(client, body):
    assert client.post("/tutor/b1/ask", json=body).status_code == 400
    assert client.post("/tutor/b1/ask", content=b"notjson").status_code == 400


def test_rate_limiter_unit():
    L = RateLimiter(10)
    assert L.acquire("k", 0) is None
    assert L.acquire("k", 1) == "busy"
    L.release("k")
    for i in range(9):
        assert L.acquire("k", 2 + i) is None
        L.release("k")
    assert L.acquire("k", 20) == "rate"
    assert L.acquire("k", 70) is None  # window rolled
    assert L.acquire("other", 20) is None


def test_ask_rate_limit_http(client):
    for _ in range(10):
        assert client.post("/tutor/b2/ask", json={"action": "hint"}).status_code == 200
    r = client.post("/tutor/b2/ask", json={"action": "hint"})
    assert r.status_code == 429 and r.headers["cache-control"] == "no-store"
    assert client.post("/tutor/b1/ask", json={"action": "hint"}).status_code == 200


def test_state_broker_errors(settings):
    s = Settings(mock=False, api_key="k", spend_file=settings.spend_file)
    for code, expect in ((409, 200), (404, 404), (500, 502)):
        def handler(req, code=code):
            return httpx.Response(code, json={"session_status": "starting"})

        hc = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app, _ = make_app(s, http_client=hc)
        r = TestClient(app).get("/tutor/b1/state")
        assert r.status_code == expect
        if code == 409:
            assert r.json()["status"] == "not_ready"


KEY = "sk-SUPERSECRETKEY-12345"


def _live_app(tmp_path, handler):
    s = Settings(mock=False, api_key=KEY, base_url="http://m.test/v1", spend_file=str(tmp_path / "s.json"),
                 price_in_per_mtok=1.0, price_out_per_mtok=2.0)
    sample = json.load(open(__import__("lab_tutor.broker", fromlist=["x"]).FIXTURE_DIR / "sample-lab03.json"))

    def route(req: httpx.Request):
        if "/internal/v1/" in req.url.path:
            return httpx.Response(200, json=sample)
        return handler(req)

    hc = httpx.AsyncClient(transport=httpx.MockTransport(route))
    app, _ = make_app(s, http_client=hc)
    return TestClient(app), s


def test_live_flow_records_spend(tmp_path):
    raw = (
        b'data: {"choices":[{"delta":{"content":"[Hint 1] look"}}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":1000,"completion_tokens":100}}\n\n'
        b"data: [DONE]\n\n"
    )

    def h(req):
        assert req.headers["authorization"] == f"Bearer {KEY}"
        body = json.loads(req.content)
        assert body["messages"][0]["role"] == "system"
        assert "UNIQUE" not in body["messages"][0]["content"]
        return httpx.Response(200, content=raw)

    c, s = _live_app(tmp_path, h)
    r = c.post("/tutor/b1/ask", json={"action": "hint"})
    ev = events(r.text)
    assert ev[-1][0] == "done" and ev[-1][1]["cost_usd"] == pytest.approx(0.0012)
    assert json.load(open(s.spend_file))["calls"] == 1
    assert KEY not in r.text


def test_spend_cap_blocks_ask(tmp_path):
    c, s = _live_app(tmp_path, lambda req: pytest.fail("must not call upstream"))
    json.dump({"total_usd": 99}, open(s.spend_file, "w"))
    r = c.post("/tutor/b1/ask", json={"action": "hint"})
    ev = events(r.text)
    assert ev[-1][0] == "error" and "spend" in ev[-1][1]["message"]


def test_key_never_leaks_on_upstream_failure(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)

    def h(req):
        if req.url.path.endswith("chat/completions"):
            return httpx.Response(500, text=f"boom Authorization: Bearer {KEY}")
        raise AssertionError

    c, _ = _live_app(tmp_path, h)
    r = c.post("/tutor/b1/ask", json={"action": "hint"})
    assert KEY not in r.text and KEY not in r.headers.__str__()
    assert events(r.text)[-1][0] == "error"
    assert KEY not in caplog.text

    def h2(req):
        if req.url.path.endswith("chat/completions"):
            raise httpx.ConnectError(f"cannot reach, hdr Bearer {KEY}", request=req)
        raise AssertionError

    c, _ = _live_app(tmp_path, h2)
    r = c.post("/tutor/b1/ask", json={"action": "hint"})
    assert KEY not in r.text and KEY not in caplog.text
    assert events(r.text)[-1][1]["message"] == "The tutor is unavailable right now."


def test_muse_error_has_no_key():
    import asyncio

    s = Settings(api_key=KEY, base_url="http://m.test/v1")

    def h(req):
        return httpx.Response(401, text=KEY)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
            async for _ in stream_chat(s, [], c):
                pass

    with pytest.raises(MuseError) as e:
        asyncio.run(go())
    assert KEY not in str(e.value) and KEY not in repr(s)


def test_standalone_app():
    from lab_tutor.app_standalone import app

    c = TestClient(app)
    assert c.get("/tutor/demo").status_code == 200
    assert c.get("/tutor/other").status_code == 404
    t = c.get("/tutor-demo-terminal").text
    assert "<pre>" in t and "&lt;" in t or "&#x27;" in t
    assert c.get("/tutor/demo/state").json()["score"] == 0


def test_live_broker_with_mock_replies_reads_the_real_broker(settings):
    import dataclasses
    from lab_tutor.web import make_router
    s = dataclasses.replace(settings, live_broker=True)

    async def resolve(request, bid):
        return None
    assert make_router(s, resolve).mock_broker is None
    assert make_router(settings, resolve).mock_broker is not None


def test_router_prompt_detection():
    from lab_tutor.web import ROUTER_PROMPT
    for t in ["corvane-r3#show ip int brief", "R1>", "R1(config-if)#no shut"]:
        assert ROUTER_PROMPT.search("student@workspace:~$ ssh r\n" + t)
    for t in ["student@workspace:~$ ", "Score: 0/2", "  [--] corvane-r3 Ethernet0/2 is up/up", ""]:
        assert not ROUTER_PROMPT.search(t)


def test_iol01_mock_set_and_boot_note(settings):
    import asyncio, dataclasses
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from lab_tutor.web import TutorTarget, make_router
    s = dataclasses.replace(settings, mock_lab="iol01", router_boot_s=90)

    async def resolve(request, bid):
        return TutorTarget(session_id="ab" * 16, broker_url="http://b.invalid", guac_url="/guacamole/")
    r = make_router(s, resolve)
    app = FastAPI(); app.include_router(r)
    c = TestClient(app)
    st = c.get("/tutor/x/state").json()
    assert st["lab_id"] == "iol01" and st["max"] == 2 and st["router_seen"] is True   # corvane-r3# in the tail
    r.mock_broker.set_variant(1)
    assert c.get("/tutor/x/state").json()["passed"] is True
    assert 'data-boot="90"' in c.get("/tutor/x").text
