"""Spend meter: a JSON ledger with running totals, a hard cap and reservations.

Every paid call first reserve()s its worst-case cost, then settle()s with the
real usage. The ledger is rewritten atomically under an in-process asyncio lock
plus an flock on a sidecar file, so several workers on one host share it.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import math
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path

from .config import Settings
from .muse import Usage

STALE_RESERVATION_S = 3600
CHARS_PER_TOKEN = 3  # conservative: real text is closer to 4


class SpendCapReached(Exception):
    pass


class LedgerUnreadable(SpendCapReached):
    """The ledger exists but can't be read: refuse paid calls, never assume $0."""


class SpendNotConfigured(Exception):
    """Live calls need both prices set (> 0)."""


def estimate_input_tokens(messages: list[dict]) -> int:
    chars = sum(len(str(m.get("content") or "")) for m in messages)
    return math.ceil(chars / CHARS_PER_TOKEN)


class SpendMeter:
    def __init__(self, path: str, cap_usd: float, price_in: float = 0.0, price_out: float = 0.0):
        self.path = Path(path)
        self.cap = cap_usd
        self.price_in = price_in
        self.price_out = price_out
        self._lock = asyncio.Lock()
        self._now = time.time

    @classmethod
    def from_settings(cls, s: Settings) -> "SpendMeter":
        return cls(s.spend_file, s.spend_cap_usd, s.price_in_per_mtok, s.price_out_per_mtok)

    # ---- pricing -------------------------------------------------------

    def require_prices(self) -> None:
        if not (self.price_in > 0 and self.price_out > 0):
            raise SpendNotConfigured("MUSE_PRICE_IN_PER_MTOK / MUSE_PRICE_OUT_PER_MTOK must both be > 0")

    def cost_of(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (prompt_tokens * self.price_in + completion_tokens * self.price_out) / 1_000_000

    def max_cost(self, messages: list[dict], max_completion_tokens: int) -> float:
        return self.cost_of(estimate_input_tokens(messages), max_completion_tokens)

    # ---- ledger io -----------------------------------------------------

    @contextmanager
    def _flock(self):
        lock_path = self.path.with_name(self.path.name + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _load(self) -> dict:
        """Read the ledger. Missing file = fresh $0. Present but unreadable = refuse."""
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            raw = None
        except OSError as e:
            raise LedgerUnreadable("spend ledger unreadable") from e
        if raw is None:
            d: dict = {}
        else:
            try:
                d = json.loads(raw)
                if not isinstance(d, dict):
                    raise ValueError("not an object")
            except ValueError as e:
                raise LedgerUnreadable("spend ledger corrupt") from e
        try:
            out = {
                "total_usd": float(d.get("total_usd", 0.0)),
                "calls": int(d.get("calls", 0)),
                "tokens_in": int(d.get("tokens_in", 0)),
                "tokens_out": int(d.get("tokens_out", 0)),
                "reservations": {},
            }
            for rid, r in (d.get("reservations") or {}).items():
                out["reservations"][str(rid)] = {"max_usd": float(r["max_usd"]), "ts": float(r["ts"])}
        except (TypeError, ValueError, KeyError, AttributeError) as e:
            raise LedgerUnreadable("spend ledger corrupt") from e
        # reservations nobody settled for an hour: book them at their max cost
        now = self._now()
        for rid in [k for k, r in out["reservations"].items() if now - r["ts"] > STALE_RESERVATION_S]:
            out["total_usd"] += out["reservations"].pop(rid)["max_usd"]
            out["calls"] += 1
        return out

    def _save(self, d: dict) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(d))
        os.replace(tmp, self.path)

    @staticmethod
    def _outstanding(d: dict) -> float:
        return sum(r["max_usd"] for r in d["reservations"].values())

    # ---- api -----------------------------------------------------------

    async def totals(self) -> dict:
        async with self._lock:
            with self._flock():
                d = self._load()
        d["outstanding_usd"] = self._outstanding(d)
        return d

    async def check(self) -> None:
        async with self._lock:
            with self._flock():
                d = self._load()
        if d["total_usd"] + self._outstanding(d) >= self.cap:
            raise SpendCapReached("spend cap reached")

    async def reserve(self, max_cost_usd: float) -> str:
        """Hold `max_cost_usd` against the cap. Raises SpendCapReached if it won't fit."""
        async with self._lock:
            with self._flock():
                d = self._load()
                if d["total_usd"] + self._outstanding(d) + max_cost_usd > self.cap:
                    raise SpendCapReached("spend cap reached")
                rid = secrets.token_hex(8)
                d["reservations"][rid] = {"max_usd": max_cost_usd, "ts": self._now()}
                self._save(d)
        return rid

    async def settle(self, rid: str, usage: Usage | None) -> float:
        """Replace the reservation with the real cost; unknown usage books the full max."""
        async with self._lock:
            with self._flock():
                d = self._load()
                r = d["reservations"].pop(rid, None)
                if r is None:  # already booked as stale
                    self._save(d)
                    return 0.0
                if usage is not None and usage.seen:
                    cost = self.cost_of(usage.prompt_tokens, usage.completion_tokens)
                    d["tokens_in"] += usage.prompt_tokens
                    d["tokens_out"] += usage.completion_tokens
                else:
                    cost = r["max_usd"]
                d["total_usd"] += cost
                d["calls"] += 1
                self._save(d)
        return cost

    async def record(self, usage: Usage) -> float:
        """Book a finished call that was never reserved (kept for simple callers)."""
        cost = self.cost_of(usage.prompt_tokens, usage.completion_tokens)
        async with self._lock:
            with self._flock():
                d = self._load()
                d["total_usd"] += cost
                d["calls"] += 1
                d["tokens_in"] += usage.prompt_tokens
                d["tokens_out"] += usage.completion_tokens
                self._save(d)
        return cost
