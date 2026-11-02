import asyncio
import importlib.util
import json
import math
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from lab_tutor import web
from lab_tutor.broker import FIXTURE_DIR
from lab_tutor.config import Settings
from lab_tutor.muse import Usage
from lab_tutor.prompt import HintLadder, build_messages, strip_hint_lines
from lab_tutor.spend import SpendCapReached, SpendMeter, estimate_input_tokens

from .conftest import make_app
from .test_web import events


def run(c):
    return asyncio.run(c)


def chunk(obj):
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def content(t, finish=None):
    return chunk({"choices": [{"delta": {"content": t}, **({"finish_reason": finish} if finish else {})}]})


USAGE = chunk({"choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 100}})
DONE = b"data: [DONE]\n\n"
GOOD = content("[Hint 1] look", "stop") + USAGE + DONE


class Upstream:
    """Scripted upstream: one response factory per chat call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.bodies = []

    def __call__(self, req):
        sample = json.loads((FIXTURE_DIR / "sample-lab03.json").read_text())
        if "/internal/v1/" in req.url.path:
            return httpx.Response(200, json=sample)
        self.bodies.append(json.loads(req.content))
        r = self.responses[min(len(self.bodies) - 1, len(self.responses) - 1)]
        return r() if callable(r) else httpx.Response(200, content=r)


def live(tmp_path, up, pin=1.0, pout=2.0, cap=20.0):
    s = Settings(mock=False, api_key="k", base_url="http://m.test/v1", spend_file=str(tmp_path / "s.json"),
                 price_in_per_mtok=pin, price_out_per_mtok=pout, spend_cap_usd=cap)
    hc = httpx.AsyncClient(transport=httpx.MockTransport(up))
    app, _ = make_app(s, http_client=hc)
    return TestClient(app), s


def ledger(s):
    return json.loads(Path(s.spend_file).read_text())


def ask(c, action="hint"):
    return events(c.post("/tutor/b1/ask", json={"action": action}).text)


# ---- spend ---------------------------------------------------------------


def test_concurrent_reservations_near_cap(tmp_path):
    m = SpendMeter(str(tmp_path / "s.json"), 1.0, 1.0, 1.0)

    async def go():
        return await asyncio.gather(m.reserve(0.6), m.reserve(0.6), return_exceptions=True)

    out = run(go())
    assert sum(isinstance(o, SpendCapReached) for o in out) == 1
    assert sum(isinstance(o, str) for o in out) == 1


def test_two_meters_share_the_file(tmp_path):
    f = str(tmp_path / "s.json")
    a, b = SpendMeter(f, 1.0, 1.0, 1.0), SpendMeter(f, 1.0, 1.0, 1.0)
    run(a.reserve(0.6))
    with pytest.raises(SpendCapReached):
        run(b.reserve(0.6))


def test_settle_actual_vs_unknown(tmp_path):
    m = SpendMeter(str(tmp_path / "s.json"), 10.0, 1000.0, 2000.0)
    r1 = run(m.reserve(1.0))
    assert run(m.settle(r1, Usage(100, 100, True))) == pytest.approx(0.3)
    r2 = run(m.reserve(1.0))
    assert run(m.settle(r2, None)) == pytest.approx(1.0)
    t = run(m.totals())
    assert t["total_usd"] == pytest.approx(1.3) and t["calls"] == 2 and t["reservations"] == {}


def test_stale_reservation_booked_at_max(tmp_path):
    m = SpendMeter(str(tmp_path / "s.json"), 10.0, 1.0, 1.0)
    run(m.reserve(2.0))
    m._now = lambda: __import__("time").time() + 3601
    t = run(m.totals())
    assert t["total_usd"] == pytest.approx(2.0) and t["reservations"] == {}


def test_max_cost_estimate(tmp_path):
    m = SpendMeter(str(tmp_path / "s.json"), 10.0, 3.0, 5.0)
    msgs = [{"role": "system", "content": "a" * 10}, {"role": "user", "content": "b" * 5}]
    assert estimate_input_tokens(msgs) == math.ceil(15 / 3) == 5
    assert m.max_cost(msgs, 100) == pytest.approx((5 * 3.0 + 100 * 5.0) / 1e6)


@pytest.mark.parametrize("junk", ["{not json", "", "[]", '{"total_usd": "x"}'])
def test_corrupt_ledger_refuses(tmp_path, junk):
    up = Upstream(GOOD)
    c, s = live(tmp_path, up)
    Path(s.spend_file).write_text(junk)
    ev = ask(c)
    assert ev[-1][0] == "error" and "spend" in ev[-1][1]["message"]
    assert up.bodies == []
    assert Path(s.spend_file).read_text() == junk  # untouched


@pytest.mark.parametrize("prices", [(0.0, 2.0), (1.0, 0.0), (0.0, 0.0)])
def test_zero_prices_refuse_live(tmp_path, prices):
    up = Upstream(GOOD)
    c, _ = live(tmp_path, up, *prices)
    ev = ask(c)
    assert ev[-1] == ("error", {"message": "The tutor is not configured."})
    assert up.bodies == []


def test_mock_mode_records_nothing(client, settings):
    assert events(client.post("/tutor/b1/ask", json={"action": "hint"}).text)[-1][0] == "done"
    from pathlib import Path as P
    assert not P(settings.spend_file).exists()


# ---- accounting on failure -------------------------------------------------


def broken_stream(prefix):
    async def agen():
        yield prefix
        raise httpx.ReadError("boom")

    return lambda: httpx.Response(200, stream=_AStream(agen()))


class _AStream(httpx.AsyncByteStream):
    def __init__(self, g):
        self.g = g

    async def __aiter__(self):
        async for b in self.g:
            yield b


def test_failed_stream_books_full_reservation(tmp_path):
    c, s = live(tmp_path, Upstream(broken_stream(content("[Hint 1] hi"))))
    ev = ask(c)
    assert ev[-1][0] == "error"
    d = ledger(s)
    assert d["reservations"] == {} and d["calls"] == 1
    assert d["total_usd"] > 1500 * 2.0 / 1e6 * 0.99  # at least the completion budget


def test_usage_seen_before_error_is_used(tmp_path):
    c, s = live(tmp_path, Upstream(broken_stream(content("[Hint 1] hi") + USAGE)))
    assert ask(c)[-1][0] == "error"
    d = ledger(s)
    assert d["total_usd"] == pytest.approx(0.0012) and d["tokens_in"] == 1000 and d["reservations"] == {}


def test_slow_stream_hits_deadline_and_books(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STREAM_DEADLINE_S", 0.2)

    async def agen():
        yield content("[Hint 1] a")
        await asyncio.sleep(5)
        yield GOOD

    c, s = live(tmp_path, Upstream(lambda: httpx.Response(200, stream=_AStream(agen()))))
    ev = ask(c)
    assert ev[-1] == ("error", {"message": "The tutor is unavailable right now."})
    d = ledger(s)
    assert d["calls"] == 1 and d["reservations"] == {} and d["total_usd"] > 0


# ---- empty / truncated replies, retry, ladder ---------------------------------


def test_empty_reply_errors_and_ladder_unchanged(tmp_path):
    up = Upstream(GOOD, USAGE + DONE, USAGE + DONE, GOOD)
    c, s = live(tmp_path, up)
    assert ask(c)[-1][1]["level"] == 1
    ev = ask(c)  # empty, retried once, empty again
    assert ev[-1] == ("error", {"message": "The tutor could not finish an answer. Try again."})
    assert not any(e == "delta" for e, _ in ev)
    assert len(up.bodies) == 3
    assert ask(c)[-1][1]["level"] == 2  # not 3: the failed press changed nothing
    assert ledger(s)["reservations"] == {}


def test_length_with_content_is_truncated(tmp_path):
    c, _ = live(tmp_path, Upstream(content("[Hint 1] part", "length") + USAGE + DONE))
    ev = ask(c)
    assert ev[-1][0] == "done" and ev[-1][1]["truncated"] is True


def test_length_without_content_is_error(tmp_path):
    reply = chunk({"choices": [{"delta": {"content": ""}, "finish_reason": "length"}]}) + USAGE + DONE
    c, s = live(tmp_path, Upstream(reply))
    assert ask(c)[-1][1]["message"] == "The tutor could not finish an answer. Try again."
    assert ledger(s)["calls"] == 2  # both attempts settled with real usage


def test_normal_done_has_no_truncated_flag(tmp_path):
    c, _ = live(tmp_path, Upstream(GOOD))
    assert "truncated" not in ask(c)[-1][1]


def test_retry_once_with_doubled_budget_then_success(tmp_path):
    up = Upstream(USAGE + DONE, GOOD)
    c, s = live(tmp_path, up)
    ev = ask(c)
    assert ev[-1][0] == "done"
    assert [b["max_completion_tokens"] for b in up.bodies] == [1500, 3000]
    assert ledger(s)["calls"] == 2


def test_retry_budget_capped_at_4000(tmp_path):
    up = Upstream(USAGE + DONE, GOOD)
    c, s = live(tmp_path, up)
    s.max_tokens = 3000
    ask(c)
    assert [b["max_completion_tokens"] for b in up.bodies] == [3000, 4000]


def test_no_retry_once_text_was_sent(tmp_path):
    up = Upstream(content("[Hint 1] partial") + USAGE + DONE, GOOD)  # text but no finish_reason
    c, _ = live(tmp_path, up)
    ev = ask(c)
    assert len(up.bodies) == 1
    assert ev[0][0] == "delta" and ev[-1][1]["message"] == "The tutor could not finish an answer. Try again."


# ---- lock token ---------------------------------------------------------------


def test_lock_release_needs_owner_token():
    L = web.RateLimiter(10)
    old, _ = L.acquire("k", 0)
    new, why = L.acquire("k", web.BUSY_TTL_S + 1)  # old expired
    assert new and why is None and new != old
    L.release("k", old)  # stale owner: no-op
    assert L.acquire("k", web.BUSY_TTL_S + 2) == (None, "busy")
    L.release("k", new)
    assert L.acquire("k", web.BUSY_TTL_S + 3)[1] is None


def test_hint_ladder_peek_does_not_mutate():
    L = HintLadder()
    st = {"last_check": {"checks": [{"name": "a", "ok": False}]}}
    assert L.peek("s", st, "hint") == 1
    assert L.peek("s", st, "hint") == 1
    L.commit("s", st, 1)
    assert L.peek("s", st, "hint") == 2
    assert L.peek("s", st, "hint") == 2


# ---- hints out of <terminal> ----------------------------------------------------


def test_hint_lines_stripped_from_terminal():
    tail = (
        "student@workspace:~$ lab check\nScore: 0/2\n"
        "  [--] one\n        hint: do the exact thing\n        and wrap here\n"
        "  [--] two\n        hint: another fix\n"
        "student@workspace:~$ "
    )
    assert "hint:" not in strip_hint_lines(tail)
    assert "and wrap here" not in strip_hint_lines(tail)
    assert "[--] two" in strip_hint_lines(tail) and strip_hint_lines(tail).endswith("student@workspace:~$ ")
    st = {"lab_tasks": "t", "terminal_tail": tail, "last_check": {"checks": [{"name": "one", "ok": False, "hint": "SECRETFIX"}]}}
    user = build_messages(st, "hint", None, 1)[1]["content"]
    term = user.split("<terminal>")[1].split("</terminal>")[0]
    assert "do the exact thing" not in term and "hint:" not in term
    assert "SECRETFIX" in user.split("<answer_key>")[1]  # still reaches the model there


@pytest.mark.parametrize("f", sorted(p.name for p in FIXTURE_DIR.glob("*.json")))
def test_fixture_tails_have_no_hint_lines(f):
    t = json.loads((FIXTURE_DIR / f).read_text())["terminal_tail"]
    assert "hint:" not in t and "Stuck? Ask the Lab Tutor for a hint." in t


# ---- evals -----------------------------------------------------------------------


def _evals():
    spec = importlib.util.spec_from_file_location("run_evals", Path(__file__).parent.parent / "evals" / "run_evals.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_eval_rejects_empty_and_bad_format():
    ev = _evals()
    case = {"must_not": ["Secret"], "must": ["[Hint 2]"]}
    assert ev.check_reply("", case)[0] == ["empty reply"]
    assert ev.check_reply("  \n", case)[0] == ["empty reply"]
    assert any("format" in p for p in ev.check_reply("hello there", case)[0])
    p, missing = ev.check_reply("[Hint 1] the secret is x", case)
    assert p == ["Secret"] and missing == ["[Hint 2]"]  # case-insensitive must_not
    assert ev.check_reply("[Hint 2] ok", case) == ([], [])
    assert ev.check_reply("[Done] nice", {}) == ([], [])
    assert ev.check_reply("[Hint x] no", {})[0]
