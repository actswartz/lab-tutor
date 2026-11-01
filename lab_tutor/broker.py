"""Client for the lab broker's tutor route (HMAC signed), plus a mock."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

FIXTURE_DIR = Path(__file__).parent / "fixtures"
MOCK_SETS = {
    "lab03": ["sample-lab03.json", "variant-typo-fixed-4of7.json", "variant-all-pass-7of7.json"],
    "iol01": ["sample-iol01-fail.json", "sample-iol01-pass.json"],  # real IOS router, 2 checks
}
MOCK_FIXTURES = MOCK_SETS["lab03"]
_SID_RE = re.compile(r"[0-9a-f]{16,64}")


class TutorBrokerError(Exception):
    """Broker unreachable or answered with an unexpected status."""


class TutorNotFound(TutorBrokerError):
    pass


class TutorNotReady(TutorBrokerError):
    def __init__(self, session_status: str = "unknown"):
        super().__init__("session not ready")
        self.session_status = session_status


def valid_session_id(sid: str) -> bool:
    return isinstance(sid, str) and _SID_RE.fullmatch(sid) is not None


def sign(secret: str, ts: int | str, path: str) -> str:
    msg = f"{ts}\nGET\n{path}\n".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


async def fetch_tutor(
    base_url: str, secret: str, session_id: str, client: httpx.AsyncClient
) -> dict:
    if not valid_session_id(session_id):
        raise ValueError("invalid session id")
    path = f"/internal/v1/sessions/{session_id}/tutor"
    ts = int(time.time())
    headers = {"X-Timestamp": str(ts), "X-Signature": sign(secret, ts, path)}
    try:
        r = await client.get(base_url.rstrip("/") + path, headers=headers, timeout=10)
    except httpx.HTTPError:
        # Do not chain: the request object carries the signature headers.
        raise TutorBrokerError("broker unreachable") from None
    if r.status_code == 200:
        try:
            data = r.json()
        except ValueError:
            raise TutorBrokerError("broker sent invalid json") from None
        if not isinstance(data, dict):
            raise TutorBrokerError("broker sent unexpected json")
        return data
    if r.status_code == 404:
        raise TutorNotFound("session not found")
    if r.status_code == 409:
        try:
            status = str(r.json().get("session_status", "unknown"))
        except (ValueError, AttributeError):
            status = "unknown"
        raise TutorNotReady(status)
    raise TutorBrokerError(f"broker returned {r.status_code}")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class MockBroker:
    """Serves fixtures. Stays on one variant until `advance()` is called."""

    def __init__(self, fixtures: list[str] | None = None):
        names = fixtures or MOCK_FIXTURES
        self._states = [json.loads((FIXTURE_DIR / n).read_text()) for n in names]
        self.index = 0

    def advance(self) -> int:
        self.index = (self.index + 1) % len(self._states)
        return self.index

    def set_variant(self, i: int) -> None:
        self.index = i % len(self._states)

    async def fetch(self, session_id: str | None = None) -> dict:
        s = copy.deepcopy(self._states[self.index])
        now = datetime.now(timezone.utc)
        s["terminal_updated_at"] = _iso(now - timedelta(seconds=12))
        s["generated_at"] = _iso(now)
        return s
