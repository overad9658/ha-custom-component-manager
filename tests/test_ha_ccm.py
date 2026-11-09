"""Unit and integration tests for Home Assistant Custom Component Manager (ha-ccm)."""

from __future__ import annotations

import io
import json
import os
import stat
import tarfile
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from ha_ccm.cli import cli
from ha_ccm.config import ConfigError, load_config, parse_github_repo
from ha_ccm.github import GitHubClient, GitHubError
from ha_ccm.homeassistant import (
    MANUAL_RESTART_MESSAGE,
    HomeAssistantAPIError,
    restart_home_assistant,
)
from ha_ccm.manager import ComponentManager
from ha_ccm.models import (
    AppConfig,
    ComponentConfig,
    HomeAssistantConfig,
    RefType,
    SSHConfig,
    TargetMode,
)
from ha_ccm.target import LocalTarget, SSHTarget


def make_test_tarball(domains: Dict[str, Dict[str, Any]], repo_prefix: str = "huesyncbox-dev") -> bytes:
    """Create an in-memory tar.gz archive matching a standard HACS repository layout."""
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w:gz") as tf:
        for domain, files in domains.items():
            for rel_path, content in files.items():
                full_name = f"{repo_prefix}/custom_components/{domain}/{rel_path}"
                data = (
                    json.dumps(content).encode("utf-8")
                    if isinstance(content, dict)
                    else str(content).encode("utf-8")
                )
                info = tarfile.TarInfo(name=full_name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return bio.getvalue()


class FakeResponse:
    def __init__(self, status_code: int = 200, json_data: Any = None, content: bytes = b"", text: str = "") -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.content = content
        self.text = text or (json.dumps(json_data) if json_data is not None else "")

    def json(self) -> Any:
        return self._json_data


# ---------------------------------------------------------------------------
# Config & GitHub URL parsing tests
# ---------------------------------------------------------------------------


def test_parse_github_repo_variants() -> None:
    assert parse_github_repo("mvdwetering/huesyncbox") == ("mvdwetering/huesyncbox", None)
    assert parse_github_repo("https://github.com/mvdwetering/huesyncbox") == ("mvdwetering/huesyncbox", None)
    assert parse_github_repo("https://github.com/mvdwetering/huesyncbox.git") == ("mvdwetering/huesyncbox", None)
    assert parse_github_repo("https://github.com/mvdwetering/huesyncbox/tree/dev/custom_components") == (
        "mvdwetering/huesyncbox",
        "dev",
    )


def test_load_config_with_env_and_tree_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_HA_KEY", "secret-token-123")
    cfg_file = tmp_path / "components.yaml"
    cfg_file.write_text(
        """
homeassistant:
  mode: local
  custom_components_dir: ./my_cc
  url: http://192.168.1.50:8123
  api_key: "${TEST_HA_KEY}"
prune: true
components:
  - repo: https://github.com/mvdwetering/huesyncbox/tree/dev/custom_components
  - name: Example Release Component
    repo: owner/comp2
    version: v2.1.0
""",
        encoding="utf-8",
    )
    app_cfg = load_config(cfg_file)
    assert app_cfg.homeassistant.mode == TargetMode.LOCAL
    assert app_cfg.homeassistant.api_key == "secret-token-123"
    assert app_cfg.prune is True
    assert len(app_cfg.components) == 2

    c1 = app_cfg.components[0]
    assert c1.repo == "mvdwetering/huesyncbox"
    assert c1.ref_type == RefType.BRANCH
    assert c1.ref == "dev"

    c2 = app_cfg.components[1]
    assert c2.repo == "owner/comp2"
    assert c2.ref_type == RefType.RELEASE
    assert c2.ref == "v2.1.0"


def test_config_rejects_both_branch_and_version(tmp_path: Path) -> None:
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text(
        """
components:
  - repo: mvdwetering/huesyncbox
    branch: dev
    version: v1.0.0
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="both 'branch'"):
        load_config(cfg_file)


# ---------------------------------------------------------------------------
# GitHub client tests
# ---------------------------------------------------------------------------


def test_github_resolve_branch_and_extract(tmp_path: Path) -> None:
    tar_bytes = make_test_tarball(
        {
            "huesyncbox": {
                "manifest.json": {"domain": "huesyncbox", "name": "Hue Sync Box", "version": "1.30.0"},
                "__init__.py": "# hue sync box init\n",
                "sensor.py": "# sensor\n",
            }
        }
    )

    session = MagicMock()
    session.headers = {}

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        if url.endswith("/repos/mvdwetering/huesyncbox/commits/dev"):
            return FakeResponse(200, json_data={"sha": "abcdef1234567890abcdef1234567890abcdef12"})
        if "codeload.github.com/mvdwetering/huesyncbox/tar.gz/abcdef1" in url:
            return FakeResponse(200, content=tar_bytes)
        return FakeResponse(404)

    session.get.side_effect = fake_get

    client = GitHubClient(session=session)
    comp = ComponentConfig(repo="mvdwetering/huesyncbox", ref_type=RefType.BRANCH, ref="dev")
    resolved = client.resolve_ref(comp)

    assert resolved.resolved_ref == "abcdef1234567890abcdef1234567890abcdef12"
    assert resolved.version_label == "dev@abcdef1"

    extracted = client.download_and_extract_components(comp, resolved, tmp_path / "staging")
    assert "huesyncbox" in extracted
    assert (extracted["huesyncbox"] / "manifest.json").is_file()
    assert (extracted["huesyncbox"] / "__init__.py").read_text() == "# hue sync box init\n"


def test_github_resolve_release_version_with_v_fallback() -> None:
    session = MagicMock()
    session.headers = {}

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        # User specified "1.5.0", repo tag is actually "v1.5.0"
        if url.endswith("/releases/tags/1.5.0") or url.endswith("/git/ref/tags/1.5.0"):
            return FakeResponse(404)
        if url.endswith("/releases/tags/v1.5.0"):
            return FakeResponse(200, json_data={"tag_name": "v1.5.0"})
        return FakeResponse(404)

    session.get.side_effect = fake_get
    client = GitHubClient(session=session)
    comp = ComponentConfig(repo="owner/mycomp", ref_type=RefType.RELEASE, ref="1.5.0")
    resolved = client.resolve_ref(comp)
    assert resolved.resolved_ref == "v1.5.0"
    assert resolved.version_label == "v1.5.0"


# ---------------------------------------------------------------------------
# Local Sync, Lockfile, Update, Prune & Restart tests
# ---------------------------------------------------------------------------


def test_local_sync_install_skip_update_and_prune(tmp_path: Path) -> None:
    cc_dir = tmp_path / "custom_components"
    tar_v1 = make_test_tarball(
        {
            "huesyncbox": {
                "manifest.json": {"domain": "huesyncbox", "version": "1.0.0"},
                "__init__.py": "VERSION = '1.0.0'\n",
            }
        }
    )
    tar_v2 = make_test_tarball(
        {
            "huesyncbox": {
                "manifest.json": {"domain": "huesyncbox", "version": "2.0.0"},
                "__init__.py": "VERSION = '2.0.0'\n",
            }
        }
    )

    current_sha = "1111111222222233333334444444555555566666"
    current_tar = tar_v1

    session = MagicMock()
    session.headers = {}

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        if "commits/dev" in url:
            return FakeResponse(200, json_data={"sha": current_sha})
        if "codeload.github.com" in url:
            return FakeResponse(200, content=current_tar)
        return FakeResponse(404)

    session.get.side_effect = fake_get
    gh_client = GitHubClient(session=session)

    # 1. Initial sync without API key -> installs component and outputs manual restart notice
    backup_dir = tmp_path / "backups"
    cfg = AppConfig(
        homeassistant=HomeAssistantConfig(
            custom_components_dir=str(cc_dir),
            mode=TargetMode.LOCAL,
            api_key=None,
        ),
        components=[
            ComponentConfig(repo="mvdwetering/huesyncbox", ref_type=RefType.BRANCH, ref="dev")
        ],
        prune=True,
        backup_enabled=True,
        backup_dir=str(backup_dir),
    )

    manager = ComponentManager(cfg, github_client=gh_client)
    report1 = manager.sync()

    assert len(report1.actions) == 1
    assert report1.actions[0].action == "installed"
    assert (cc_dir / "huesyncbox" / "__init__.py").read_text() == "VERSION = '1.0.0'\n"
    assert report1.restart is not None
    assert report1.restart.manual_restart_required is True
    assert report1.restart.message == MANUAL_RESTART_MESSAGE

    # 2. Second sync with same SHA -> skips download ("up_to_date")
    report2 = manager.sync()
    assert report2.actions[0].action == "up_to_date"
    assert report2.has_changes is False
    assert report2.restart is not None
    assert report2.restart.manual_restart_required is True

    # 3. Third sync with updated commit SHA -> backs up v1.0.0 and updates component to v2.0.0
    current_sha = "9999999888888877777776666666555555544444"
    current_tar = tar_v2
    report3 = manager.sync()
    assert report3.actions[0].action == "updated"
    assert len(report3.actions[0].backup_paths) == 1
    assert Path(report3.actions[0].backup_paths[0]).is_dir()
    assert (cc_dir / "huesyncbox" / "__init__.py").read_text() == "VERSION = '2.0.0'\n"

    # Verify backup listing and revert back to v1.0.0
    backups = manager.list_backups("huesyncbox")
    assert len(backups) == 1
    revert_report = manager.revert("huesyncbox")
    assert revert_report.actions[0].action == "reverted"
    assert (cc_dir / "huesyncbox" / "__init__.py").read_text() == "VERSION = '1.0.0'\n"

    # 4. Fourth sync with empty component list and prune=True -> backs up and prunes huesyncbox
    cfg.components = []
    report4 = manager.sync()
    assert report4.pruned_domains == ["huesyncbox"]
    assert not (cc_dir / "huesyncbox").exists()
    assert len(manager.list_backups("huesyncbox")) == 2


# ---------------------------------------------------------------------------
# Home Assistant Restart API tests
# ---------------------------------------------------------------------------


def test_restart_home_assistant_with_and_without_api_key() -> None:
    # No API key -> manual restart required
    ha_no_key = HomeAssistantConfig(url="http://ha.local:8123", api_key=None)
    outcome_no_key = restart_home_assistant(ha_no_key)
    assert outcome_no_key.attempted is False
    assert outcome_no_key.restarted is False
    assert outcome_no_key.manual_restart_required is True
    assert "manually restart" in outcome_no_key.message.lower()

    # With API key -> calls POST /api/services/homeassistant/restart
    session = MagicMock()
    session.post.return_value = FakeResponse(200, json_data=[])
    ha_with_key = HomeAssistantConfig(url="http://ha.local:8123", api_key="my-long-lived-token")
    outcome_with_key = restart_home_assistant(ha_with_key, session=session)
    assert outcome_with_key.attempted is True
    assert outcome_with_key.restarted is True
    assert outcome_with_key.manual_restart_required is False
    session.post.assert_called_once_with(
        "http://ha.local:8123/api/services/homeassistant/restart",
        headers={
            "Authorization": "Bearer my-long-lived-token",
            "Content-Type": "application/json",
        },
        json={},
        timeout=20,
        verify=True,
    )


# ---------------------------------------------------------------------------
# SSH Target tests (using in-memory fake SFTP backed by local tmp_path)
# ---------------------------------------------------------------------------


class FakeSFTPFile:
    def __init__(self, path: Path, mode: str) -> None:
        self.path = path
        self.mode = mode

    def __enter__(self) -> FakeSFTPFile:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        pass

    def read(self) -> bytes:
        return self.path.read_bytes()

    def write(self, data: str | bytes) -> None:
        if isinstance(data, str):
            self.path.write_text(data, encoding="utf-8")
        else:
            self.path.write_bytes(data)


class FakeSFTPAttr:
    def __init__(self, filename: str, st_mode: int) -> None:
        self.filename = filename
        self.st_mode = st_mode


class FakeSFTPClient:
    """Maps remote POSIX paths under /config/custom_components into a local tmp_path."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _map(self, remote_path: str) -> Path:
        rel = remote_path.lstrip("/")
        return self.root / rel

    def stat(self, remote_path: str) -> FakeSFTPAttr:
        p = self._map(remote_path)
        if not p.exists():
            raise FileNotFoundError(remote_path)
        mode = stat.S_IFDIR if p.is_dir() else stat.S_IFREG
        return FakeSFTPAttr(p.name, mode)

    def mkdir(self, remote_path: str) -> None:
        self._map(remote_path).mkdir(parents=False, exist_ok=True)

    def rmdir(self, remote_path: str) -> None:
        self._map(remote_path).rmdir()

    def remove(self, remote_path: str) -> None:
        self._map(remote_path).unlink()

    def listdir_attr(self, remote_path: str) -> list[FakeSFTPAttr]:
        p = self._map(remote_path)
        out = []
        for child in p.iterdir():
            mode = stat.S_IFDIR if child.is_dir() else stat.S_IFREG
            out.append(FakeSFTPAttr(child.name, mode))
        return out

    def open(self, remote_path: str, mode: str = "r") -> FakeSFTPFile:
        p = self._map(remote_path)
        if "r" in mode and not p.exists():
            raise FileNotFoundError(remote_path)
        return FakeSFTPFile(p, mode)

    def put(self, local_path: str, remote_path: str) -> None:
        dest = self._map(remote_path)
        dest.write_bytes(Path(local_path).read_bytes())

    def get(self, remote_path: str, local_path: str) -> None:
        src = self._map(remote_path)
        Path(local_path).write_bytes(src.read_bytes())

    def close(self) -> None:
        pass


def test_ssh_target_sync(tmp_path: Path) -> None:
    remote_root = tmp_path / "remote_fs"
    remote_root.mkdir()
    backup_dir = tmp_path / "ssh_backups"

    # Pre-populate an existing component on the remote SSH server to verify pre-install backup download
    existing_remote_comp = remote_root / "config/custom_components/huesyncbox"
    existing_remote_comp.mkdir(parents=True)
    (existing_remote_comp / "manifest.json").write_text(
        json.dumps({"domain": "huesyncbox", "version": "1.20.0"}), encoding="utf-8"
    )
    (existing_remote_comp / "__init__.py").write_text("# pre-existing v1.20.0\n", encoding="utf-8")

    fake_sftp = FakeSFTPClient(remote_root)
    fake_ssh_client = MagicMock()
    fake_ssh_client.open_sftp.return_value = fake_sftp

    ssh_cfg = SSHConfig(host="192.168.1.100", username="root", port=22)
    ssh_target = SSHTarget(
        ssh_config=ssh_cfg,
        base_dir="/config/custom_components",
        ssh_client=fake_ssh_client,
    )

    tar_bytes = make_test_tarball(
        {
            "huesyncbox": {
                "manifest.json": {"domain": "huesyncbox", "version": "1.30.0"},
                "__init__.py": "# remote ssh test\n",
                "translations/en.json": {"title": "Hue Sync Box"},
            }
        }
    )

    session = MagicMock()
    session.headers = {}
    session.get.side_effect = lambda url, **kw: (
        FakeResponse(200, json_data={"sha": "abcdef1234567890"})
        if "commits/dev" in url
        else FakeResponse(200, content=tar_bytes)
    )

    app_cfg = AppConfig(
        homeassistant=HomeAssistantConfig(
            custom_components_dir="/config/custom_components",
            mode=TargetMode.SSH,
            ssh=ssh_cfg,
            api_key=None,
        ),
        components=[
            ComponentConfig(repo="mvdwetering/huesyncbox", ref_type=RefType.BRANCH, ref="dev")
        ],
        backup_enabled=True,
        backup_dir=str(backup_dir),
    )

    manager = ComponentManager(
        app_cfg,
        github_client=GitHubClient(session=session),
        target_backend=ssh_target,
    )
    report = manager.sync()

    assert report.actions[0].action == "updated"
    assert len(report.actions[0].backup_paths) == 1
    installed_init = remote_root / "config/custom_components/huesyncbox/__init__.py"
    installed_trans = remote_root / "config/custom_components/huesyncbox/translations/en.json"
    lock_file = remote_root / "config/custom_components/.ha-ccm-lock.json"

    assert installed_init.is_file()
    assert installed_init.read_text() == "# remote ssh test\n"
    assert installed_trans.is_file()
    assert lock_file.is_file()

    # Revert over SSH restores the pre-existing v1.20.0 backup
    manager.revert("huesyncbox")
    assert installed_init.read_text() == "# pre-existing v1.20.0\n"
    assert not installed_trans.exists()


def test_ssh_encrypted_key_prompts_for_passphrase(monkeypatch: pytest.MonkeyPatch) -> None:
    import paramiko

    fake_sftp = MagicMock()
    mock_client_instance = MagicMock()
    mock_client_instance.open_sftp.return_value = fake_sftp

    call_log = []

    def fake_connect(**kwargs: Any) -> None:
        call_log.append(kwargs)
        if not kwargs.get("passphrase"):
            raise paramiko.PasswordRequiredException("Private key file is encrypted")

    mock_client_instance.connect.side_effect = fake_connect
    monkeypatch.setattr(paramiko, "SSHClient", lambda: mock_client_instance)
    monkeypatch.setattr(SSHTarget, "_try_load_macos_keychain_into_agent", staticmethod(lambda kf: False))

    ssh_cfg = SSHConfig(
        host="homeassistant.local",
        username="root",
        port=22,
        key_file="/Users/test/.ssh/id_ed25519",
    )
    target = SSHTarget(
        ssh_config=ssh_cfg,
        base_dir="/config/custom_components",
        passphrase_prompter=lambda prompt: "my-key-passphrase",
    )
    target.connect()

    assert len(call_log) == 2
    assert "passphrase" not in call_log[0]
    assert call_log[1]["passphrase"] == "my-key-passphrase"
    assert ssh_cfg.passphrase == "my-key-passphrase"
    target.close()


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def test_cli_validate_and_restart_without_api_key(tmp_path: Path) -> None:
    cfg_file = tmp_path / "components.yaml"
    cfg_file.write_text(
        """
homeassistant:
  mode: local
  custom_components_dir: ./custom_components
components:
  - repo: mvdwetering/huesyncbox
    branch: dev
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    res_val = runner.invoke(cli, ["validate", "-c", str(cfg_file)])
    assert res_val.exit_code == 0
    assert "is valid" in res_val.output

    res_restart = runner.invoke(cli, ["restart", "-c", str(cfg_file)])
    assert res_restart.exit_code == 0
    assert "No Home Assistant API key was supplied" in res_restart.output
    assert "manually restart" in res_restart.output
