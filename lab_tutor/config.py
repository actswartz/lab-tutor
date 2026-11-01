"""Settings read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PROMPT_PATH = str(Path(__file__).parent / "prompts" / "system.md")


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _float(v: str | None, default: float) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


def _int(v: str | None, default: int) -> int:
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default


@dataclass
class Settings:
    mock: bool = False
    mock_lab: str = "lab03"  # fixture set for full mock mode: lab03 | iol01
    router_boot_s: int = 0  # >0: show "router starting" until a router prompt appears or this many seconds pass
    live_broker: bool = False  # with mock: real broker state, canned replies (no model spend)
    api_key: str = field(default="", repr=False)  # never printed
    base_url: str = "https://api.meta.ai/v1"
    model: str = "muse-spark-1.3"
    temperature: float = 0.3
    max_tokens: int = 350
    spend_cap_usd: float = 20.0
    # 0 means "unknown": tokens are still counted, but dollars are not, so the
    # cap cannot trip. The operator must set both prices to enforce the cap.
    price_in_per_mtok: float = 0.0
    price_out_per_mtok: float = 0.0
    spend_file: str = "./tutor-spend.json"
    poll_seconds: int = 5
    system_prompt_path: str = DEFAULT_PROMPT_PATH

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        return cls(
            mock=_bool(e.get("TUTOR_MOCK")),
            live_broker=_bool(e.get("TUTOR_LIVE_BROKER")),
            mock_lab=e.get("TUTOR_MOCK_LAB", "lab03"),
            router_boot_s=max(0, _int(e.get("TUTOR_ROUTER_BOOT_S"), 0)),
            api_key=e.get("MUSE_SPARK_API_KEY", ""),
            base_url=e.get("MUSE_BASE_URL", "https://api.meta.ai/v1"),
            model=e.get("TUTOR_MODEL", "muse-spark-1.3"),
            temperature=_float(e.get("TUTOR_TEMPERATURE"), 0.3),
            max_tokens=_int(e.get("TUTOR_MAX_TOKENS"), 350),
            spend_cap_usd=_float(e.get("TUTOR_SPEND_CAP_USD"), 20.0),
            price_in_per_mtok=_float(e.get("MUSE_PRICE_IN_PER_MTOK"), 0.0),
            price_out_per_mtok=_float(e.get("MUSE_PRICE_OUT_PER_MTOK"), 0.0),
            spend_file=e.get("TUTOR_SPEND_FILE", "./tutor-spend.json"),
            poll_seconds=max(1, _int(e.get("TUTOR_POLL_SECONDS"), 5)),
            system_prompt_path=e.get("TUTOR_SYSTEM_PROMPT") or DEFAULT_PROMPT_PATH,
        )
