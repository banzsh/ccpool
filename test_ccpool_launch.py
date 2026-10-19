#!/usr/bin/env python3
"""Self-check for `ccpool launch`. `python3 test_ccpool_launch.py` -- no framework.

Runs the real CLI against a fake tool, on free ports and a throwaway
CCPOOL_HOME, so the proxies it starts never meet a real account or port 8790.

What has to hold:

  * proxy down       -> started detached, the tool runs pointed at it
  * proxy up         -> reused, never started twice
  * two at once      -> one proxy serves both
  * port held by something else -> refused, and the tool never runs
"""
import http.server
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{'' if cond else ' -- ' + str(detail)}")
    if not cond:
        FAILURES.append(name)


class Other(http.server.BaseHTTPRequestHandler):
    """Something that is not ccpool on the port: it answers GET with a 501."""

    def log_message(self, *a):
        pass


class OldProxy(Other):
    """A ccpool proxy from before the health path: it relays the path upstream,
    so the answer is Anthropic's 404 under the proxy's own Server header."""
    server_version = "ccpool"

    def do_GET(self):
        self.send_error(404)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def launch(env, *args):
    return subprocess.Popen(
        [sys.executable, str(HERE / "ccpool.py"), "launch", *args], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def finish(proc):
    out, err = proc.communicate(timeout=30)
    return proc.returncode, out, err


def ran(out):
    """What the fake tool recorded, one entry per run."""
    if not out.exists():
        return []
    return [json.loads(line) for line in out.read_text().splitlines()]


def proxies(port):
    r = subprocess.run(["pgrep", "-f", f"ccpool.py proxy --port {port}$"],
                       capture_output=True, text=True)
    return r.stdout.split()


def main():
    import ccpool_proxy as P

    home = Path(tempfile.mkdtemp())
    out = home / "ran.jsonl"
    tool = home / "tool"
    tool.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(out)!r}, 'a') as f:\n"
        "    f.write(json.dumps({'argv': sys.argv[1:],\n"
        "                        'base': os.environ.get('ANTHROPIC_BASE_URL')}) + '\\n')\n")
    tool.chmod(0o755)
    (home / "vault.json").write_text(json.dumps(
        {"accounts": {"one": {"token": "tok-one"}}}))
    env = {**os.environ, "CCPOOL_HOME": str(home), "CCPOOL_CLAUDE": str(tool),
           "CCPOOL_UPSTREAM": "http://127.0.0.1:9"}

    port, raced = free_port(), free_port()
    other = http.server.HTTPServer(("127.0.0.1", 0), Other)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    old = http.server.HTTPServer(("127.0.0.1", 0), OldProxy)
    threading.Thread(target=old.serve_forever, daemon=True).start()
    empty = Path(tempfile.mkdtemp())

    try:
        print("\nProxy down: launch starts it, then runs claude through it")
        rc, so, se = finish(launch(env, "--port", str(port), "--", "--model", "opus"))
        check("exits with the tool's status", rc == 0, f"{rc} {se}")
        check("claude gets the args after --, pointed at the proxy",
              ran(out)[-1:] == [{"argv": ["--model", "opus"],
                                 "base": f"http://127.0.0.1:{port}"}], ran(out))
        first = P.probe(port)
        check("the proxy outlives the session", first is not None, first)
        check("launch prints nothing of its own", so == "" and se == "", (so, se))
        check("the proxy's banner went to its log",
              "ccpool-proxy on" in (home / "proxy.log").read_text())

        print("\nProxy up: launch reuses it and runs the named tool")
        rc, so, se = finish(launch(env, "--port", str(port), str(tool), "--", "again"))
        check("exits with the tool's status", rc == 0, f"{rc} {se}")
        check("the tool gets the args after --", ran(out)[-1]["argv"] == ["again"],
              ran(out))
        check("same proxy, not a second one", P.probe(port) == first, P.probe(port))

        print("\nTwo launches at once: one proxy serves both")
        before = len(ran(out))
        rcs = [finish(p)[0] for p in [launch(env, "--port", str(raced))
                                      for _ in range(2)]]
        check("both run their tool", rcs == [0, 0] and len(ran(out)) == before + 2,
              (rcs, ran(out)[before:]))
        deadline = time.monotonic() + 5
        while len(proxies(raced)) > 1 and time.monotonic() < deadline:
            time.sleep(0.1)
        check("exactly one proxy is left on the port", len(proxies(raced)) == 1,
              proxies(raced))

        print("\nAn older ccpool proxy on the port: reused, not refused")
        rc, so, se = finish(launch(env, "--port", str(old.server_port)))
        check("exits with the tool's status", rc == 0, f"{rc} {se}")
        check("the tool runs pointed at it",
              ran(out)[-1]["base"] == f"http://127.0.0.1:{old.server_port}", ran(out))
        check("no proxy of its own was started", proxies(old.server_port) == [],
              proxies(old.server_port))

        print("\nPort held by something else: refused, the tool never runs")
        before = len(ran(out))
        rc, so, se = finish(launch(env, "--port", str(other.server_port)))
        check("exits 2", rc == 2, f"{rc} {se}")
        check("says the port is not a ccpool proxy", "ccpool-proxy" in se, se)
        check("the tool did not run", len(ran(out)) == before, ran(out)[before:])

        print("\nNo accounts: refused before anything starts")
        rc, so, se = finish(launch({**env, "CCPOOL_HOME": str(empty)},
                                   "--port", str(free_port())))
        check("exits 2 and says so", rc == 2 and "no accounts" in se, f"{rc} {se}")

        print("\nTool missing: reported, not a traceback")
        rc, so, se = finish(launch(env, "--port", str(port), str(home / "nope")))
        check("exits 127 naming the tool", rc == 127 and "nope" in se
              and "Traceback" not in se, f"{rc} {se}")
    finally:
        other.shutdown(); old.shutdown()
        for p in (port, raced, old.server_port):
            for pid in proxies(p):
                os.kill(int(pid), signal.SIGTERM)
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(empty, ignore_errors=True)

    print(f"\n{'FAILED: ' + ', '.join(FAILURES) if FAILURES else 'all checks passed'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
