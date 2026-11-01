"""Spend meter: a JSON file with running totals and a hard cap."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .config import Settings
from .muse import Usage


class SpendCapReached(Exception):
    pass


class SpendMeter:
    def __init__(self, path: str, cap_usd: float, price_in: float = 0.0, price_out: float = 0.0):
        self.path = Path(path)
        self.cap = cap_usd
        self.price_in = price_in
        self.price_out = price_out
        self._lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, s: Settings) -> "SpendMeter":
        return cls(s.spend_file, s.spend_cap_usd, s.price_in_per_mtok, s.price_out_per_mtok)

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            d = {}
        return {
            "total_usd": float(d.get("total_usd", 0.0)),
            "calls": int(d.get("calls", 0)),
            "tokens_in": int(d.get("tokens_in", 0)),
            "tokens_out": int(d.get("tokens_out", 0)),
        }

    def _save(self, d: dict) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(d))
        os.replace(tmp, self.path)

    async def totals(self) -> dict:
        async with self._lock:
            return self._load()

    async def check(self) -> None:
        async with self._lock:
            if self._load()["total_usd"] >= self.cap:
                raise SpendCapReached("spend cap reached")

    async def record(self, usage: Usage) -> float:
        cost = (
            usage.prompt_tokens * self.price_in + usage.completion_tokens * self.price_out
        ) / 1_000_000
        async with self._lock:
            d = self._load()
            d["total_usd"] += cost
            d["calls"] += 1
            d["tokens_in"] += usage.prompt_tokens
            d["tokens_out"] += usage.completion_tokens
            self._save(d)
        return cost
