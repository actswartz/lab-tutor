"""Every eval case through the real prompt builder: the parts that do not depend on the model."""
import json
import re
from pathlib import Path

import pytest

from lab_tutor.prompt import build_messages

CASES = json.loads((Path(__file__).parents[1] / "evals" / "cases.json").read_text())
SYSTEM = (Path(__file__).parents[1] / "lab_tutor" / "prompts" / "system.md").read_text()


def _section(text, tag):
    m = re.search(rf"<{tag}>\n(.*?)\n</{tag}>", text, re.S)
    return m.group(1) if m else None


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_case_prompt_structure(case):
    state = {k: case.get(k) for k in ("lab_id", "lab_tasks", "terminal_tail", "last_check")}
    q = (case.get("question") or "").strip()
    msgs = build_messages(state, "ask" if q else "what_went_wrong", q, 1)
    assert [m["role"] for m in msgs] == ["system", "user"]
    sys_txt, user = msgs[0]["content"], msgs[1]["content"]
    assert sys_txt.strip() == SYSTEM.strip()                     # nothing student-controlled in the system role
    tail = (case.get("terminal_tail") or "")[-8192:]
    if tail.strip():
        assert tail.splitlines()[-1].strip() in (_section(user, "terminal") or "")
    if q:
        assert q[:500].replace("</", "<​/") in (_section(user, "question") or "")
    hints = [c["hint"] for c in ((case.get("last_check") or {}).get("checks") or []) if c.get("hint")]
    key = _section(user, "answer_key") or ""
    check = _section(user, "check_result") or ""
    for h in hints:
        assert h in key and h not in check                       # hints only in the answer key
    # injected text can't close a section: exactly one of each delimiter
    for tag in ("terminal", "check_result", "answer_key"):
        assert user.count(f"</{tag}>") == 1
