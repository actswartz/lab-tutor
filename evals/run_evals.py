#!/usr/bin/env python3
"""Run evals/cases.json through build_messages + stream_chat.

Mock by default. Real API only with --live AND MUSE_SPARK_API_KEY set.
Only must_not substrings are checked automatically; `must` items that start
with "[" (like "[Hint 3]") are checked too, the rest are shown for a human.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from lab_tutor.config import Settings  # noqa: E402
from lab_tutor.muse import Usage, stream_chat  # noqa: E402
from lab_tutor.prompt import build_messages  # noqa: E402


async def run_case(settings, case, client):
    state = {
        "lab_id": case.get("lab_id"),
        "lab_tasks": case.get("lab_tasks", ""),
        "terminal_tail": case.get("terminal_tail", ""),
        "last_check": case.get("last_check"),
    }
    q = (case.get("question") or "").strip()
    action = "ask" if q else "what_went_wrong"
    level = int(case.get("level", 3 if case["id"] == "escalate" else 1))
    msgs = build_messages(state, action, q, level)
    usage = Usage()
    reply = "".join([p async for p in stream_chat(settings, msgs, client, usage)])
    low = reply.lower()
    leaks = [s for s in case.get("must_not", []) if s.lower() in low]
    missing = [m for m in case.get("must", []) if m.startswith("[") and m.lower() not in low]
    return reply, leaks, missing, usage


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="call the real API (costs money)")
    ap.add_argument("--cases", default=str(ROOT / "evals" / "cases.json"))
    ap.add_argument("--show", action="store_true", help="print each reply")
    a = ap.parse_args()
    settings = Settings.from_env()
    live = a.live and bool(settings.api_key)
    if a.live and not live:
        print("--live given but MUSE_SPARK_API_KEY is not set; running mock instead")
    settings = dataclasses.replace(settings, mock=not live)
    cases = json.loads(Path(a.cases).read_text())
    print(f"mode: {'LIVE' if live else 'MOCK'}  cases: {len(cases)}")
    rows, total_cost, fails = [], 0.0, 0
    async with httpx.AsyncClient() as client:
        for c in cases:
            reply, leaks, missing, usage = await run_case(settings, c, client)
            cost = usage.cost_usd(settings)
            total_cost += cost
            ok = not leaks and not missing
            fails += not ok
            rows.append((c["id"], "PASS" if ok else "FAIL", len(reply), usage.prompt_tokens + usage.completion_tokens, cost, leaks + [f"missing {m}" for m in missing]))
            if a.show:
                print(f"--- {c['id']}\n{reply}\n")
    print(f"{'case':22} {'result':6} {'chars':>5} {'tokens':>6} {'usd':>8}  notes")
    for r in rows:
        print(f"{r[0]:22} {r[1]:6} {r[2]:5d} {r[3]:6d} {r[4]:8.4f}  {'; '.join(r[5])}")
    note = "" if settings.price_in_per_mtok or settings.price_out_per_mtok else " (prices unset, usd shows 0)"
    print(f"\n{len(rows) - fails}/{len(rows)} passed, total cost ${total_cost:.4f}{note}")
    print("`must` items without a leading [ are for human review (see evals/cases.json).")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
