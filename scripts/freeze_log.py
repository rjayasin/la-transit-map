"""Serve the map and record what the page reports about itself, so a freeze
leaves evidence somewhere other than the tab that froze.

The freeze this exists for takes the tab with it: the console stops, the
devtools pane stops, and the only cure is closing the tab — which throws away
everything that would say why. So the page ships its telemetry out of the
process as it goes, and this server writes it to a file. Whatever is in that
file when the tab dies is the record, and it is still there afterwards.

Drop-in replacement for the README's `python3 -m http.server 8741`: it serves
the repo the same way and adds one endpoint.

    .venv/bin/python scripts/freeze_log.py          # serve + record on :8741
    .venv/bin/python scripts/freeze_report.py       # read back what it caught

    POST /_trace   newline-delimited JSON, appended to scratch/freeze-trace.jsonl
    GET  /_trace   the file back, for reading it from the browser

Every response also carries the two headers that make the page
cross-origin-isolated, which is what unlocks
performance.measureUserAgentSpecificMemory() — a breakdown of what the tab is
actually holding, by type, which is the one measurement that would have settled
this in the first place. Chrome only; harmless elsewhere.
"""
import argparse
import datetime
import http.server
import json
import os
import socketserver
import sys
import threading

LOG = "scratch/freeze-trace.jsonl"
PORT = 8741
MAX_BODY = 4 << 20      # 4 MB; a batch is a few KB, so this is only a sanity cap

_lock = threading.Lock()


class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def end_headers(self):
        # Cross-origin isolation, for measureUserAgentSpecificMemory(). Every
        # asset here is same-origin, so require-corp costs nothing.
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        # The whole point is to reload into freshly instrumented code, so never
        # let the page or its script be answered out of cache.
        if self.path.rstrip("/").endswith((".html", "")) or self.path in ("/", ""):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_POST(self):
        if self.path != "/_trace":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            self.send_error(400)
            return
        body = self.rfile.read(n)
        stamp = datetime.datetime.now().isoformat(timespec="milliseconds")
        with _lock:
            os.makedirs(os.path.dirname(LOG) or ".", exist_ok=True)
            with open(LOG, "a") as f:
                for line in body.decode("utf-8", "replace").splitlines():
                    if line.strip():
                        f.write(json.dumps({"rx": stamp, **safe(line)}) + "\n")
                f.flush()
                os.fsync(f.fileno())      # the tab may die a moment from now
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *a):       # one line per POST would drown the console
        if self.command == "POST":
            return
        super().log_message(fmt, *a)


def safe(line):
    try:
        v = json.loads(line)
        return v if isinstance(v, dict) else {"value": v}
    except Exception:
        return {"raw": line}


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", type=int, default=PORT)
    ap.add_argument("--fresh", action="store_true", help="truncate the log first")
    a = ap.parse_args()
    if not os.path.exists("index.html"):
        sys.exit("run from the repo root")
    os.makedirs(os.path.dirname(LOG) or ".", exist_ok=True)
    if a.fresh and os.path.exists(LOG):
        os.remove(LOG)
    with Server(("127.0.0.1", a.port), Handler) as srv:
        print(f"serving {os.getcwd()} on http://localhost:{a.port}")
        print(f"recording POSTs to {LOG}")
        print(f"open http://localhost:{a.port}/index.html?trace=1 and reproduce the freeze")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


if __name__ == "__main__":
    main()
