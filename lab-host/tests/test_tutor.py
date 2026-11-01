"""Lab Tutor broker module: terminal record cleaning, safe reads and trims. Run: pytest -q tests"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "broker"))
import tutor  # noqa: E402


def test_clean_strips_escapes_and_replays_redraws():
    raw = (b"\x1b]0;student@ws: ~\x07\x1b[01;32mstudent@ws\x1b[00m:~$ ssh r1\r\n"
           b"R1#show ip int brief\r\n"
           b"Interface   IP-Address  OK? Method Status                Protocol\r\n"
           b"Ethernet0/1 10.1.2.1    YES manual administratively down down    \r\n"
           b" --More-- \b\b\b\b\b\b\b\b\b\b          \b\b\b\b\b\b\b\b\b\bEthernet0/2 unassigned\r\n"
           b"R1#shw\b\bhow run\r\n\r\n\r\n\r\n"
           b"\x1b[?2004hR1#\x1b(B\x1b=\x07\r\n")
    t = tutor.clean(raw, partial_start=False)
    assert "\x1b" not in t and "\x07" not in t and "\r" not in t and "\b" not in t
    lines = t.split("\n")
    assert lines[0] == "student@ws:~$ ssh r1"
    assert "Ethernet0/1 10.1.2.1    YES manual administratively down down" in lines
    assert "Ethernet0/2 unassigned" in lines            # --More-- erased by backspaces and spaces
    assert "R1#show run" in lines                        # the typo fixed with backspaces
    assert "\n\n\n" not in t                             # runs of blank lines collapse to one
    assert lines[-1] == "R1#"


def test_clean_tail_limit_and_banners():
    raw = b"Script started on 2026-11-01 [TERM=xterm]\n" + b"".join(b"line %d\r\n" % i for i in range(5000))
    t = tutor.clean(raw, limit=1000, partial_start=False)
    assert len(t) <= 1000 and t.endswith("line 4999") and t.split("\n")[0].startswith("line ")
    assert "Script started" not in t
    mid = tutor.clean(b"\x1b[31mhalf-line\nwhole line\n", partial_start=True)
    assert mid == "whole line"                           # a read that began mid-file drops its first line


def test_read_and_trim_refuse_links_and_fifos(tmp_path):
    secret = tmp_path / "host-secret"
    secret.write_text("do not read")
    link = tmp_path / "typescript"
    link.symlink_to(secret)
    assert tutor.read_tail(str(link)) == (b"", 0, None)
    assert tutor.trim(str(link), at=0, keep=1) is False and secret.read_text() == "do not read"
    link.unlink()
    os.mkfifo(link)                                      # opening a FIFO must not hang the broker
    assert tutor.read_tail(str(link)) == (b"", 0, None) and tutor.trim(str(link), at=0) is False
    link.unlink()
    assert tutor.read_tail(str(link)) == (b"", 0, None)


def test_trim_keeps_the_tail_and_appends_continue(tmp_path):
    p = tmp_path / "typescript"
    with open(p, "ab") as w:                             # `script -a` keeps the file open with O_APPEND
        w.write(b"a" * 300 + b"b" * 100); w.flush()
        assert tutor.trim(str(p), at=200, keep=100) is True
        assert p.read_bytes() == b"b" * 100
        w.write(b"NEW"); w.flush()
    assert p.read_bytes() == b"b" * 100 + b"NEW"
    assert tutor.trim(str(p), at=200, keep=100) is False
    raw, size, mtime = tutor.read_tail(str(p), 5)
    assert raw == b"bbNEW" and size == 103 and mtime
