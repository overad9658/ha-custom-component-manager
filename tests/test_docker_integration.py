"""End-to-end integration tests using the mock GitHub API, mock Home Assistant API, and Docker SSH target."""

from __future__ import annotations

import io
import json
import os
import socket
import threading
from pathlib import Path
from typing import Any, Dict, Generator
from urllib.parse import urlparse

import pytest
import requests
from click.testing import CliRunner

from docker.mock_github_server import (
    MockGitHubState,
    build_tarball_bytes,
    make_github_handler,
    start_mock_github_server,
)
from docker.mock_ha_api import (
    MockHomeAssistantState,
    make_ha_handler,
    start_mock_ha_server,
)
from ha_ccm.cli import cli


class _InMemoryHTTPResponse:
    """Minimal requests.Response-compatible object for environments that block loopback sockets."""

    def __init__(self, status_code: int, content: bytes, headers: Dict[str, str]) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = headers
        self.text = content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}: {self.text}")


def _dispatch_handler_in_memory(
    handler_cls: type,
    method: str,
    url: str,
    headers: Dict[str, str] | None = None,
    json_body: Any = None,
) -> _InMemoryHTTPResponse:
    """Invoke a BaseHTTPRequestHandler subclass in memory without opening a TCP socket."""
    parsed = urlparse(url)
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    body_bytes = json.dumps(json_body).encode("utf-8") if json_body is not None else b""

    req_lines = [f"{method} {path} HTTP/1.1", "Host: localhost"]
    merged_headers = dict(headers or {})
    if body_bytes:
        merged_headers["Content-Length"] = str(len(body_bytes))
    for k, v in merged_headers.items():
        req_lines.append(f"{k}: {v}")
    raw_request = ("\r\n".join(req_lines) + "\r\n\r\n").encode("utf-8") + body_bytes

    rfile = io.BytesIO(raw_request)
    wfile = io.BytesIO()

    class _DummySocket:
        def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
            return rfile if "r" in mode else wfile

        def sendall(self, data: bytes) -> None:
            wfile.write(data)

    handler_cls(_DummySocket(), ("127.0.0.1", 0), None)
    raw_resp = wfile.getvalue()
    header_block, _, resp_body = raw_resp.partition(b"\r\n\r\n")
    header_lines = header_block.decode("iso-8859-1").split("\r\n")
    status_code = int(header_lines[0].split(" ")[1])
    resp_headers: Dict[str, str] = {}
    for line in header_lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            resp_headers[k.strip()] = v.strip()
    return _InMemoryHTTPResponse(status_code, resp_body, resp_headers)


