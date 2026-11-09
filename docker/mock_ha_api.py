"""Mock Home Assistant REST API server for local development and integration tests."""

from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse


class MockHomeAssistantState:
    """Thread-safe state tracking Home Assistant restart calls and installed custom components."""

    def __init__(self, expected_api_key: str = "test-ha-api-token", config_dir: str = "/config") -> None:
        self._lock = threading.Lock()
        self.expected_api_key = expected_api_key
        self.config_dir = Path(config_dir)
        self.restarts: List[Dict[str, Any]] = []

    @property
    def custom_components_dir(self) -> Path:
        return self.config_dir / "custom_components"

    def record_restart(self) -> Dict[str, Any]:
        with self._lock:
            event = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "count": len(self.restarts) + 1,
            }
            self.restarts.append(event)
            return event

    def reset(self, clear_components: bool = True) -> None:
        with self._lock:
            self.restarts.clear()
            if clear_components and self.custom_components_dir.exists():
                for child in self.custom_components_dir.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            installed_domains: Dict[str, Any] = {}
            cc_dir = self.custom_components_dir
            if cc_dir.is_dir():
                for child in sorted(cc_dir.iterdir()):
                    if child.is_dir() and not child.name.startswith("."):
                        manifest_path = child / "manifest.json"
                        manifest_data = None
                        if manifest_path.is_file():
                            try:
                                manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
                            except Exception:
                                manifest_data = None
                        installed_domains[child.name] = {
                            "has_manifest": manifest_path.is_file(),
                            "manifest": manifest_data,
                            "files": sorted(
                                str(p.relative_to(child))
                                for p in child.rglob("*")
                                if p.is_file()
                            ),
                        }
            lockfile_path = cc_dir / ".ha-ccm-lock.json"
            lockfile_data = None
            if lockfile_path.is_file():
                try:
                    lockfile_data = json.loads(lockfile_path.read_text(encoding="utf-8"))
                except Exception:
                    lockfile_data = None

            return {
                "restart_count": len(self.restarts),
                "restarts": list(self.restarts),
                "custom_components": installed_domains,
                "lockfile": lockfile_data,
            }


def make_ha_handler(state: MockHomeAssistantState) -> type[BaseHTTPRequestHandler]:
    """Create an HTTP handler bound to a MockHomeAssistantState."""

    class MockHomeAssistantHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            if os.environ.get("MOCK_SERVER_QUIET") == "1":
                return
            super().log_message(format, *args)

        def _send_json(self, status_code: int, payload: Any) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _is_authorized(self) -> bool:
            auth = self.headers.get("Authorization", "")
            return auth == f"Bearer {state.expected_api_key}"

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path in ("/api", "/api/"):
                self._send_json(200, {"message": "API running."})
                return
            if path == "/_test/state":
                self._send_json(200, state.snapshot())
                return
            self._send_json(404, {"message": f"Not found: {path}"})

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length > 0 else b"{}"
            try:
                data = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except Exception:
                data = {}

            if path == "/api/services/homeassistant/restart":
                if not self._is_authorized():
                    self._send_json(401, {"message": "401: Unauthorized"})
                    return
                state.record_restart()
                self._send_json(200, [])
                return

            if path == "/_test/reset":
                clear_cc = bool(data.get("clear_components", True))
                state.reset(clear_components=clear_cc)
                self._send_json(200, state.snapshot())
                return

            self._send_json(404, {"message": f"Not found: {path}"})

    return MockHomeAssistantHandler


def start_mock_ha_server(
    host: str = "0.0.0.0",
    port: int = 8123,
    expected_api_key: str = "test-ha-api-token",
    config_dir: str = "/config",
) -> tuple[ThreadingHTTPServer, MockHomeAssistantState]:
    """Create and return a (ThreadingHTTPServer, MockHomeAssistantState) tuple."""
    state = MockHomeAssistantState(expected_api_key=expected_api_key, config_dir=config_dir)
    server = ThreadingHTTPServer((host, port), make_ha_handler(state))
    return server, state


def main() -> None:
    host = os.environ.get("MOCK_HA_HOST", "0.0.0.0")
    port = int(os.environ.get("MOCK_HA_PORT", "8123"))
    api_key = os.environ.get("HA_API_KEY", "test-ha-api-token")
    config_dir = os.environ.get("HA_CONFIG_DIR", "/config")
    Path(config_dir, "custom_components").mkdir(parents=True, exist_ok=True)
    server, _ = start_mock_ha_server(host, port, expected_api_key=api_key, config_dir=config_dir)
    print(f"Mock Home Assistant API listening on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
