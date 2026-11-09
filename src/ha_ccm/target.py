"""Local and SSH target backends for deploying custom_components to Home Assistant."""

from __future__ import annotations

import getpass
import json
import os
import posixpath
import shutil
import stat
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import paramiko

from ha_ccm.models import HomeAssistantConfig, LockState, SSHConfig, TargetMode


class TargetError(RuntimeError):
    """Raised when an operation on the local or SSH target fails."""


class TargetBackend(ABC):
    """Abstract interface for managing a Home Assistant custom_components directory."""

    def __init__(self, base_dir: str, lockfile_name: str = ".ha-ccm-lock.json") -> None:
        self.base_dir = base_dir
        self.lockfile_name = lockfile_name

    def __enter__(self) -> TargetBackend:
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def connect(self) -> None:
        """Open connection if needed."""

    def close(self) -> None:
        """Close connection if needed."""

    @abstractmethod
    def ensure_base_dir(self) -> None:
        """Ensure the target custom_components directory exists."""

    @abstractmethod
    def read_lock_state(self) -> LockState:
        """Read the lockfile from the target custom_components directory."""

    @abstractmethod
    def write_lock_state(self, state: LockState) -> None:
        """Write the lockfile to the target custom_components directory."""

    @abstractmethod
    def domain_exists(self, domain: str) -> bool:
        """Check whether custom_components/<domain>/manifest.json exists on the target."""

    @abstractmethod
    def download_domain(self, domain: str, dest_dir: Path) -> bool:
        """Download/copy an existing custom_components/<domain> from the target into local dest_dir.

        Returns True if the domain existed and was copied, False if it did not exist.
        """

    @abstractmethod
    def install_domain(self, domain: str, source_dir: Path) -> None:
        """Replace custom_components/<domain> on the target with the contents of source_dir."""

    @abstractmethod
    def remove_domain(self, domain: str) -> None:
        """Remove custom_components/<domain> from the target."""


class LocalTarget(TargetBackend):
    """Manages custom_components on the local filesystem."""

    def __init__(self, base_dir: str, lockfile_name: str = ".ha-ccm-lock.json") -> None:
        super().__init__(base_dir, lockfile_name)
        self.base_path = Path(base_dir).expanduser().resolve()

    @property
    def lockfile_path(self) -> Path:
        return self.base_path / self.lockfile_name

    def ensure_base_dir(self) -> None:
        try:
            self.base_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise TargetError(
                f"Failed to create local custom_components directory '{self.base_path}': {exc}"
            ) from exc

    def read_lock_state(self) -> LockState:
        if not self.lockfile_path.is_file():
            return LockState()
        try:
            raw = json.loads(self.lockfile_path.read_text(encoding="utf-8"))
            return LockState.from_dict(raw)
        except Exception as exc:
            raise TargetError(
                f"Failed to read lockfile '{self.lockfile_path}': {exc}"
            ) from exc

    def write_lock_state(self, state: LockState) -> None:
        self.ensure_base_dir()
        try:
            content = json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n"
            self.lockfile_path.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise TargetError(
                f"Failed to write lockfile '{self.lockfile_path}': {exc}"
            ) from exc

    def domain_exists(self, domain: str) -> bool:
        manifest = self.base_path / domain / "manifest.json"
        return manifest.is_file()

    def download_domain(self, domain: str, dest_dir: Path) -> bool:
        src_dir = self.base_path / domain
        if not src_dir.is_dir():
            return False
        try:
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            dest_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src_dir, dest_dir)
            return True
        except OSError as exc:
            raise TargetError(
                f"Failed to back up local component domain '{domain}' from '{src_dir}' to '{dest_dir}': {exc}"
            ) from exc

    def install_domain(self, domain: str, source_dir: Path) -> None:
        self.ensure_base_dir()
        dest_dir = self.base_path / domain
        try:
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            shutil.copytree(source_dir, dest_dir)
        except OSError as exc:
            raise TargetError(
                f"Failed to install component domain '{domain}' to '{dest_dir}': {exc}"
            ) from exc

    def remove_domain(self, domain: str) -> None:
        dest_dir = self.base_path / domain
        try:
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
        except OSError as exc:
            raise TargetError(
                f"Failed to remove component domain '{domain}' at '{dest_dir}': {exc}"
            ) from exc


