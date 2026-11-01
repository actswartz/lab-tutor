"""Reference wiring for the tutor route (the production lab broker is private; this is the same contract).
  TUTOR_SECRET=... RUN_DIR=/var/lib/lab/run uvicorn reference_app:app
GET /internal/v1/sessions/{sid}/tutor, signed like the gateway's other calls:
  X-Timestamp: unix seconds (+-300 s), X-Signature: hex HMAC-SHA256 over "{ts}\\nGET\\n{path}\\n" (empty body).
Lab tasks and the last check result come from callbacks here; the broker passes its own."""
import hashlib, hmac, os, re, time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import tutor

SECRET = os.environ.get("TUTOR_SECRET", "")
RUN_DIR = os.environ.get("RUN_DIR", "/var/lib/lab/run")
SID_RE = re.compile(r"^[0-9a-f]{32}$")
app = FastAPI()


def lab_of(sid):            # the broker looks these up in its session store
    return os.environ.get("LAB_ID", "iol01"), os.environ.get("LAB_TASKS", "")


def last_check_of(sid):     # the broker keeps the latest graded `lab check` result per session, in memory
    return None


def signed(request: Request) -> bool:
    ts, sig = request.headers.get("x-timestamp", ""), request.headers.get("x-signature", "").lower()
    if not SECRET or not ts.isdigit() or abs(time.time() - int(ts)) > 300:
        return False
    exp = hmac.new(SECRET.encode(), f"{ts}\n{request.method}\n{request.url.path}\n".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(exp, sig)


@app.get("/internal/v1/sessions/{sid}/tutor")
async def tutor_view(sid: str, request: Request):
    if not signed(request):
        return JSONResponse({"error": "bad_signature"}, status_code=401)
    if not SID_RE.match(sid) or not os.path.isdir(os.path.join(RUN_DIR, sid)):
        return JSONResponse({"error": "not_found"}, status_code=404)
    lab, tasks = lab_of(sid)
    return JSONResponse(tutor.snapshot(RUN_DIR, sid, lab, tasks, last_check_of(sid)), headers={"Cache-Control": "no-store"})
