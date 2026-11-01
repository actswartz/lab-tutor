"""FastAPI router: the panel page, a state JSON route and the streaming ask route."""

from __future__ import annotations

import html
import json
import logging
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .broker import (
    MockBroker,
    TutorBrokerError,
    TutorNotFound,
    TutorNotReady,
    fetch_tutor,
)
from .config import Settings
from .muse import MuseError, Usage, stream_chat
from .prompt import ACTIONS, HintLadder, build_messages
from .spend import SpendCapReached, SpendMeter

log = logging.getLogger("lab_tutor.web")
PAGE = (Path(__file__).parent / "page.html").read_text(encoding="utf-8")
NO_STORE = {"Cache-Control": "no-store"}


BUSY_TTL_S = 90  # longer than one streamed reply (60 s upstream timeout)


@dataclass
class TutorTarget:
    session_id: str
    broker_url: str
    broker_secret: str = ""
    guac_url: str = "about:blank"


ResolveSession = Callable[[Request, str], Awaitable["TutorTarget | None"]]


class RateLimiter:
    """Per key: one request in flight, `per_minute` per rolling minute."""

    def __init__(self, per_minute: int = 10):
        self.per_minute = per_minute
        self._hits: dict[str, deque] = defaultdict(deque)
        self._busy: dict[str, float] = {}

    def acquire(self, key: str, now: float | None = None) -> str | None:
        now = time.monotonic() if now is None else now
        if now - self._busy.get(key, -1e9) < BUSY_TTL_S:  # TTL: a stream that never started can't hold the lock forever
            return "busy"
        q = self._hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_minute:
            return "rate"
        q.append(now)
        self._busy[key] = now
        return None

    def release(self, key: str) -> None:
        self._busy.pop(key, None)


def _json(data: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers=NO_STORE)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _safe_frame_url(u: str) -> str:
    return u if urlparse(u).scheme in ("", "http", "https") else "about:blank"


def make_router(
    settings: Settings,
    resolve_session: ResolveSession,
    *,
    http_client: httpx.AsyncClient | None = None,
    meter: SpendMeter | None = None,
    broker: MockBroker | None = None,
) -> APIRouter:
    router = APIRouter()
    ladder = HintLadder()
    limiter = RateLimiter(10)
    meter = meter or SpendMeter.from_settings(settings)
    mock_broker = (broker or MockBroker()) if settings.mock and not settings.live_broker else None
    holder: dict[str, httpx.AsyncClient] = {}
    router.mock_broker = mock_broker  # type: ignore[attr-defined]
    router.limiter = limiter  # type: ignore[attr-defined]

    def client() -> httpx.AsyncClient:
        if http_client is not None:
            return http_client
        if "c" not in holder:
            holder["c"] = httpx.AsyncClient()
        return holder["c"]

    async def get_state(target: TutorTarget) -> dict:
        if mock_broker is not None:
            return await mock_broker.fetch(target.session_id)
        return await fetch_tutor(
            target.broker_url, target.broker_secret, target.session_id, client()
        )

    @router.get("/tutor/{booking_id}")
    async def page(request: Request, booking_id: str):
        target = await resolve_session(request, booking_id)
        if target is None:
            return HTMLResponse(
                "<!doctype html><title>Not found</title><p>Session not found.</p>",
                status_code=404,
                headers=NO_STORE,
            )
        nonce = secrets.token_urlsafe(16)
        body = (
            PAGE.replace("__NONCE__", nonce)
            .replace("__GUAC_URL__", html.escape(_safe_frame_url(target.guac_url), quote=True))
            .replace("__POLL__", str(settings.poll_seconds))
            .replace("__MOCK__", "1" if settings.mock else "0")
        )
        csp = (
            "default-src 'self'; frame-src 'self'; connect-src 'self'; img-src 'self' data:; "
            f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
        )
        return HTMLResponse(body, headers={**NO_STORE, "Content-Security-Policy": csp})

    @router.get("/tutor/{booking_id}/state")
    async def state(request: Request, booking_id: str):
        target = await resolve_session(request, booking_id)
        if target is None:
            return _json({"status": "not_found"}, 404)
        try:
            st = await get_state(target)
        except TutorNotReady as e:
            return _json({"status": "not_ready", "session_status": e.session_status})
        except TutorNotFound:
            return _json({"status": "not_found"}, 404)
        except (TutorBrokerError, ValueError):
            return _json({"status": "broker_error"}, 502)
        lc = st.get("last_check") or {}
        title = (st.get("lab_tasks") or "").split("\n", 1)[0][:120]
        return _json(
            {
                "status": "ok",
                "lab_id": st.get("lab_id"),
                "lab_title": title,
                "score": lc.get("score"),
                "max": lc.get("max"),
                "passed": bool(lc.get("passed")),
                # names and ok only: hints are the tutor's answer key
                "checks": [
                    {"name": str(c.get("name", "")), "ok": bool(c.get("ok"))}
                    for c in lc.get("checks", [])
                ],
                "terminal_updated_at": st.get("terminal_updated_at"),
                "capturing": bool(st.get("capturing")),
                "generated_at": st.get("generated_at"),
                "mock": settings.mock,
            }
        )

    @router.post("/tutor/{booking_id}/ask")
    async def ask(request: Request, booking_id: str):
        target = await resolve_session(request, booking_id)
        if target is None:
            return _json({"status": "not_found"}, 404)
        try:
            body = await request.json()
            action = body.get("action")
            question = body.get("question")
            if action not in ACTIONS or not (question is None or isinstance(question, str)):
                raise ValueError
            if action == "ask" and not (question or "").strip():
                raise ValueError
        except (ValueError, AttributeError):
            return _json({"status": "bad_request"}, 400)
        why = limiter.acquire(booking_id)
        if why:
            return _json({"status": "rate_limited", "reason": why}, 429)

        async def gen():
            try:
                st = await get_state(target)
                level = ladder.level_for(target.session_id, st, action)
                msgs = build_messages(st, action, question, level)
                if not settings.mock:
                    await meter.check()
                usage = Usage()
                async for piece in stream_chat(settings, msgs, client(), usage):
                    yield _sse("delta", {"text": piece})
                done: dict = {"level": level}
                if not settings.mock and usage.seen:
                    cost = await meter.record(usage)
                    if settings.price_in_per_mtok or settings.price_out_per_mtok:
                        done["cost_usd"] = round(cost, 6)
                yield _sse("done", done)
            except SpendCapReached:
                yield _sse("error", {"message": "The tutor has reached its spend limit."})
            except TutorNotReady:
                yield _sse("error", {"message": "The lab session is not ready yet."})
            except (TutorBrokerError, ValueError):
                yield _sse("error", {"message": "Could not read your lab session."})
            except MuseError:
                yield _sse("error", {"message": "The tutor is unavailable right now."})
            except Exception as e:  # never leak details to the browser
                log.error("ask failed: %s", type(e).__name__)
                yield _sse("error", {"message": "Something went wrong."})
            finally:
                limiter.release(booking_id)

        return StreamingResponse(
            gen(), media_type="text/event-stream", headers={**NO_STORE, "X-Accel-Buffering": "no"}
        )

    return router
