"""Local demo: `uvicorn lab_tutor.app_standalone:app` (always mock mode)."""

from __future__ import annotations

import dataclasses
import html

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .broker import MockBroker
from .config import Settings
from .web import TutorTarget, make_router

settings = dataclasses.replace(Settings.from_env(), mock=True)
mock_broker = MockBroker()
app = FastAPI(title="Lab Tutor (mock demo)")


async def resolve_session(request: Request, booking_id: str):
    if booking_id != "demo":
        return None
    return TutorTarget(
        session_id="0" * 32,
        broker_url="",
        broker_secret="",
        guac_url="/tutor-demo-terminal",
    )


app.include_router(make_router(settings, resolve_session, broker=mock_broker))

TERM_CSP = "default-src 'none'; style-src 'unsafe-inline'"


@app.get("/")
async def root():
    return RedirectResponse("/tutor/demo")


@app.get("/tutor-demo-terminal")
async def demo_terminal():
    st = await mock_broker.fetch()
    tail = html.escape(st["terminal_tail"])
    page = (
        "<!doctype html><meta charset=utf-8><title>terminal</title>"
        "<style>html,body{margin:0;height:100%;background:#0c0c0c}"
        "pre{margin:0;padding:12px;color:#d0d0d0;font:13px/1.35 ui-monospace,Menlo,monospace;"
        "white-space:pre-wrap;word-break:break-word}</style>"
        f"<pre>{tail}</pre>"
    )
    return HTMLResponse(
        page, headers={"Cache-Control": "no-store", "Content-Security-Policy": TERM_CSP}
    )


@app.post("/tutor-demo/step")
async def step():
    """Advance the mock lab to its next fixture (more checks passing)."""
    return JSONResponse({"variant": mock_broker.advance()}, headers={"Cache-Control": "no-store"})