@pytest.fixture()
def integration_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[Dict[str, str], None, None]:
    """Provide URLs for the mock GitHub and Home Assistant servers.

    When running inside Docker Compose (HA_CCM_DOCKER_TEST=1), uses the live
    github-mock and ha-instance containers. Otherwise starts ephemeral local
    HTTP servers in background threads (falling back to in-memory handler
    dispatch if loopback TCP connections are restricted by a sandbox).
    """
    if os.environ.get("HA_CCM_DOCKER_TEST") == "1":
        gh_base = (
            os.environ.get("HA_CCM_TEST_GITHUB_API_BASE")
            or os.environ["HA_CCM_GITHUB_API_BASE"]
        ).rstrip("/")
        codeload_base = (
            os.environ.get("HA_CCM_TEST_GITHUB_CODELOAD_BASE")
            or os.environ["HA_CCM_GITHUB_CODELOAD_BASE"]
        ).rstrip("/")
        ha_url = os.environ["HA_CCM_TEST_HA_URL"].rstrip("/")
        ha_api_key = os.environ.get("HA_CCM_TEST_HA_API_KEY", "test-ha-api-token")

        requests.post(f"{gh_base}/_test/reset", timeout=5).raise_for_status()
        requests.post(
            f"{ha_url}/_test/reset",
            json={"clear_components": True},
            timeout=5,
        ).raise_for_status()

        monkeypatch.setenv("HA_CCM_GITHUB_API_BASE", gh_base)
        monkeypatch.setenv("HA_CCM_GITHUB_CODELOAD_BASE", codeload_base)
        yield {
            "github_api_base": gh_base,
            "codeload_base": codeload_base,
            "ha_url": ha_url,
            "ha_api_key": ha_api_key,
        }
        return

    monkeypatch.setenv("MOCK_SERVER_QUIET", "1")
    ha_config_dir = tmp_path / "ha_config"
    (ha_config_dir / "custom_components").mkdir(parents=True, exist_ok=True)
    ha_api_key = "test-ha-api-token"

    # Check if loopback TCP sockets are permitted in the current environment
    loopback_allowed = False
    try:
        gh_server, gh_state = start_mock_github_server("127.0.0.1", 0)
        ha_server, ha_state = start_mock_ha_server(
            "127.0.0.1",
            0,
            expected_api_key=ha_api_key,
            config_dir=str(ha_config_dir),
        )
        gh_port = gh_server.server_address[1]
        with socket.create_connection(("127.0.0.1", gh_port), timeout=1):
            loopback_allowed = True
    except OSError:
        loopback_allowed = False

    if loopback_allowed:
        gh_thread = threading.Thread(target=gh_server.serve_forever, daemon=True)
        ha_thread = threading.Thread(target=ha_server.serve_forever, daemon=True)
        gh_thread.start()
        ha_thread.start()

        gh_port = gh_server.server_address[1]
        ha_port = ha_server.server_address[1]
        gh_base = f"http://127.0.0.1:{gh_port}"
        codeload_base = f"{gh_base}/codeload"
        ha_url = f"http://127.0.0.1:{ha_port}"

        monkeypatch.setenv("HA_CCM_GITHUB_API_BASE", gh_base)
        monkeypatch.setenv("HA_CCM_GITHUB_CODELOAD_BASE", codeload_base)

        try:
            yield {
                "github_api_base": gh_base,
                "codeload_base": codeload_base,
                "ha_url": ha_url,
                "ha_api_key": ha_api_key,
            }
        finally:
            gh_server.shutdown()
            gh_server.server_close()
            ha_server.shutdown()
            ha_server.server_close()
        return

    # Fallback when running inside a network-restricted sandbox
    gh_state = MockGitHubState()
    ha_state = MockHomeAssistantState(expected_api_key=ha_api_key, config_dir=str(ha_config_dir))
    gh_handler_cls = make_github_handler(gh_state)
    ha_handler_cls = make_ha_handler(ha_state)

    gh_base = "http://mock-github.local:8000"
    codeload_base = f"{gh_base}/codeload"
    ha_url = "http://mock-ha.local:8123"

    def _route_request(method: str, url: str, headers: Dict[str, str] | None = None, json_body: Any = None) -> _InMemoryHTTPResponse:
        if url.startswith(gh_base):
            return _dispatch_handler_in_memory(gh_handler_cls, method, url, headers=headers, json_body=json_body)
        if url.startswith(ha_url):
            return _dispatch_handler_in_memory(ha_handler_cls, method, url, headers=headers, json_body=json_body)
        raise RuntimeError(f"Unexpected URL in sandboxed test: {url}")

    def _session_get(self: Any, url: str, **kwargs: Any) -> _InMemoryHTTPResponse:
        hdrs = dict(getattr(self, "headers", {}) or {})
        hdrs.update(kwargs.get("headers") or {})
        return _route_request("GET", url, headers=hdrs)

    def _session_post(self: Any, url: str, **kwargs: Any) -> _InMemoryHTTPResponse:
        hdrs = dict(getattr(self, "headers", {}) or {})
        hdrs.update(kwargs.get("headers") or {})
        return _route_request("POST", url, headers=hdrs, json_body=kwargs.get("json"))

    monkeypatch.setattr(requests.Session, "get", _session_get)
    monkeypatch.setattr(requests.Session, "post", _session_post)
    monkeypatch.setattr(requests, "get", lambda url, **kw: _route_request("GET", url, headers=kw.get("headers")))
    monkeypatch.setattr(requests, "post", lambda url, **kw: _route_request("POST", url, headers=kw.get("headers"), json_body=kw.get("json")))

    monkeypatch.setenv("HA_CCM_GITHUB_API_BASE", gh_base)
    monkeypatch.setenv("HA_CCM_GITHUB_CODELOAD_BASE", codeload_base)

    yield {
        "github_api_base": gh_base,
        "codeload_base": codeload_base,
        "ha_url": ha_url,
        "ha_api_key": ha_api_key,
    }


