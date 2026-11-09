"""Core orchestration logic for syncing, backing up, reverting, and checking custom components."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ha_ccm.github import GitHubClient
from ha_ccm.homeassistant import restart_home_assistant
from ha_ccm.models import (
    AppConfig,
    ComponentConfig,
    ComponentSyncAction,
    LockEntry,
    RestartOutcome,
    SyncReport,
)
from ha_ccm.target import TargetBackend, TargetError, create_target_backend

BACKUP_META_FILENAME = ".ha-ccm-backup.json"


def _lock_key(comp: ComponentConfig) -> str:
    """Return a canonical lockfile key for a ComponentConfig."""
    if comp.domain:
        return f"{comp.repo}#{comp.domain}"
    return comp.repo


def _sanitize_label(label: str) -> str:
    """Sanitize a version label for safe use in directory names."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", label.strip())
    return cleaned.strip("_") or "unknown"


class ComponentManager:
    """Orchestrates downloading, backing up, installing, reverting, pruning, and restarting."""

    def __init__(
        self,
        config: AppConfig,
        github_client: Optional[GitHubClient] = None,
        target_backend: Optional[TargetBackend] = None,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.config = config
        self.github = github_client or GitHubClient(token=config.github_token)
        self._target = target_backend
        self.log = logger or (lambda msg: None)

    def _get_target(self) -> TargetBackend:
        if self._target is not None:
            return self._target
        return create_target_backend(
            self.config.homeassistant,
            lockfile_name=self.config.lockfile_name,
        )

    @property
    def backup_root(self) -> Path:
        return Path(self.config.backup_dir).expanduser().resolve()

    def _backup_domain_from_target(
        self,
        target: TargetBackend,
        domain: str,
        existing_entry: Optional[LockEntry],
        tmp_root: Path,
    ) -> Optional[Path]:
        """Download a copy of an existing custom component domain from the target into backup_dir."""
        if not target.domain_exists(domain):
            return None

        stage_backup = tmp_root / f"backup_stage_{domain}"
        if stage_backup.exists():
            shutil.rmtree(stage_backup)

        self.log(f"  -> Downloading backup of existing domain '{domain}' before install...")
        downloaded = target.download_domain(domain, stage_backup)
        if not downloaded:
            return None

        # Inspect manifest.json in downloaded backup to determine version if not in lockfile
        manifest_version: Optional[str] = None
        manifest_file = stage_backup / "manifest.json"
        if manifest_file.is_file():
            try:
                manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
                if isinstance(manifest_data, dict) and manifest_data.get("version"):
                    manifest_version = str(manifest_data["version"])
            except Exception:
                pass

        version_label = (
            existing_entry.version_label
            if existing_entry and existing_entry.version_label
            else (manifest_version or "untracked")
        )
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_id = f"{timestamp}_{_sanitize_label(version_label)}"

        domain_backup_dir = self.backup_root / domain
        dest_backup_dir = domain_backup_dir / backup_id

        # Ensure unique directory name if multiple backups happen within the same second
        counter = 1
        while dest_backup_dir.exists():
            dest_backup_dir = domain_backup_dir / f"{backup_id}_{counter}"
            counter += 1

        domain_backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage_backup, dest_backup_dir)

        meta = {
            "domain": domain,
            "backup_id": dest_backup_dir.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "version_label": version_label,
            "manifest_version": manifest_version,
            "lock_entry": existing_entry.to_dict() if existing_entry else None,
        }
        (dest_backup_dir / BACKUP_META_FILENAME).write_text(
            json.dumps(meta, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        self.log(f"  -> Saved backup of '{domain}' ({version_label}) to {dest_backup_dir}")
        return dest_backup_dir

    def sync(
        self,
        force: bool = False,
        prune: Optional[bool] = None,
        backup: Optional[bool] = None,
        dry_run: bool = False,
        restart_on_change: bool = True,
        always_restart: bool = False,
    ) -> SyncReport:
        """Synchronize configured custom components to the local or SSH target."""
        should_prune = self.config.prune if prune is None else prune
        should_backup = self.config.backup_enabled if backup is None else backup
        report = SyncReport()

        target = self._get_target()
        with target:
            if not dry_run:
                target.ensure_base_dir()
            lock_state = target.read_lock_state()

            active_keys = set()

            with tempfile.TemporaryDirectory(prefix="ha-ccm-") as tmp_dir:
                tmp_root = Path(tmp_dir)

                for idx, comp in enumerate(self.config.components):
                    key = _lock_key(comp)
                    active_keys.add(key)

                    # Also match legacy repo-only key if domain was later added or vice versa
                    existing = lock_state.installed.get(key) or lock_state.installed.get(comp.repo)

                    self.log(
                        f"Checking {comp.display_name} ({comp.ref_type.value}: {comp.ref})..."
                    )
                    resolved = self.github.resolve_ref(comp)

                    domains_present = bool(existing and existing.domains) and all(
                        target.domain_exists(d) for d in existing.domains
                    )

                    if (
                        not force
                        and existing is not None
                        and existing.resolved_ref == resolved.resolved_ref
                        and domains_present
                    ):
                        self.log(
                            f"  -> Up to date ({resolved.version_label}) "
                            f"[{', '.join(existing.domains)}]"
                        )
                        report.actions.append(
                            ComponentSyncAction(
                                repo=comp.repo,
                                action="up_to_date",
                                domains=list(existing.domains),
                                previous_version=existing.version_label,
                                current_version=resolved.version_label,
                                message=f"Already at {resolved.version_label}",
                            )
                        )
                        continue

                    comp_staging = tmp_root / f"comp_{idx}"
                    comp_staging.mkdir(parents=True, exist_ok=True)
                    extracted_domains = self.github.download_and_extract_components(
                        comp, resolved, comp_staging
                    )
                    domain_names = sorted(extracted_domains.keys())

                    any_domain_exists_on_target = any(
                        target.domain_exists(d) for d in domain_names
                    )
                    action_type = (
                        "updated"
                        if (existing is not None or any_domain_exists_on_target)
                        else "installed"
                    )
                    prev_version = existing.version_label if existing is not None else None
                    comp_backups: List[str] = []

                    if dry_run:
                        if should_backup and any_domain_exists_on_target:
                            self.log(
                                f"  -> [DRY RUN] Would back up existing domain(s) "
                                f"({', '.join(d for d in domain_names if target.domain_exists(d))}) "
                                f"to {self.backup_root}"
                            )
                        self.log(
                            f"  -> [DRY RUN] Would {action_type[:-1]} {comp.display_name} "
                            f"({resolved.version_label}) -> domains: {', '.join(domain_names)}"
                        )
                    else:
                        # If an update removed a domain that previously belonged to this repo, back it up and clean it up
                        if existing is not None:
                            for old_domain in existing.domains:
                                if old_domain not in extracted_domains:
                                    if should_backup:
                                        b_path = self._backup_domain_from_target(
                                            target, old_domain, existing, tmp_root
                                        )
                                        if b_path is not None:
                                            comp_backups.append(str(b_path))
                                    target.remove_domain(old_domain)

                        for domain_name, domain_path in extracted_domains.items():
                            if should_backup:
                                b_path = self._backup_domain_from_target(
                                    target, domain_name, existing, tmp_root
                                )
                                if b_path is not None:
                                    comp_backups.append(str(b_path))

                            self.log(
                                f"  -> Installing domain '{domain_name}' "
                                f"({resolved.version_label})..."
                            )
                            target.install_domain(domain_name, domain_path)

                        # Clean up old key if key changed
                        if comp.repo in lock_state.installed and key != comp.repo:
                            del lock_state.installed[comp.repo]

                        lock_state.installed[key] = LockEntry(
                            repo=comp.repo,
                            ref_type=comp.ref_type.value,
                            requested_ref=comp.ref,
                            resolved_ref=resolved.resolved_ref,
                            version_label=resolved.version_label,
                            domains=domain_names,
                            installed_at=datetime.now(timezone.utc).isoformat(),
                        )

                    report.actions.append(
                        ComponentSyncAction(
                            repo=comp.repo,
                            action=action_type,
                            domains=domain_names,
                            previous_version=prev_version,
                            current_version=resolved.version_label,
                            backup_paths=comp_backups,
                        )
                    )

                if should_prune:
                    stale_keys = [
                        k for k in list(lock_state.installed.keys()) if k not in active_keys
                    ]
                    for stale_key in stale_keys:
                        entry = lock_state.installed[stale_key]
                        pruned_backups: List[str] = []
                        for domain_name in entry.domains:
                            if dry_run:
                                self.log(
                                    f"  -> [DRY RUN] Would prune removed domain '{domain_name}' "
                                    f"from {entry.repo}"
                                )
                            else:
                                if should_backup:
                                    b_path = self._backup_domain_from_target(
                                        target, domain_name, entry, tmp_root
                                    )
                                    if b_path is not None:
                                        pruned_backups.append(str(b_path))
                                self.log(
                                    f"  -> Pruning removed domain '{domain_name}' ({entry.repo})..."
                                )
                                target.remove_domain(domain_name)
                            report.pruned_domains.append(domain_name)

                        report.actions.append(
                            ComponentSyncAction(
                                repo=entry.repo,
                                action="pruned",
                                domains=list(entry.domains),
                                previous_version=entry.version_label,
                                current_version=None,
                                backup_paths=pruned_backups,
                            )
                        )
                        if not dry_run:
                            del lock_state.installed[stale_key]

            if not dry_run and report.has_changes:
                target.write_lock_state(lock_state)

        self._handle_restart(report, restart_on_change=restart_on_change, always_restart=always_restart, dry_run=dry_run)
        return report

    def _handle_restart(
        self,
        report: SyncReport,
        restart_on_change: bool = True,
        always_restart: bool = False,
        dry_run: bool = False,
    ) -> None:
        has_api_key = bool((self.config.homeassistant.api_key or "").strip())
        should_trigger_restart = (report.has_changes and restart_on_change) or always_restart

        if should_trigger_restart and not dry_run:
            report.restart = restart_home_assistant(self.config.homeassistant)
        elif should_trigger_restart and dry_run:
            if has_api_key:
                report.restart = RestartOutcome(
                    attempted=False,
                    restarted=False,
                    manual_restart_required=False,
                    message=(
                        f"[DRY RUN] Would restart Home Assistant via API at "
                        f"{self.config.homeassistant.url}."
                    ),
                )
            else:
                report.restart = restart_home_assistant(self.config.homeassistant)
        elif restart_on_change and not has_api_key:
            report.restart = restart_home_assistant(self.config.homeassistant)

    def list_backups(self, domain: Optional[str] = None) -> List[Dict[str, Any]]:
        """List available local component backups in backup_dir."""
        if not self.backup_root.is_dir():
            return []

        domains_to_scan: List[Path] = []
        if domain:
            d_path = self.backup_root / domain
            if d_path.is_dir():
                domains_to_scan.append(d_path)
        else:
            domains_to_scan = sorted(
                p for p in self.backup_root.iterdir() if p.is_dir() and not p.name.startswith(".")
            )

        backups: List[Dict[str, Any]] = []
        for dom_dir in domains_to_scan:
            for child in sorted(dom_dir.iterdir(), reverse=True):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                meta_file = child / BACKUP_META_FILENAME
                meta: Dict[str, Any] = {}
                if meta_file.is_file():
                    try:
                        meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    except Exception:
                        meta = {}
                backups.append(
                    {
                        "domain": dom_dir.name,
                        "backup_id": child.name,
                        "path": str(child),
                        "version_label": meta.get("version_label", "unknown"),
                        "manifest_version": meta.get("manifest_version"),
                        "created_at": meta.get("created_at", ""),
                    }
                )
        return backups

    def revert(
        self,
        domain: str,
        backup_id: Optional[str] = None,
        restart_on_change: bool = True,
    ) -> SyncReport:
        """Revert a custom component domain on the target from a saved backup."""
        domain_backup_dir = self.backup_root / domain
        if not domain_backup_dir.is_dir():
            raise TargetError(
                f"No backups found for domain '{domain}' in '{self.backup_root}'."
            )

        if backup_id:
            selected_dir = domain_backup_dir / backup_id
            if not selected_dir.is_dir():
                raise TargetError(
                    f"Backup '{backup_id}' for domain '{domain}' not found at '{selected_dir}'."
                )
        else:
            candidates = sorted(
                (p for p in domain_backup_dir.iterdir() if p.is_dir() and not p.name.startswith(".")),
                reverse=True,
            )
            if not candidates:
                raise TargetError(
                    f"No backup snapshots found for domain '{domain}' in '{domain_backup_dir}'."
                )
            selected_dir = candidates[0]

        if not (selected_dir / "manifest.json").is_file():
            raise TargetError(
                f"Backup directory '{selected_dir}' does not contain a valid manifest.json."
            )

        meta: Dict[str, Any] = {}
        meta_file = selected_dir / BACKUP_META_FILENAME
        if meta_file.is_file():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                meta = {}

        restored_version = str(meta.get("version_label") or selected_dir.name)
        lock_entry_dict = meta.get("lock_entry")
        restored_repo = (
            str(lock_entry_dict["repo"])
            if isinstance(lock_entry_dict, dict) and lock_entry_dict.get("repo")
            else domain
        )

        report = SyncReport()
        target = self._get_target()
        with target:
            target.ensure_base_dir()
            lock_state = target.read_lock_state()

            with tempfile.TemporaryDirectory(prefix="ha-ccm-revert-") as tmp_dir:
                clean_stage = Path(tmp_dir) / domain
                shutil.copytree(selected_dir, clean_stage)
                staged_meta = clean_stage / BACKUP_META_FILENAME
                if staged_meta.exists():
                    staged_meta.unlink()

                self.log(
                    f"Reverting domain '{domain}' from backup '{selected_dir.name}' ({restored_version})..."
                )
                target.install_domain(domain, clean_stage)

            if isinstance(lock_entry_dict, dict) and lock_entry_dict.get("repo"):
                entry = LockEntry.from_dict(lock_entry_dict)
                entry.installed_at = datetime.now(timezone.utc).isoformat()
                lock_state.installed[entry.repo] = entry
                target.write_lock_state(lock_state)

            report.actions.append(
                ComponentSyncAction(
                    repo=restored_repo,
                    action="reverted",
                    domains=[domain],
                    previous_version=None,
                    current_version=restored_version,
                    backup_paths=[str(selected_dir)],
                    message=f"Reverted '{domain}' from {selected_dir}",
                )
            )

        self._handle_restart(report, restart_on_change=restart_on_change, always_restart=False, dry_run=False)
        return report

    def status(self) -> List[Dict[str, Any]]:
        """Check status of configured components against the target lockfile and GitHub."""
        results: List[Dict[str, Any]] = []
        target = self._get_target()
        with target:
            lock_state = target.read_lock_state()
            for comp in self.config.components:
                key = _lock_key(comp)
                existing = lock_state.installed.get(key) or lock_state.installed.get(comp.repo)
                resolved = self.github.resolve_ref(comp)
                domains_exist = bool(existing and existing.domains) and all(
                    target.domain_exists(d) for d in existing.domains
                )

                if existing is None or not domains_exist:
                    state = "not_installed"
                elif existing.resolved_ref == resolved.resolved_ref:
                    state = "up_to_date"
                else:
                    state = "update_available"

                results.append(
                    {
                        "repo": comp.repo,
                        "name": comp.display_name,
                        "ref_type": comp.ref_type.value,
                        "requested_ref": comp.ref,
                        "installed_version": existing.version_label if existing else None,
                        "latest_version": resolved.version_label,
                        "domains": existing.domains if existing else ([comp.domain] if comp.domain else []),
                        "status": state,
                    }
                )
        return results
