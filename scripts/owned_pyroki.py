#!/usr/bin/env python3
"""Own a pinned original PyRoKi server and expose local HTTP readiness.

Runs in the declared PyRoKi dependency environment. Only this child is stopped;
existing shared solvers and GraspGen instances are never touched.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--http-port", type=int, required=True)
    parser.add_argument("--zmq-port", type=int, required=True)
    parser.add_argument("--gpu", type=int, required=True)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--robot-cache", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    if not (source / "server.py").is_file():
        raise FileNotFoundError(source / "server.py")
    # Explicit port allocation fails closed instead of borrowing or replacing a
    # solver which may already be serving another experiment.
    with socket.socket() as check:
        check.bind(("127.0.0.1", args.zmq_port))
    env = dict(os.environ, ROBORSI_PYROKI_PORT=str(args.zmq_port), CUDA_VISIBLE_DEVICES=str(args.gpu),
               XLA_PYTHON_CLIENT_PREALLOCATE="false", PYTHONDONTWRITEBYTECODE="1",
               ROBOT_DESCRIPTIONS_CACHE=str(args.robot_cache.absolute()))
    launcher = "import runpy,sys; sys.path.insert(0,sys.argv[1]); runpy.run_path(sys.argv[1]+'/server.py',run_name='__main__')"
    child = subprocess.Popen([sys.executable, "-I", "-B", "-c", launcher, str(source)], env=env,
        stdin=subprocess.DEVNULL, start_new_session=True)
    record = {"pid": child.pid, "source": str(source), "port": args.zmq_port, "ownership": "owned", "closed": False}
    args.record.write_text(json.dumps(record, indent=2))
    stopping, ready = threading.Event(), threading.Event()
    for sig in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(sig, lambda *_: stopping.set())
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            live = ready.is_set() and child.poll() is None
            self.send_response(200 if live else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ready": live, "zmq_port": args.zmq_port}).encode())
    http = ThreadingHTTPServer(("127.0.0.1", args.http_port), Handler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    try:
        while not stopping.wait(.1):
            if child.poll() is not None:
                raise RuntimeError("Owned PyRoKi process exited")
            if not ready.is_set():
                with socket.socket() as sock:
                    sock.settimeout(.1)
                    if sock.connect_ex(("127.0.0.1", args.zmq_port)) == 0:
                        ready.set()
    finally:
        http.shutdown()
        http.server_close()
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(5)
        record.update(closed=True, exit_code=child.poll())
        args.record.write_text(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
