# Terminal capture and the tutor route (lab host side)

**Built during the hackathon (Nov 1, 2026).** Everything in this folder is new code. The lab broker and gateway it plugs into existed before the hackathon and stay private; they are only the stage. The private repo's commit log is the record:

| Commit | Time (ET) | What |
|---|---|---|
| e913a74 | Nov 1, 07:35 | Terminal capture (`lab-shell`, `tutor.py`), the tutor route, tests |
| 0221985 | Nov 1, 08:14 | Demo lab `iol01` and its graded check on a real IOS router (`checks/iol-link.yaml`) |
| 2f6a9bc | Nov 1, 08:46 | Tutor tests added to the lab host's install test |

## What it does
A student works in a hosted lab: a terminal in the browser (Guacamole over SSH) into a Linux workspace, from which they log in to a real Cisco IOS router. Lab Tutor needs to see what the student sees. This side:
1. **Records the terminal.** sshd's `ForceCommand` runs `workspace/lab-shell`. For an interactive login, it starts the shell under `script -qfa`, which appends everything the terminal *displays* to `/run/lab/term/typescript`. That includes the router's output, because the router session runs inside that terminal. Keystrokes are **not** recorded (no `-I`), so a password typed at a no-echo prompt never reaches the record or the model.
2. **Serves a clean view.** `GET /internal/v1/sessions/{sid}/tutor` (HMAC-signed) returns the lab's tasks, the last 8 KiB of the terminal as plain text, and the latest graded `lab check` result. `broker/tutor.py` builds it:
   - strips escape codes;
   - replays `\r` and backspace the way a terminal draws them (IOS `--More--`, readline corrections);
   - drops banners and runs of blank lines;
   - cuts the text at a line start.
3. **Grades on the real router.** `checks/iol-link.yaml` is the demo task: Ethernet0/2 gets 10.20.32.1/24 and comes up. It runs two checks over SSH with Netmiko. Its hints spell out the fix; the tutor treats them as a private answer key.

## Safety (the student controls the record file)
- **The record is a per-session 4 MiB tmpfs** (nosuid, nodev, noexec), owned by the student. A runaway `cat` can't fill the host disk, and a hard link to a host file can't exist on it.
- **Safe reads.** The host opens the file with `O_NOFOLLOW | O_NONBLOCK` and reads only regular files, so a symlink to a host file or a FIFO is refused. It reads at most 64 KiB.
- **Trimmed in place.** Past 256 KiB the file is cut back to its last 64 KiB. `script -a` writes with O_APPEND, so recording carries on.
- **Treated as data.** The terminal text is student-written: HTML-escape it in any page, and keep it in the prompt's data section.
- **Nothing outlives the session.** The record and the check result are dropped when the session ends.

## Files
| Path | |
|---|---|
| `broker/tutor.py` | the module (standard library only) |
| `broker/reference_app.py` | a minimal FastAPI app serving the route with the same HMAC contract |
| `workspace/lab-shell` | the SSH login wrapper (`ForceCommand /usr/local/bin/lab-shell` in sshd_config) |
| `checks/iol-link.yaml` | the demo lab's graded checks |
| `tests/` | `pytest -q tests`: 5 tests (needs fastapi and httpx for the reference app test) |

## Route contract
`200`:
```json
{"session_id": "…", "lab_id": "iol01", "lab_tasks": "…", "terminal_tail": "corvane-r3#show ip interface brief\n…",
 "terminal_updated_at": "2026-11-01T13:11:20Z", "capturing": true,
 "last_check": {"score": 0, "max": 2, "passed": false, "at": "…", "checks": [{"name": "…", "ok": false, "hint": "…"}]},
 "generated_at": "…"}
```
`last_check` is `null` before the first `lab check`, and `hint` is `""` on passing checks.

Other statuses: `401` bad signature, `404` unknown session, `409` session not ready.

**Signature:** `X-Timestamp` (unix seconds, within ±300 s) and `X-Signature` = hex HMAC-SHA256 over `"{ts}\nGET\n{path}\n"`.

**Known limit:** if a typed command is longer than the terminal is wide, readline's redraw can garble that echoed line. Output lines are not affected.
