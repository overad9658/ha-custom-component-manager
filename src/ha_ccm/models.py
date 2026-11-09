"""Data models for Home Assistant Custom Component Manager."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class RefType(str, Enum):
    """GitHub reference type for downloading a custom component."""

    BRANCH = "branch"
    RELEASE = "release"


class TargetMode(str, Enum):
    """Deployment target mode."""

    LOCAL = "local"
    SSH = "ssh"


@dataclass
class ComponentConfig:
    """Configuration for a single Home Assistant custom component."""

    repo: str  # Normalized "owner/repo"
    ref_type: RefType
    ref: str  # Branch name (e.g. "main", "dev") or release version/tag (e.g. "v1.2.0", "latest")
    name: Optional[str] = None
    domain: Optional[str] = None  # Optional filter if repo has multiple domains under custom_components/

    @property
    def display_name(self) -> str:
        return self.name or self.repo


@dataclass
class SSHConfig:
    """SSH connection settings for remote deployment."""

    host: str
    username: str = "root"
    port: int = 22
    password: Optional[str] = None
    key_file: Optional[str] = None
    passphrase: Optional[str] = None
    strict_host_key_checking: bool = False
    connect_timeout: int = 15


@dataclass
class HomeAssistantConfig:
    """Home Assistant instance configuration."""

    custom_components_dir: str = "/config/custom_components"
    mode: TargetMode = TargetMode.LOCAL
    url: Optional[str] = None
    api_key: Optional[str] = None
    verify_ssl: bool = True
    ssh: Optional[SSHConfig] = None


@dataclass
class AppConfig:
    """Top-level application configuration loaded from YAML."""

    homeassistant: HomeAssistantConfig
    components: List[ComponentConfig] = field(default_factory=list)
    github_token: Optional[str] = None
    prune: bool = False
    backup_enabled: bool = True
    backup_dir: str = "./backups"
    lockfile_name: str = ".ha-ccm-lock.json"


@dataclass
class ResolvedRef:
    """Resolved GitHub reference and download metadata."""

    repo: str
    ref_type: RefType
    requested_ref: str
    resolved_ref: str  # Commit SHA for branches, or exact tag_name for releases
    archive_url: str
    version_label: str  # Human-readable version or branch@short_sha


@dataclass
class LockEntry:
    """Record of an installed component stored in the lockfile."""

    repo: str
    ref_type: str
    requested_ref: str
    resolved_ref: str
    version_label: str
    domains: List[str]
    installed_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo": self.repo,
            "ref_type": self.ref_type,
            "requested_ref": self.requested_ref,
            "resolved_ref": self.resolved_ref,
            "version_label": self.version_label,
            "domains": list(self.domains),
            "installed_at": self.installed_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> LockEntry:
        return cls(
            repo=str(data["repo"]),
            ref_type=str(data.get("ref_type", RefType.BRANCH.value)),
            requested_ref=str(data.get("requested_ref", "")),
            resolved_ref=str(data.get("resolved_ref", "")),
            version_label=str(data.get("version_label", data.get("resolved_ref", ""))),
            domains=[str(d) for d in data.get("domains", [])],
            installed_at=str(data.get("installed_at", "")),
        )


@dataclass
class LockState:
    """Full lockfile state persisted in custom_components_dir."""

    schema_version: int = 1
    installed: Dict[str, LockEntry] = field(default_factory=dict)  # keyed by normalized repo

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "installed": {repo: entry.to_dict() for repo, entry in sorted(self.installed.items())},
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> LockState:
        if not data or not isinstance(data, dict):
            return cls()
        installed_raw = data.get("installed", {})
        installed: Dict[str, LockEntry] = {}
        if isinstance(installed_raw, dict):
            for repo, entry_data in installed_raw.items():
                if isinstance(entry_data, dict):
                    installed[repo] = LockEntry.from_dict(entry_data)
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            installed=installed,
        )


@dataclass
class ComponentSyncAction:
    """Result of syncing a single component."""

    repo: str
    action: str  # "installed", "updated", "up_to_date", "pruned", "reverted", "skipped"
    domains: List[str]
    previous_version: Optional[str] = None
    current_version: Optional[str] = None
    backup_paths: List[str] = field(default_factory=list)
    message: Optional[str] = None


@dataclass
class RestartOutcome:
    """Result of attempting or skipping a Home Assistant restart."""

    attempted: bool
    restarted: bool
    manual_restart_required: bool
    message: str


@dataclass
class SyncReport:
    """Summary of a full sync execution."""

    actions: List[ComponentSyncAction] = field(default_factory=list)
    pruned_domains: List[str] = field(default_factory=list)
    restart: Optional[RestartOutcome] = None

    @property
    def has_changes(self) -> bool:
        for action in self.actions:
            if action.action in ("installed", "updated", "pruned", "reverted"):
                return True
        return bool(self.pruned_domains)