def test_e2e_local_cli_workflow_with_mock_services(
    tmp_path: Path, integration_services: Dict[str, str]
) -> None:
    """End-to-end CLI test of sync, status, update, backup, revert, and restart against HTTP services."""
    gh_base = integration_services["github_api_base"]
    ha_url = integration_services["ha_url"]
    ha_api_key = integration_services["ha_api_key"]

    cc_dir = tmp_path / "custom_components"
    backup_dir = tmp_path / "backups"
    cfg_file = tmp_path / "components.yaml"
    cfg_file.write_text(
        f"""
homeassistant:
  mode: local
  custom_components_dir: "{cc_dir}"
  url: "{ha_url}"
  api_key: "{ha_api_key}"

prune: true
backup_enabled: true
backup_dir: "{backup_dir}"

components:
  - name: Philips Hue Play HDMI Sync Box
    repo: mvdwetering/huesyncbox
    branch: dev
  - name: HACS
    repo: hacs/integration
    version: latest
""",
        encoding="utf-8",
    )

    runner = CliRunner()

    # 1. Initial sync installs both components and triggers Home Assistant restart
    res1 = runner.invoke(cli, ["sync", "-c", str(cfg_file)])
    assert res1.exit_code == 0, res1.output
    assert "2 installed, 0 updated, 0 up-to-date, 0 pruned" in res1.output
    assert "Successfully triggered Home Assistant restart" in res1.output
    assert (cc_dir / "huesyncbox" / "manifest.json").is_file()
    assert (cc_dir / "hacs" / "manifest.json").is_file()

    ha_state = requests.get(f"{ha_url}/_test/state", timeout=5).json()
    assert ha_state["restart_count"] == 1

    # 2. Push a new commit to mvdwetering/huesyncbox@dev on the mock GitHub server
    requests.post(
        f"{gh_base}/_test/update_branch",
        json={
            "repo": "mvdwetering/huesyncbox",
            "branch": "dev",
            "sha": "9999999888888877777776666666555555544444",
            "domains": {
                "huesyncbox": {
                    "manifest.json": {
                        "domain": "huesyncbox",
                        "name": "Philips Hue Play HDMI Sync Box",
                        "version": "1.31.0",
                    },
                    "__init__.py": '"""Hue Sync Box integration."""\nVERSION = "1.31.0"\n',
                }
            },
        },
        timeout=5,
    ).raise_for_status()

    # 3. Check status -> huesyncbox has update_available, hacs is up_to_date
    res_status = runner.invoke(cli, ["status", "-c", str(cfg_file)])
    assert res_status.exit_code == 0, res_status.output
    assert "status=update_available" in res_status.output
    assert "status=up_to_date" in res_status.output

    # 4. Sync again -> backs up huesyncbox 1.30.0 and installs 1.31.0
    res2 = runner.invoke(cli, ["sync", "-c", str(cfg_file)])
    assert res2.exit_code == 0, res2.output
    assert "0 installed, 1 updated, 1 up-to-date, 0 pruned" in res2.output
    manifest_after_update = json.loads((cc_dir / "huesyncbox" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_after_update["version"] == "1.31.0"

    ha_state = requests.get(f"{ha_url}/_test/state", timeout=5).json()
    assert ha_state["restart_count"] == 2

    # 5. List backups and revert huesyncbox back to 1.30.0
    res_backups = runner.invoke(cli, ["backups", "huesyncbox", "-c", str(cfg_file)])
    assert res_backups.exit_code == 0, res_backups.output
    assert "huesyncbox | backup_id=" in res_backups.output

    res_revert = runner.invoke(cli, ["revert", "huesyncbox", "-c", str(cfg_file)])
    assert res_revert.exit_code == 0, res_revert.output
    manifest_after_revert = json.loads((cc_dir / "huesyncbox" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_after_revert["version"] == "1.30.0"

    ha_state = requests.get(f"{ha_url}/_test/state", timeout=5).json()
    assert ha_state["restart_count"] == 3


@pytest.mark.skipif(
    os.environ.get("HA_CCM_DOCKER_TEST") != "1",
    reason="Docker SSH container tests run when HA_CCM_DOCKER_TEST=1",
)
def test_docker_ssh_password_sync_update_revert_and_prune(
    tmp_path: Path, integration_services: Dict[str, str]
) -> None:
    """Full end-to-end test against the real OpenSSH/SFTP server in the ha-instance container."""
    gh_base = integration_services["github_api_base"]
    ha_url = integration_services["ha_url"]
    ha_api_key = integration_services["ha_api_key"]
    ssh_host = os.environ["HA_CCM_TEST_SSH_HOST"]
    ssh_port = int(os.environ.get("HA_CCM_TEST_SSH_PORT", "22"))
    ssh_user = os.environ.get("HA_CCM_TEST_SSH_USER", "root")
    ssh_password = os.environ.get("HA_CCM_TEST_SSH_PASSWORD", "ha_test_pass")

    backup_dir = tmp_path / "ssh_backups"
    cfg_file = tmp_path / "components.ssh.yaml"
    cfg_file.write_text(
        f"""
homeassistant:
  mode: ssh
  custom_components_dir: /config/custom_components
  url: "{ha_url}"
  api_key: "{ha_api_key}"
  ssh:
    host: "{ssh_host}"
    port: {ssh_port}
    username: "{ssh_user}"
    password: "{ssh_password}"
    strict_host_key_checking: false

prune: true
backup_enabled: true
backup_dir: "{backup_dir}"

components:
  - name: Philips Hue Play HDMI Sync Box
    repo: mvdwetering/huesyncbox
    branch: dev
  - name: HACS
    repo: hacs/integration
    version: v2.0.1
""",
        encoding="utf-8",
    )

    runner = CliRunner()

    # 1. Initial sync over SSH
    res1 = runner.invoke(cli, ["sync", "-c", str(cfg_file)])
    assert res1.exit_code == 0, res1.output
    assert "2 installed, 0 updated, 0 up-to-date, 0 pruned" in res1.output

    ha_state = requests.get(f"{ha_url}/_test/state", timeout=5).json()
    assert ha_state["restart_count"] == 1
    assert "huesyncbox" in ha_state["custom_components"]
    assert "hacs" in ha_state["custom_components"]
    assert ha_state["custom_components"]["huesyncbox"]["manifest"]["version"] == "1.30.0"

    # 2. Push branch update on mock GitHub and sync over SSH
    requests.post(
        f"{gh_base}/_test/update_branch",
        json={
            "repo": "mvdwetering/huesyncbox",
            "branch": "dev",
            "sha": "7777777666666655555554444444333333322222",
            "domains": {
                "huesyncbox": {
                    "manifest.json": {
                        "domain": "huesyncbox",
                        "name": "Philips Hue Play HDMI Sync Box",
                        "version": "1.32.0",
                    },
                    "__init__.py": 'VERSION = "1.32.0"\n',
                }
            },
        },
        timeout=5,
    ).raise_for_status()

    res2 = runner.invoke(cli, ["sync", "-c", str(cfg_file)])
    assert res2.exit_code == 0, res2.output
    assert "0 installed, 1 updated, 1 up-to-date, 0 pruned" in res2.output

    ha_state = requests.get(f"{ha_url}/_test/state", timeout=5).json()
    assert ha_state["restart_count"] == 2
    assert ha_state["custom_components"]["huesyncbox"]["manifest"]["version"] == "1.32.0"

    # 3. Revert over SSH back to 1.30.0
    res_rev = runner.invoke(cli, ["revert", "huesyncbox", "-c", str(cfg_file)])
    assert res_rev.exit_code == 0, res_rev.output

    ha_state = requests.get(f"{ha_url}/_test/state", timeout=5).json()
    assert ha_state["restart_count"] == 3
    assert ha_state["custom_components"]["huesyncbox"]["manifest"]["version"] == "1.30.0"

    # 4. Remove HACS from config and sync with prune=True -> prunes hacs over SSH
    cfg_file.write_text(
        f"""
homeassistant:
  mode: ssh
  custom_components_dir: /config/custom_components
  url: "{ha_url}"
  api_key: "{ha_api_key}"
  ssh:
    host: "{ssh_host}"
    port: {ssh_port}
    username: "{ssh_user}"
    password: "{ssh_password}"
    strict_host_key_checking: false

prune: true
backup_enabled: true
backup_dir: "{backup_dir}"

components:
  - name: Philips Hue Play HDMI Sync Box
    repo: mvdwetering/huesyncbox
    branch: dev
""",
        encoding="utf-8",
    )

    res_prune = runner.invoke(cli, ["sync", "-c", str(cfg_file)])
    assert res_prune.exit_code == 0, res_prune.output
    assert "1 pruned" in res_prune.output

    ha_state = requests.get(f"{ha_url}/_test/state", timeout=5).json()
    assert "hacs" not in ha_state["custom_components"]
    assert "huesyncbox" in ha_state["custom_components"]


@pytest.mark.skipif(
    os.environ.get("HA_CCM_DOCKER_TEST") != "1",
    reason="Docker SSH container tests run when HA_CCM_DOCKER_TEST=1",
)
def test_docker_ssh_key_and_encrypted_key_authentication(
    tmp_path: Path, integration_services: Dict[str, str]
) -> None:
    """Verify SSH public-key authentication with both unencrypted and passphrase-encrypted keys."""
    ha_url = integration_services["ha_url"]
    ha_api_key = integration_services["ha_api_key"]
    ssh_host = os.environ["HA_CCM_TEST_SSH_HOST"]
    ssh_port = int(os.environ.get("HA_CCM_TEST_SSH_PORT", "22"))
    ssh_user = os.environ.get("HA_CCM_TEST_SSH_USER", "root")
    key_dir = Path(os.environ.get("HA_CCM_TEST_SSH_KEY_DIR", "/ssh-keys"))
    passphrase = os.environ.get("HA_CCM_TEST_SSH_PASSPHRASE", "test-key-passphrase")

    unencrypted_key = key_dir / "id_ed25519"
    encrypted_key = key_dir / "id_ed25519_encrypted"
    assert unencrypted_key.is_file()
    assert encrypted_key.is_file()

    cfg_file = tmp_path / "components.key.yaml"
    cfg_file.write_text(
        f"""
homeassistant:
  mode: ssh
  custom_components_dir: /config/custom_components
  url: "{ha_url}"
  api_key: "{ha_api_key}"
  ssh:
    host: "{ssh_host}"
    port: {ssh_port}
    username: "{ssh_user}"
    key_file: "{unencrypted_key}"
    strict_host_key_checking: false

components:
  - repo: mvdwetering/huesyncbox
    branch: dev
""",
        encoding="utf-8",
    )

    runner = CliRunner()

    # 1. Sync using unencrypted SSH private key
    res_unenc = runner.invoke(cli, ["sync", "-c", str(cfg_file)])
    assert res_unenc.exit_code == 0, res_unenc.output
    assert "1 installed" in res_unenc.output

    # 2. Sync (--force) using passphrase-encrypted SSH private key via CLI override
    res_enc = runner.invoke(
        cli,
        [
            "sync",
            "-c",
            str(cfg_file),
            "--ssh-key",
            str(encrypted_key),
            "--ssh-passphrase",
            passphrase,
            "--force",
        ],
    )
    assert res_enc.exit_code == 0, res_enc.output
    assert "1 updated" in res_enc.output
