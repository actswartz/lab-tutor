"""Lab Tutor, broker side: what the student's terminal shows, made safe and short for a coaching model.

The workspace login shell (workspace/tools/lab-shell) runs under `script`, which appends everything the terminal
DISPLAYS (output only, never keystrokes: a password typed at a no-echo prompt is not recorded) to
<run_dir>/<sid>/term/typescript. term/ is its own small tmpfs owned by the student, so the student can write
anything there: this module opens the file without following links, reads only a bounded tail of a regular file,
and trims it in place so it never fills the tmpfs.

Self-contained (standard library only), so it can live in the public lab-tutor repo."""
from __future__ import annotations
import os, re, stat, time

TERM_DIR = "term"
TERM_FILE = "typescript"
TERM_TMPFS = "size=4m,nr_inodes=16,mode=0700,uid=1000,gid=1000,nosuid,nodev,noexec"
TAIL_CHARS = 8192             # what the tutor gets, after cleaning
READ_BYTES = 64 * 1024        # raw bytes read to produce it (escape codes and redraws shrink a lot)
TRIM_AT = 256 * 1024          # above this the file is cut back to its last KEEP bytes (script appends with O_APPEND)
KEEP = 64 * 1024

OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")          # window titles etc.
CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")                      # colours, cursor moves, erase line
ESC = re.compile(r"\x1b[()*+][0-9A-Za-z]|\x1b[@-Z\\^_=>78]")      # charset picks, keypad modes, other 2-byte escapes
BANNER = re.compile(r"^Script (started|done) on ")


def term_path(run_dir: str, sid: str) -> str:
    return os.path.join(run_dir, sid, TERM_DIR, TERM_FILE)


def _open(path, flags):
    """fd of a regular file at path, never through a link or onto a FIFO or device; None if there is none."""
    try:
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    return fd


def read_tail(path: str, n: int = READ_BYTES):
    """(last n bytes, file size, mtime) of the typescript, or (b"", 0, None) if there is no usable file."""
    fd = _open(path, os.O_RDONLY)
    if fd is None:
        return b"", 0, None
    try:
        s = os.fstat(fd)
        os.lseek(fd, max(0, s.st_size - n), os.SEEK_SET)
        return os.read(fd, n), s.st_size, s.st_mtime
    finally:
        os.close(fd)


def trim(path: str, at: int = TRIM_AT, keep: int = KEEP) -> bool:
    """Cut the typescript back to its last `keep` bytes once it is over `at`. `script -a` opened it O_APPEND, so its
    next write lands at the new end. Bytes written between the read and the truncate are lost (a few, at most)."""
    fd = _open(path, os.O_RDWR)
    if fd is None:
        return False
    try:
        size = os.fstat(fd).st_size
        if size <= at:
            return False
        os.lseek(fd, size - keep, os.SEEK_SET)
        tail = os.read(fd, keep)
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, tail)
        return True
    finally:
        os.close(fd)


def _line(s: str) -> str:
    """Replay one line the way a terminal draws it: \\r returns to column 0 and overwrites, backspace steps left
    (IOS erases `--More--` and readline redraws this way). Other control characters are dropped."""
    buf, pos = [], 0
    for ch in s:
        if ch == "\r":
            pos = 0
        elif ch == "\b":
            pos = max(0, pos - 1)
        elif ch == "\t" or ch >= " " and ch != "\x7f":
            if pos < len(buf):
                buf[pos] = ch
            else:
                buf.append(ch)
            pos += 1
    return "".join(buf).rstrip()


def clean(raw: bytes, limit: int = TAIL_CHARS, partial_start: bool = True) -> str:
    """Terminal bytes -> plain text: escape codes stripped, redraws replayed, `script` banners and runs of blank
    lines dropped, then the last `limit` characters cut at a line start. partial_start drops the first line (the
    read began mid-file, maybe inside an escape sequence)."""
    t = raw.decode("utf-8", "replace")
    t = CSI.sub("", OSC.sub("", t))
    t = ESC.sub("", t).replace("\x1b", "")
    lines = t.split("\n")
    if partial_start and len(lines) > 1:
        lines = lines[1:]
    out, blank = [], 0
    for ln in map(_line, lines):
        if BANNER.match(ln):
            continue
        blank = blank + 1 if not ln else 0
        if blank <= 1:
            out.append(ln)
    text = "\n".join(out).strip("\n")
    if len(text) > limit:
        text = text[-limit:]
        nl = text.find("\n")
        if 0 <= nl < len(text) - 1:
            text = text[nl + 1:]
    return text


def snapshot(run_dir: str, sid: str, lab: str, tasks: str, last_check, now=None) -> dict:
    """The tutor's view of one session (the body of GET /internal/v1/sessions/{sid}/tutor)."""
    path = term_path(run_dir, sid)
    trim(path)
    raw, size, mtime = read_tail(path)
    return {
        "session_id": sid,
        "lab_id": lab,
        "lab_tasks": tasks,
        "terminal_tail": clean(raw, partial_start=size > len(raw)),
        "terminal_updated_at": _iso(mtime),
        "capturing": os.path.isdir(os.path.dirname(path)),
        "last_check": last_check,
        "generated_at": _iso(now if now is not None else time.time()),
    }


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t else None
