"""Loopback-only, GET-only presentation preview using the production artifact reader.

Run with backend/.venv/Scripts/python.exe. No App/bootstrap, database or collector runs.
Only the comparison page, its shared assets and the fixed comparison API are served.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.services import coarse_model_comparison as comparison_service  # noqa: E402

SERVICE_LOCK = Lock()

FILES = {
    "/": ("coarse-comparison-preview.html", "text/html; charset=utf-8"),
    "/coarse-comparison-preview.html": ("coarse-comparison-preview.html", "text/html; charset=utf-8"),
    "/css/style.css": ("css/style.css", "text/css; charset=utf-8"),
    "/css/coarse-comparison.css": ("css/coarse-comparison.css", "text/css; charset=utf-8"),
    "/js/coarse-comparison.js": ("js/coarse-comparison.js", "application/javascript; charset=utf-8"),
}


class PreviewHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        route = urlsplit(self.path).path
        if route == "/api/v1/training/coarse-model-comparison":
            # This local authoring preview must also pick up ongoing schema updates.
            with SERVICE_LOCK:
                importlib.reload(comparison_service)
                body = json.dumps(comparison_service.load_comparison(), ensure_ascii=False, allow_nan=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif route in FILES:
            relative, content_type = FILES[route]
            body = (ROOT / "frontend" / relative).read_bytes()
        else:
            self.send_error(404, "This read-only preview serves only the comparison page and its assets.")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def reject_write(self):
        self.send_response(405)
        self.send_header("Allow", "GET")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_PUT = do_PATCH = do_DELETE = reject_write


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), PreviewHandler)
    print(f"Read-only preview: http://127.0.0.1:{args.port}/ (PID {os.getpid()})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
