"""YAML configuration loader and validator for ha-ccm."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import yaml

from ha_ccm.models import (
    AppConfig,
    ComponentConfig,
    HomeAssistantConfig,
    RefType,
    SSHConfig,
    TargetMode,
)

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_REPO_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ConfigError(ValueError):
    """Raised when the YAML configuration is invalid."""


def interpolate_env(value: Any) -> Any:
    """Recursively expand ${VAR} and ${VAR:-default} in strings."""
    if isinstance(value, str):
        def _replacer(match: re.Match[str]) -> str:
            var_name = match.group(1)
            default_val = match.group(2)
            env_val = os.environ.get(var_name)
            if env_val is not None:
                return env_val
            if default_val is not None:
                return default_val
            return ""

        return _ENV_PATTERN.sub(_replacer, value)
    if isinstance(value, list):
        return [interpolate_env(item) for item in value]
    if isinstance(value, dict):
        return {k: interpolate_env(v) for k, v in value.items()}
    return value


def parse_github_repo(raw_repo: str) -> Tuple[str, Optional[str]]:
    """Normalize a GitHub repo identifier or URL into ('owner/repo', optional_branch_from_url).

    Supports:
      - 'owner/repo'
      - 'https://github.com/owner/repo'
      - 'https://github.com/owner/repo.git'
      - 'https://github.com/owner/repo/tree/<branch>/custom_components'
    """
    cleaned = raw_repo.strip()
    if not cleaned:
        raise ConfigError("Component repository cannot be empty.")

    if cleaned.startswith(("http://", "https://")):
        parsed = urlparse(cleaned)
        if parsed.netloc.lower() not in ("github.com", "www.github.com"):
            raise ConfigError(f"Unsupported repository host '{parsed.netloc}' in '{raw_repo}'. Only github.com is supported.")
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        if len(parts) < 2:
            raise ConfigError(f"Invalid GitHub URL '{raw_repo}'. Expected 'https://github.com/<owner>/<repo>'.")
        owner = parts[0]
        repo_name = parts[1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        slug = f"{owner}/{repo_name}"

        url_branch: Optional[str] = None
        if len(parts) >= 4 and parts[2] == "tree":
            url_branch = parts[3]
        return slug, url_branch

    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    cleaned = cleaned.strip("/")

    if not _REPO_SLUG_PATTERN.match(cleaned):
        raise ConfigError(
            f"Invalid GitHub repository '{raw_repo}'. Expected 'owner/repo' or a GitHub URL."
        )
    return cleaned, None


def _parse_component(item: Any, index: int) -> ComponentConfig:
    """Parse a single component entry from the YAML 'components' list."""
    if isinstance(item, str):
        slug, url_branch = parse_github_repo(item)
        return ComponentConfig(
            repo=slug,
            ref_type=RefType.BRANCH,
            ref=url_branch or "main",
        )

    if not isinstance(item, dict):
        raise ConfigError(f"Component at index {index} must be a mapping or string.")

    raw_repo = item.get("repo") or item.get("repository") or item.get("url")
    if not raw_repo or not isinstance(raw_repo, str):
        raise ConfigError(
            f"Component at index {index} is missing required field 'repo' (or 'url')."
        )

    slug, url_branch = parse_github_repo(raw_repo)

    branch = item.get("branch")
    release = item.get("version") if "version" in item else item.get("release", item.get("tag"))

    if branch is not None and release is not None:
        raise ConfigError(
            f"Component '{slug}' specifies both 'branch' ({branch}) and 'version/release' ({release}). "
            "Specify only one."
        )

    if release is not None:
        ref_type = RefType.RELEASE
        ref = str(release).strip()
        if not ref:
            raise ConfigError(f"Component '{slug}' has an empty release/version value.")
    elif branch is not None:
        ref_type = RefType.BRANCH
        ref = str(branch).strip()
        if not ref:
            raise ConfigError(f"Component '{slug}' has an empty branch value.")
    elif url_branch is not None:
        ref_type = RefType.BRANCH
        ref = url_branch
    else:
        ref_type = RefType.BRANCH
        ref = "main"

    name = str(item["name"]).strip() if item.get("name") else None
    domain = str(item["domain"]).strip() if item.get("domain") else None

    return ComponentConfig(
        repo=slug,
        ref_type=ref_type,
        ref=ref,
        name=name,
        domain=domain,
    )


def load_config(config_path: str | Path) -> AppConfig:
    """Load and validate an AppConfig from a YAML file."""
    path = Path(config_path)
    if not path.exists():
        raise ConfigError(f"Configuration file not found: {path}")

    try:
        raw_text = path.read_text(encoding="utf-8")
        raw_data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Failed to parse YAML file '{path}': {exc}") from exc

    if raw_data is None:
        raw_data = {}
    if not isinstance(raw_data, dict):
        raise ConfigError(f"Top-level YAML content in '{path}' must be a mapping.")

    data: Dict[str, Any] = interpolate_env(raw_data)

    ha_raw = data.get("homeassistant") or data.get("home_assistant") or {}
    if not isinstance(ha_raw, dict):
        raise ConfigError("'homeassistant' section in YAML must be a mapping.")

    # Allow top-level ssh / custom_components_dir / api_key convenience keys or nested under homeassistant
    ssh_raw = ha_raw.get("ssh") or data.get("ssh")
    ssh_config: Optional[SSHConfig] = None
    if ssh_raw:
        if not isinstance(ssh_raw, dict):
            raise ConfigError("'ssh' configuration must be a mapping.")
        host = str(ssh_raw.get("host", "")).strip()
        if not host:
            raise ConfigError("'ssh.host' is required when an 'ssh' section is provided.")
        port = int(ssh_raw.get("port", 22))
        username = str(ssh_raw.get("username") or ssh_raw.get("user") or "root").strip()
        password = ssh_raw.get("password") or os.environ.get("HA_SSH_PASSWORD")
        passphrase = (
            ssh_raw.get("passphrase")
            or ssh_raw.get("key_passphrase")
            or os.environ.get("HA_SSH_PASSPHRASE")
        )
        key_file = ssh_raw.get("key_file") or ssh_raw.get("private_key") or ssh_raw.get("private_key_path")
        if key_file:
            key_file = str(Path(str(key_file)).expanduser())
        strict_host_key = bool(ssh_raw.get("strict_host_key_checking", False))
        connect_timeout = int(ssh_raw.get("connect_timeout", 15))
        ssh_config = SSHConfig(
            host=host,
            username=username,
            port=port,
            password=str(password) if password else None,
            key_file=key_file,
            passphrase=str(passphrase) if passphrase else None,
            strict_host_key_checking=strict_host_key,
            connect_timeout=connect_timeout,
        )

    raw_mode = ha_raw.get("mode") or data.get("mode")
    if raw_mode:
        mode_str = str(raw_mode).strip().lower()
        if mode_str not in (TargetMode.LOCAL.value, TargetMode.SSH.value):
            raise ConfigError(f"Invalid mode '{raw_mode}'. Must be 'local' or 'ssh'.")
        mode = TargetMode(mode_str)
    else:
        mode = TargetMode.SSH if ssh_config is not None else TargetMode.LOCAL

    if mode == TargetMode.SSH and ssh_config is None:
        raise ConfigError("Mode is set to 'ssh', but no 'ssh' configuration block was provided.")

    custom_components_dir = str(
        ha_raw.get("custom_components_dir")
        or ha_raw.get("target_dir")
        or data.get("custom_components_dir")
        or ("./custom_components" if mode == TargetMode.LOCAL else "/config/custom_components")
    ).strip()

    api_key = (
        ha_raw.get("api_key")
        or ha_raw.get("token")
        or data.get("api_key")
        or os.environ.get("HA_API_KEY")
        or os.environ.get("HASS_TOKEN")
    )
    if api_key is not None:
        api_key = str(api_key).strip() or None

    ha_url = (
        ha_raw.get("url")
        or data.get("ha_url")
        or os.environ.get("HA_URL")
        or os.environ.get("HASS_URL")
    )
    if ha_url is not None:
        ha_url = str(ha_url).strip().rstrip("/") or None
    elif ssh_config is not None:
        ha_url = f"http://{ssh_config.host}:8123"
    else:
        ha_url = "http://localhost:8123"

    verify_ssl = bool(ha_raw.get("verify_ssl", True))

    ha_config = HomeAssistantConfig(
        custom_components_dir=custom_components_dir,
        mode=mode,
        url=ha_url,
        api_key=api_key,
        verify_ssl=verify_ssl,
        ssh=ssh_config,
    )

    raw_components = data.get("components")
    if raw_components is None:
        raise ConfigError("Configuration is missing required 'components' list.")
    if not isinstance(raw_components, list):
        raise ConfigError("'components' in YAML must be a list.")

    components: List[ComponentConfig] = []
    seen_repos: Dict[str, int] = {}
    for idx, item in enumerate(raw_components):
        comp = _parse_component(item, idx)
        key = f"{comp.repo}:{comp.domain or '*'}"
        if key in seen_repos:
            raise ConfigError(
                f"Duplicate component entry for '{comp.repo}' at index {idx} "
                f"(already defined at index {seen_repos[key]})."
            )
        seen_repos[key] = idx
        components.append(comp)

    github_token = (
        data.get("github_token")
        or os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
    )
    if github_token is not None:
        github_token = str(github_token).strip() or None

    prune = bool(data.get("prune", ha_raw.get("prune", False)))
    backup_raw = data.get("backup_enabled", data.get("backup", ha_raw.get("backup_enabled", ha_raw.get("backup", True))))
    backup_enabled = bool(backup_raw)
    backup_dir = str(
        data.get("backup_dir")
        or ha_raw.get("backup_dir")
        or "./backups"
    ).strip()

    return AppConfig(
        homeassistant=ha_config,
        components=components,
        github_token=github_token,
        prune=prune,
        backup_enabled=backup_enabled,
        backup_dir=backup_dir,
    )
