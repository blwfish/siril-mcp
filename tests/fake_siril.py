#!/usr/bin/env python3
"""Stand-in for `siril-cli -p -r <in> -w <out> -d <dir>`, speaking the pipe protocol.

Protocol (README "Pipe protocol"): "ready" once, then per command
"status: starting <verb>", zero or more "log: ..." lines, "status: success|error <verb>"
where <verb> is the command's first word (as real Siril 1.4.5 echoes it).

Behaviour is chosen by the command's first word so tests can provoke each case:
  fail          status: error with no log line (real Siril does this for `stat` with no image)
  failwithlog   log: boom, then status: error
  echo X        log: X, then success
  multi         log: line1 / unprefixed continuation line / log: line3 (real `help` output shape)
  bare          a bare "log:" line (no trailing space), then log: after
  progress      a "progress:" line (an unknown-to-the-log prefix), then log: done
  weird         a status line echoing a different command, then log: after, then success
  weirdterm     status: cancelled <verb> (a terminal-looking word outside the vocabulary)
  late          sleep FAKE_SIRIL_DELAY (default 1.0s), then log: late-output and success
  chatty        6 log lines 0.2s apart, then success (total run time longer than a short timeout)
  split         a log line delivered in two writes with a pause between them
  partial       a 50-byte log line with no newline, a pause, then the newline
  badbytes      a log line containing invalid UTF-8
  spawnchild    starts a `sleep 60` child (pid written to FAKE_SIRIL_CHILD_PID), then success
  exit          status: starting only, then the process exits (real Siril emits no terminal status)
  die           status: starting only, then the process dies abruptly with code 1
  savejpg "P" N writes P.jpg (stand-in bytes) unless FAKE_SIRIL_NOFILE is set, then success
  anything else success with no log
Env: FAKE_SIRIL_LOG=<file> appends every received command line;
     FAKE_SIRIL_FAIL=a,b / FAKE_SIRIL_LATE=a,b make commands starting with those words fail / be late;
     FAKE_SIRIL_PREREADY="a|b" emits those lines before `ready`; FAKE_SIRIL_IGNORE_EXIT=1 makes `exit` hang;
     FAKE_SIRIL_VERSION overrides the `--version` output; FAKE_SIRIL_DIE_AT_START=1 prints to stderr
     and exits before `ready`.
"""
import os
import subprocess
import sys
import time

FAKE_JPEG = b"\xff\xd8fakejpeg"


def main() -> None:
    args = sys.argv[1:]
    if "--version" in args:
        print(os.environ.get("FAKE_SIRIL_VERSION", "siril 1.4.5-fake"))
        return
    if os.environ.get("FAKE_SIRIL_DIE_AT_START"):
        print("boom-from-stderr", file=sys.stderr, flush=True)
        sys.exit(3)
    in_path = args[args.index("-r") + 1]
    out_path = args[args.index("-w") + 1]
    log_path = os.environ.get("FAKE_SIRIL_LOG")
    fail_words = set(filter(None, os.environ.get("FAKE_SIRIL_FAIL", "").split(",")))
    late_words = set(filter(None, os.environ.get("FAKE_SIRIL_LATE", "").split(",")))
    delay = float(os.environ.get("FAKE_SIRIL_DELAY", "1.0"))

    fin = os.open(in_path, os.O_RDWR)
    fout = os.open(out_path, os.O_RDWR)

    def emit(*lines: str) -> None:
        os.write(fout, "".join(line + "\n" for line in lines).encode())

    for line in filter(None, os.environ.get("FAKE_SIRIL_PREREADY", "").split("|")):
        emit(line)
    emit("ready")
    buf = b""
    while True:
        chunk = os.read(fin, 4096)
        if not chunk:
            return
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            cmd = raw.decode()
            if log_path:
                with open(log_path, "a") as f:
                    f.write(cmd + "\n")
            name = cmd.split(" ", 1)[0]
            ok = f"status: success {name}"
            emit(f"status: starting {name}")
            if name == "exit":
                if os.environ.get("FAKE_SIRIL_IGNORE_EXIT"):
                    continue  # started, but never exits and never finishes
                return
            if name == "die":
                os._exit(1)
            if name == "late" or name in late_words:
                time.sleep(delay)
                emit("log: late-output", ok)
            elif name == "fail" or name in fail_words:
                emit(f"status: error {name}")
            elif name == "failwithlog":
                emit("log: boom", f"status: error {name}")
            elif name == "echo":
                emit(f"log: {cmd.split(' ', 1)[1]}", ok)
            elif name == "multi":
                emit("log: line1", "continuation line 2", "log: line3", ok)
            elif name == "bare":
                emit("log:", "log: after", ok)
            elif name == "progress":
                emit("progress: 50%", "log: done", ok)
            elif name == "weird":
                emit("status: weird x", "log: after", ok)
            elif name == "weirdterm":
                emit(f"status: cancelled {name}")
            elif name == "chatty":
                for i in range(6):
                    emit(f"log: tick {i}")
                    time.sleep(0.2)
                emit(ok)
            elif name == "split":
                os.write(fout, b"log: hel")
                time.sleep(0.2)
                os.write(fout, b"lo\n")
                emit(ok)
            elif name == "partial":
                os.write(fout, b"log: " + b"x" * 45)
                time.sleep(0.3)
                os.write(fout, b"\n")
                emit(ok)
            elif name == "spawnchild":
                child = subprocess.Popen(["sleep", "60"])  # same process group as this fake
                with open(os.environ["FAKE_SIRIL_CHILD_PID"], "w") as f:
                    f.write(str(child.pid))
                emit(ok)
            elif name == "badbytes":
                os.write(fout, b"log: caf\xe9\n")
                emit(ok)
            elif name == "savejpg":
                if not os.environ.get("FAKE_SIRIL_NOFILE"):
                    with open(cmd.split('"')[1] + ".jpg", "wb") as f:
                        f.write(FAKE_JPEG)
                emit(ok)
            else:
                emit(ok)


if __name__ == "__main__":
    main()
