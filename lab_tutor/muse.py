"""Streaming client for the Meta Model API (OpenAI-compatible chat completions)."""

from __future__ import annotations

import asyncio
import codecs
import json
import logging
import re
from dataclasses import dataclass
from typing import AsyncIterator

import httpx

from .config import Settings

log = logging.getLogger("lab_tutor.muse")


class MuseError(Exception):
    """Upstream failure. Messages never contain headers, bodies or the key."""


class MuseIncomplete(MuseError):
    """Stream ended with no usable answer (no visible text, or no finish_reason)."""

    def __init__(self) -> None:
        super().__init__("incomplete")


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seen: bool = False
    finish_reason: str | None = None
    visible: bool = False  # any non-whitespace reply text was produced

    def cost_usd(self, s: Settings) -> float:
        return (
            self.prompt_tokens * s.price_in_per_mtok
            + self.completion_tokens * s.price_out_per_mtok
        ) / 1_000_000


def redact(text: str, key: str) -> str:
    return text.replace(key, "[redacted]") if key else text


class SSEParser:
    """Incremental SSE parser. feed() returns the `data:` payloads completed so far."""

    def __init__(self) -> None:
        self._buf = ""
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def feed(self, chunk: bytes | str) -> list[str]:
        self._buf += self._dec.decode(chunk) if isinstance(chunk, bytes) else chunk
        self._buf = self._buf.replace("\r\n", "\n")
        out: list[str] = []
        while "\n\n" in self._buf:
            event, self._buf = self._buf.split("\n\n", 1)
            data = [l[5:].lstrip(" ") for l in event.split("\n") if l.startswith("data:")]
            if data:
                out.append("\n".join(data))
        return out


def _apply(payload: str, usage: Usage) -> str:
    """Return delta text from one data payload; update usage if present."""
    try:
        obj = json.loads(payload)
    except ValueError:
        return ""
    u = obj.get("usage")
    if isinstance(u, dict):
        usage.prompt_tokens = int(u.get("prompt_tokens") or 0)
        usage.completion_tokens = int(u.get("completion_tokens") or 0)
        usage.seen = True
    text = ""
    for ch in obj.get("choices") or []:
        text += (ch.get("delta") or {}).get("content") or ""
        if ch.get("finish_reason"):
            usage.finish_reason = str(ch["finish_reason"])
    return text


async def stream_chat(
    settings: Settings,
    messages: list[dict],
    client: httpx.AsyncClient | None,
    usage: Usage | None = None,
    max_tokens: int | None = None,
) -> AsyncIterator[str]:
    """Yield reply text. Pass a Usage object to receive token counts and finish_reason.

    Raises MuseIncomplete (after usage was captured) if the stream ends with no
    visible text, with no finish_reason, or was cut by "length" before any text.
    """
    usage = usage if usage is not None else Usage()
    if settings.mock:
        async for w in MockMuse().stream(messages):
            yield w
        return
    if not settings.api_key:
        raise MuseError("tutor is not configured")
    body = {
        "model": settings.model,
        "messages": messages,
        "temperature": settings.temperature,
        "max_completion_tokens": max_tokens or settings.max_tokens,
        "reasoning_effort": settings.reasoning_effort,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    url = settings.base_url.rstrip("/") + "/chat/completions"
    parser = SSEParser()
    done = False
    try:
        async with client.stream(
            "POST", url, json=body, headers=headers, timeout=60
        ) as r:
            if r.status_code != 200:
                log.warning("muse upstream status %s", r.status_code)
                raise MuseError(f"upstream returned {r.status_code}")
            async for chunk in r.aiter_bytes():
                for payload in parser.feed(chunk):
                    if payload.strip() == "[DONE]":
                        done = True
                        break
                    text = _apply(payload, usage)
                    if text:
                        if text.strip():
                            usage.visible = True
                        yield text
                if done:
                    break
    except httpx.HTTPError as e:
        # Log only the exception class: str(e) / request headers could carry the key.
        log.warning("muse transport error: %s", type(e).__name__)
        raise MuseError("upstream unreachable") from None
    if not usage.visible or usage.finish_reason is None:
        log.warning(
            "muse incomplete reply: finish_reason=%s visible=%s", usage.finish_reason, usage.visible
        )
        raise MuseIncomplete()


# ---- mock ---------------------------------------------------------------

_REPLIES = {
    "what_went_wrong": (
        "[Hint {n}] Look at the last error your terminal printed, then at which checks "
        "still show a cross. They are probably the same problem seen from two sides. "
        "Read the line right above the error before you change anything."
    ),
    "hint": {
        1: "[Hint 1] Your own output shows a message just before the last prompt. "
        "What is it complaining about, a name, a file, or a connection?",
        2: "[Hint 2] This is a question of which device and which setting your script "
        "touches. Check the lab task wording against what you actually targeted.",
        3: "[Hint 3] Split the work into steps: reach the device, enter the right mode, "
        "make the one change, then keep it. Find which step you skipped, then run the check again.",
    },
    "explain_output": (
        "[Hint {n}] The table lists each interface with its address and status. "
        "Look at the Status and Protocol columns: up/up means working, "
        "administratively down means someone shut it on purpose."
    ),
    "ask": (
        "[Hint {n}] Good question. I will not hand you the answer, but check the lab task "
        "text again and compare it with the last thing your terminal showed. "
        "What differs?"
    ),
}
_DONE = (
    "[Done] You read both routers from a script, saved the output, and made a "
    "saved change over the CLI. That is a repeatable backup-and-change workflow."
)


class MockMuse:
    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        am = re.search(r"^Action: (\w+)", user, re.M)
        lm = re.search(r"^Hint level: (\d)", user, re.M)
        action = am.group(1) if am else "ask"
        level = int(lm.group(1)) if lm else 1
        if "passed: True" in user:
            text = _DONE
        else:
            t = _REPLIES.get(action, _REPLIES["ask"])
            text = t[level] if isinstance(t, dict) else t.format(n=level)
        for i, w in enumerate(text.split(" ")):
            await asyncio.sleep(0.03)
            yield w if i == 0 else " " + w
