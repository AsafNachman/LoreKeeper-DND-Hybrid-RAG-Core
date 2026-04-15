"""
Lightweight HTTP health endpoint for Docker / orchestration.

GET /health returns JSON: ok, chroma_ok, openai_ok, detail.

Uses HEALTH_PORT (default 8080). Start via start_health_server_background() from app startup.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_server_lock = threading.Lock()
_server_started = False


def _check_chroma(db_path: str) -> tuple[bool, str]:
    """Lightweight persist check (avoids loading embedding models on every probe)."""
    p = Path(db_path)
    if not p.is_dir():
        return False, f"db path missing: {db_path}"
    try:
        if (p / "chroma.sqlite3").exists() or any(p.glob("*.sqlite3")):
            return True, "chroma persist present"
        return True, "db dir exists (empty until first ingest)"
    except Exception as exc:
        return False, str(exc)


def _check_openai() -> tuple[bool, str]:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return False, "OPENAI_API_KEY unset"
    try:
        req = urllib.request.Request(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            if 200 <= resp.status < 300:
                return True, "models reachable"
        return False, f"unexpected status {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as exc:
        return False, str(exc)


class _HealthHandler(BaseHTTPRequestHandler):
    db_path = "db"

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("%s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        if self.path not in ("/health", "/"):
            self.send_error(404)
            return

        chroma_ok, chroma_detail = _check_chroma(self.db_path)
        openai_ok, openai_detail = _check_openai()
        ok = chroma_ok and openai_ok
        body = {
            "ok": ok,
            "chroma_ok": chroma_ok,
            "chroma": chroma_detail,
            "openai_ok": openai_ok,
            "openai": openai_detail,
        }
        payload = json.dumps(body).encode("utf-8")
        self.send_response(200 if ok else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def start_health_server_background(
    port: int | None = None,
    db_path: str = "db",
) -> None:
    """Bind once per process (idempotent). Daemon thread."""
    global _server_started
    with _server_lock:
        if _server_started:
            return
        p = port or int(os.getenv("HEALTH_PORT", "8080"))
        _HealthHandler.db_path = db_path

        httpd = HTTPServer(("0.0.0.0", p), _HealthHandler)

        def _run() -> None:
            logger.info("Health server listening on 0.0.0.0:%s", p)
            httpd.serve_forever()

        t = threading.Thread(target=_run, name="health-server", daemon=True)
        t.start()
        _server_started = True