class SSHTarget(TargetBackend):
    """Manages custom_components on a remote Home Assistant host over SSH/SFTP."""

    def __init__(
        self,
        ssh_config: SSHConfig,
        base_dir: str,
        lockfile_name: str = ".ha-ccm-lock.json",
        ssh_client: Optional[paramiko.SSHClient] = None,
        passphrase_prompter: Optional[Callable[[str], str]] = None,
    ) -> None:
        super().__init__(base_dir.rstrip("/") or "/", lockfile_name)
        self.ssh_config = ssh_config
        self._client: Optional[paramiko.SSHClient] = ssh_client
        self._sftp: Optional[paramiko.SFTPClient] = None
        self._owns_client = ssh_client is None
        self._passphrase_prompter = passphrase_prompter

    @property
    def lockfile_remote_path(self) -> str:
        return posixpath.join(self.base_dir, self.lockfile_name)

    def _build_connect_kwargs(self, passphrase: Optional[str] = None) -> Dict[str, Any]:
        connect_kwargs: Dict[str, Any] = {
            "hostname": self.ssh_config.host,
            "port": self.ssh_config.port,
            "username": self.ssh_config.username,
            "timeout": self.ssh_config.connect_timeout,
            "allow_agent": True,
            "look_for_keys": True,
        }
        if self.ssh_config.password:
            connect_kwargs["password"] = self.ssh_config.password
        if self.ssh_config.key_file:
            connect_kwargs["key_filename"] = self.ssh_config.key_file

        effective_passphrase = passphrase or self.ssh_config.passphrase or self.ssh_config.password
        if effective_passphrase:
            connect_kwargs["passphrase"] = effective_passphrase

        return connect_kwargs

    @staticmethod
    def _try_load_macos_keychain_into_agent(key_file: Optional[str]) -> bool:
        """On macOS, attempt to load SSH keys from the Apple Keychain into ssh-agent."""
        if sys.platform != "darwin":
            return False
        cmd = ["ssh-add", "--apple-load-keychain"]
        if key_file:
            cmd.append(key_file)
        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            return res.returncode == 0
        except Exception:
            return False

    def connect(self) -> None:
        if self._sftp is not None:
            return
        try:
            if self._client is None:
                client = paramiko.SSHClient()
                client.load_system_host_keys()
                if not self.ssh_config.strict_host_key_checking:
                    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                else:
                    client.set_missing_host_key_policy(paramiko.RejectPolicy())

                connect_kwargs = self._build_connect_kwargs()
                try:
                    client.connect(**connect_kwargs)
                except paramiko.PasswordRequiredException as enc_exc:
                    connected = False
                    # 1. On macOS, try loading key passphrase from Apple Keychain into ssh-agent
                    if self._try_load_macos_keychain_into_agent(self.ssh_config.key_file):
                        agent_kwargs = dict(connect_kwargs)
                        agent_kwargs.pop("key_filename", None)
                        agent_kwargs["look_for_keys"] = False
                        agent_kwargs["allow_agent"] = True
                        try:
                            client.connect(**agent_kwargs)
                            connected = True
                        except Exception:
                            connected = False

                    # 2. Prompt interactively for passphrase if a prompter or interactive TTY is available
                    if not connected:
                        key_label = self.ssh_config.key_file or "SSH private key"
                        prompt_text = f"Enter passphrase for '{key_label}': "
                        entered_passphrase: Optional[str] = None
                        if self._passphrase_prompter is not None:
                            entered_passphrase = self._passphrase_prompter(prompt_text)
                        elif sys.stdin is not None and sys.stdin.isatty():
                            entered_passphrase = getpass.getpass(prompt_text)

                        if entered_passphrase:
                            self.ssh_config.passphrase = entered_passphrase
                            retry_kwargs = self._build_connect_kwargs(passphrase=entered_passphrase)
                            client.connect(**retry_kwargs)
                            connected = True

                    if not connected:
                        raise TargetError(
                            f"Private key '{self.ssh_config.key_file or 'default'}' is encrypted. "
                            "Supply the key passphrase via 'ssh.passphrase' in YAML, "
                            "the HA_SSH_PASSPHRASE environment variable, the '--ssh-passphrase' CLI flag, "
                            "or add the key to your ssh-agent with 'ssh-add'."
                        ) from enc_exc

                self._client = client

            self._sftp = self._client.open_sftp()
        except TargetError:
            raise
        except Exception as exc:
            raise TargetError(
                f"Failed to connect via SSH to {self.ssh_config.username}@"
                f"{self.ssh_config.host}:{self.ssh_config.port}: {exc}"
            ) from exc

    def close(self) -> None:
        if self._sftp is not None:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None
        if self._client is not None and self._owns_client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    @property
    def sftp(self) -> paramiko.SFTPClient:
        if self._sftp is None:
            self.connect()
        assert self._sftp is not None
        return self._sftp

    def _mkdir_p(self, remote_dir: str) -> None:
        """Recursively create directories on the remote SFTP server."""
        if remote_dir in ("", "/"):
            return
        dirs_to_create = []
        curr = remote_dir
        while curr not in ("", "/"):
            try:
                st = self.sftp.stat(curr)
                if stat.S_ISDIR(st.st_mode or 0):
                    break
                raise TargetError(f"Remote path '{curr}' exists and is not a directory.")
            except FileNotFoundError:
                dirs_to_create.append(curr)
                curr = posixpath.dirname(curr)
            except IOError:
                dirs_to_create.append(curr)
                curr = posixpath.dirname(curr)

        for d in reversed(dirs_to_create):
            try:
                self.sftp.mkdir(d)
            except IOError as exc:
                raise TargetError(f"Failed to create remote directory '{d}': {exc}") from exc

    def _rmtree_remote(self, remote_path: str) -> None:
        """Recursively delete a directory or file over SFTP."""
        try:
            st = self.sftp.stat(remote_path)
        except (FileNotFoundError, IOError):
            return

        if not stat.S_ISDIR(st.st_mode or 0):
            self.sftp.remove(remote_path)
            return

        for entry in self.sftp.listdir_attr(remote_path):
            child_path = posixpath.join(remote_path, entry.filename)
            if stat.S_ISDIR(entry.st_mode or 0):
                self._rmtree_remote(child_path)
            else:
                self.sftp.remove(child_path)
        self.sftp.rmdir(remote_path)

    def ensure_base_dir(self) -> None:
        self._mkdir_p(self.base_dir)

    def read_lock_state(self) -> LockState:
        try:
            with self.sftp.open(self.lockfile_remote_path, "r") as f:
                raw_bytes = f.read()
            text = raw_bytes.decode("utf-8") if isinstance(raw_bytes, bytes) else str(raw_bytes)
            return LockState.from_dict(json.loads(text))
        except (FileNotFoundError, IOError):
            return LockState()
        except Exception as exc:
            raise TargetError(
                f"Failed to parse remote lockfile '{self.lockfile_remote_path}': {exc}"
            ) from exc

    def write_lock_state(self, state: LockState) -> None:
        self.ensure_base_dir()
        content = json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n"
        try:
            with self.sftp.open(self.lockfile_remote_path, "w") as f:
                f.write(content)
        except Exception as exc:
            raise TargetError(
                f"Failed to write remote lockfile '{self.lockfile_remote_path}': {exc}"
            ) from exc

    def domain_exists(self, domain: str) -> bool:
        manifest_remote = posixpath.join(self.base_dir, domain, "manifest.json")
        try:
            st = self.sftp.stat(manifest_remote)
            return not stat.S_ISDIR(st.st_mode or 0)
        except (FileNotFoundError, IOError):
            return False

    def download_domain(self, domain: str, dest_dir: Path) -> bool:
        remote_domain_dir = posixpath.join(self.base_dir, domain)
        try:
            st = self.sftp.stat(remote_domain_dir)
            if not stat.S_ISDIR(st.st_mode or 0):
                return False
        except (FileNotFoundError, IOError):
            return False

        try:
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
            self._download_dir_recursive(remote_domain_dir, dest_dir)
            return True
        except Exception as exc:
            raise TargetError(
                f"Failed to download backup of remote component domain '{domain}' "
                f"from '{remote_domain_dir}' to '{dest_dir}': {exc}"
            ) from exc

    def _download_dir_recursive(self, remote_dir: str, local_dir: Path) -> None:
        local_dir.mkdir(parents=True, exist_ok=True)
        for entry in self.sftp.listdir_attr(remote_dir):
            remote_child = posixpath.join(remote_dir, entry.filename)
            local_child = local_dir / entry.filename
            if stat.S_ISDIR(entry.st_mode or 0):
                self._download_dir_recursive(remote_child, local_child)
            else:
                self.sftp.get(remote_child, str(local_child))

    def install_domain(self, domain: str, source_dir: Path) -> None:
        self.ensure_base_dir()
        dest_remote = posixpath.join(self.base_dir, domain)
        try:
            self._rmtree_remote(dest_remote)
            self._upload_dir_recursive(source_dir, dest_remote)
        except Exception as exc:
            raise TargetError(
                f"Failed to upload component domain '{domain}' to remote '{dest_remote}': {exc}"
            ) from exc

    def _upload_dir_recursive(self, local_dir: Path, remote_dir: str) -> None:
        self._mkdir_p(remote_dir)
        for root, dirs, files in os.walk(local_dir):
            rel_path = Path(root).relative_to(local_dir)
            if str(rel_path) == ".":
                curr_remote = remote_dir
            else:
                curr_remote = posixpath.join(remote_dir, *rel_path.parts)
                self._mkdir_p(curr_remote)

            for d in dirs:
                self._mkdir_p(posixpath.join(curr_remote, d))

            for file_name in files:
                local_file = Path(root) / file_name
                remote_file = posixpath.join(curr_remote, file_name)
                self.sftp.put(str(local_file), remote_file)

    def remove_domain(self, domain: str) -> None:
        dest_remote = posixpath.join(self.base_dir, domain)
        try:
            self._rmtree_remote(dest_remote)
        except Exception as exc:
            raise TargetError(
                f"Failed to remove remote component domain '{domain}' at '{dest_remote}': {exc}"
            ) from exc


def create_target_backend(ha_config: HomeAssistantConfig, lockfile_name: str = ".ha-ccm-lock.json") -> TargetBackend:
    """Factory that creates either a LocalTarget or SSHTarget from HomeAssistantConfig."""
    if ha_config.mode == TargetMode.SSH:
        if ha_config.ssh is None:
            raise TargetError("SSH mode requires SSH configuration.")
        return SSHTarget(
            ssh_config=ha_config.ssh,
            base_dir=ha_config.custom_components_dir,
            lockfile_name=lockfile_name,
        )
    return LocalTarget(
        base_dir=ha_config.custom_components_dir,
        lockfile_name=lockfile_name,
    )
