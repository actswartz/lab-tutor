# Lab Tutor

**A CCIE looking over your shoulder in every lab session, who never gives you the answer.**

Lab Tutor is a side panel that sits next to a student's browser terminal in a hosted
router lab. It reads what the terminal showed and the latest `lab check` result, then
coaches with Muse Spark (Meta Model API): it explains the symptom, then gives a graded
hint ladder (nudge, concept, next step in words). It never prints the fix.

## Architecture

```
 Browser                         Gateway (your FastAPI app)                 Lab broker
+--------------------+          +------------------------------+          +------------------+
| iframe: terminal   |          |  lab_tutor.web router        |  HMAC    | GET .../tutor    |
| panel: score,      |  poll    |   GET  /tutor/{id}           |  GET     |  terminal_tail   |
|  checks, buttons   |--------->|   GET  /tutor/{id}/state     |--------->|  last_check      |
|                    |  5 s     |   POST /tutor/{id}/ask  (SSE)|          +------------------+
|  reply streams in  |<---------|        |                     |
+--------------------+   SSE    |        v                     |          Meta Model API
                                |  prompt.py -> muse.py -------+--------> /chat/completions
                                |  spend.py (hard cap)         |  stream   (key stays here)
                                +------------------------------+
```

## Run it in mock mode (no key, no credits)

```
python3.12 -m venv venv && . venv/bin/activate
pip install fastapi httpx uvicorn pytest
uvicorn lab_tutor.app_standalone:app
# open http://127.0.0.1:8000/tutor/demo
```

The demo serves a recorded lab transcript and canned replies, and shows a
"SIMULATED (mock mode)" badge. `POST /tutor-demo/step` advances the fake lab
(0/7, then 4/7, then 7/7 checks passing).

Tests: `pytest`. Evals: `python evals/run_evals.py` (mock) or
`MUSE_SPARK_API_KEY=... python evals/run_evals.py --live` (real calls, costs money).

## Mounting in your app

```python
from lab_tutor.config import Settings
from lab_tutor.web import make_router, TutorTarget

async def resolve_session(request, booking_id):
    # your auth + lookup; return None for "not found / not yours"
    return TutorTarget(session_id=..., broker_url=..., broker_secret=..., guac_url=...)

app.include_router(make_router(Settings.from_env(), resolve_session))
```

The package imports nothing from the host app. `guac_url` must be same-origin with the
page (the CSP says `frame-src 'self'`).

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `TUTOR_MOCK` | `0` | `1` = fixtures and canned replies, no network |
| `TUTOR_LIVE_BROKER` | `0` | with `TUTOR_MOCK=1`: read the real broker, keep canned replies (no model spend) |
| `MUSE_SPARK_API_KEY` | none | API key. Server side only |
| `MUSE_BASE_URL` | `https://api.meta.ai/v1` | OpenAI-compatible base URL |
| `TUTOR_MODEL` | `muse-spark-1.3` | model name |
| `TUTOR_TEMPERATURE` | `0.3` | sampling temperature |
| `TUTOR_MAX_TOKENS` | `350` | `max_completion_tokens` |
| `TUTOR_SPEND_CAP_USD` | `20` | hard stop; checked before each live call |
| `MUSE_PRICE_IN_PER_MTOK` | `0` | USD per million input tokens. **Operator must set** |
| `MUSE_PRICE_OUT_PER_MTOK` | `0` | USD per million output tokens. **Operator must set** |
| `TUTOR_SPEND_FILE` | `./tutor-spend.json` | running totals (usd, calls, tokens) |
| `TUTOR_POLL_SECONDS` | `5` | panel poll interval |
| `TUTOR_SYSTEM_PROMPT` | bundled `prompts/system.md` | path to an alternate system prompt |

With both prices at `0` (unknown) tokens are still counted but dollars are not, so the
cap cannot trip. Set the prices from your account's rate card before real use.

## Security notes

- **Terminal text and the student's question are untrusted.** They go only in the user
  message, inside `<terminal>` / `<question>` sections after the system prompt, with
  look-alike tags defanged. The page renders every dynamic value with `textContent`;
  nothing from the terminal is ever put in `innerHTML`.
- **The API key is server side only.** Read from `MUSE_SPARK_API_KEY`, never logged, never
  in an exception message or a response, and upstream error bodies are not forwarded.
- **Hints are never shown.** Lab-check hints can contain the exact fix. They reach the
  model as an answer key ("for your eyes only") and are stripped from `/state`. The panel
  shows only check names, pass/fail and the score.
- Page CSP: `default-src 'self'; frame-src 'self'`, inline script/style by per-response
  nonce. All responses are `Cache-Control: no-store`.
- Per booking: one request in flight and 10 per minute. Spend is capped.

## What was built during the hackathon (Nov 1-4, 2026)

Everything in this repository is new work created during the hackathon: the tutor
service, prompt builder and hint ladder, streaming client, spend meter, panel page,
mock mode, tests and evals. The lab broker and the gateway it mounts into are
pre-existing infrastructure and are not included here. The lab-host side, also new
(terminal recording, the `tutor` route module, a reference app with the same contract,
and the demo lab's graded checks), is in [`lab-host/`](lab-host/README-terminal-capture.md).

## Licence

MIT, see `LICENSE`.
