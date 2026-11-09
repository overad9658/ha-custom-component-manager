"""GitHub client for resolving refs and extracting Home Assistant custom components."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

import requests

from ha_ccm.models import ComponentConfig, RefType, ResolvedRef


class GitHubError(RuntimeError):
    """Raised when resolving or downloading a component from GitHub fails."""


class GitHubClient:
    """Downloads and extracts Home Assistant custom_components from GitHub repositories."""

    API_BASE = "https://api.github.com"
    CODELOAD_BASE = "https://codeload.github.com"

    def __init__(
        self,
        token: Optional[str] = None,
        session: Optional[requests.Session] = None,
        timeout: int = 30,
    ) -> None:
        self.token = token
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "User-Agent": "ha-custom-component-manager/0.1.0",
            }
        )
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"

    def resolve_ref(self, component: ComponentConfig) -> ResolvedRef:
        """Resolve a branch or release version to a concrete commit/tag and archive URL."""
        if component.ref_type == RefType.BRANCH:
            return self._resolve_branch(component.repo, component.ref)
        return self._resolve_release(component.repo, component.ref)

    def _resolve_branch(self, repo: str, branch: str) -> ResolvedRef:
        url = f"{self.API_BASE}/repos/{repo}/commits/{branch}"
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code == 200:
            data = resp.json()
            sha = str(data.get("sha", "")).strip()
            if sha:
                short_sha = sha[:7]
                return ResolvedRef(
                    repo=repo,
                    ref_type=RefType.BRANCH,
                    requested_ref=branch,
                    resolved_ref=sha,
                    archive_url=f"{self.CODELOAD_BASE}/{repo}/tar.gz/{sha}",
                    version_label=f"{branch}@{short_sha}",
                )
        elif resp.status_code == 404:
            raise GitHubError(
                f"Branch '{branch}' not found in GitHub repository '{repo}' (HTTP 404)."
            )
        elif resp.status_code in (403, 429):
            # Fallback to direct codeload branch archive URL when unauthenticated API rate limit hits
            return ResolvedRef(
                repo=repo,
                ref_type=RefType.BRANCH,
                requested_ref=branch,
                resolved_ref=f"branch:{branch}",
                archive_url=f"{self.CODELOAD_BASE}/{repo}/tar.gz/refs/heads/{branch}",
                version_label=branch,
            )

        raise GitHubError(
            f"Failed to resolve branch '{branch}' for '{repo}': "
            f"HTTP {resp.status_code} - {resp.text[:200]}"
        )

    def _resolve_release(self, repo: str, ref: str) -> ResolvedRef:
        if ref.lower() == "latest":
            url = f"{self.API_BASE}/repos/{repo}/releases/latest"
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                tag_name = str(data["tag_name"])
                return ResolvedRef(
                    repo=repo,
                    ref_type=RefType.RELEASE,
                    requested_ref=ref,
                    resolved_ref=tag_name,
                    archive_url=f"{self.CODELOAD_BASE}/{repo}/tar.gz/refs/tags/{tag_name}",
                    version_label=tag_name,
                )
            if resp.status_code == 404:
                # Fallback to most recent tag if repo uses tags instead of formal GitHub Releases
                tags_resp = self.session.get(
                    f"{self.API_BASE}/repos/{repo}/tags",
                    params={"per_page": 1},
                    timeout=self.timeout,
                )
                if tags_resp.status_code == 200 and tags_resp.json():
                    tag_name = str(tags_resp.json()[0]["name"])
                    return ResolvedRef(
                        repo=repo,
                        ref_type=RefType.RELEASE,
                        requested_ref=ref,
                        resolved_ref=tag_name,
                        archive_url=f"{self.CODELOAD_BASE}/{repo}/tar.gz/refs/tags/{tag_name}",
                        version_label=tag_name,
                    )
                raise GitHubError(f"No releases or tags found for repository '{repo}'.")
            raise GitHubError(
                f"Failed to resolve latest release for '{repo}': HTTP {resp.status_code}"
            )

        # Specific release version/tag: try exact ref first, then with/without 'v' prefix
        candidates = [ref]
        if ref.startswith("v") and len(ref) > 1:
            candidates.append(ref[1:])
        else:
            candidates.append(f"v{ref}")

        for candidate in candidates:
            rel_url = f"{self.API_BASE}/repos/{repo}/releases/tags/{candidate}"
            resp = self.session.get(rel_url, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                tag_name = str(data.get("tag_name", candidate))
                return ResolvedRef(
                    repo=repo,
                    ref_type=RefType.RELEASE,
                    requested_ref=ref,
                    resolved_ref=tag_name,
                    archive_url=f"{self.CODELOAD_BASE}/{repo}/tar.gz/refs/tags/{tag_name}",
                    version_label=tag_name,
                )

            # Check if tag exists directly even if no GitHub Release object was created
            tag_url = f"{self.API_BASE}/repos/{repo}/git/ref/tags/{candidate}"
            tag_resp = self.session.get(tag_url, timeout=self.timeout)
            if tag_resp.status_code == 200:
                return ResolvedRef(
                    repo=repo,
                    ref_type=RefType.RELEASE,
                    requested_ref=ref,
                    resolved_ref=candidate,
                    archive_url=f"{self.CODELOAD_BASE}/{repo}/tar.gz/refs/tags/{candidate}",
                    version_label=candidate,
                )

            if resp.status_code in (403, 429):
                return ResolvedRef(
                    repo=repo,
                    ref_type=RefType.RELEASE,
                    requested_ref=ref,
                    resolved_ref=ref,
                    archive_url=f"{self.CODELOAD_BASE}/{repo}/tar.gz/refs/tags/{ref}",
                    version_label=ref,
                )

        raise GitHubError(
            f"Release or tag '{ref}' not found in GitHub repository '{repo}'."
        )

    def download_and_extract_components(
        self,
        component: ComponentConfig,
        resolved: ResolvedRef,
        staging_dir: Path,
    ) -> Dict[str, Path]:
        """Download the repository archive and extract `custom_components/<domain>` directories.

        Returns a mapping of `{domain_name: local_extracted_domain_path}`.
        """
        resp = self.session.get(resolved.archive_url, timeout=self.timeout)
        if resp.status_code != 200:
            raise GitHubError(
                f"Failed to download archive for '{component.repo}' from {resolved.archive_url} "
                f"(HTTP {resp.status_code})."
            )

        archive_bytes = resp.content
        if resolved.resolved_ref.startswith("branch:"):
            digest = hashlib.sha256(archive_bytes).hexdigest()[:12]
            resolved.resolved_ref = f"{component.ref}:{digest}"
            resolved.version_label = f"{component.ref}@{digest[:7]}"

        extract_root = staging_dir / "extracted"
        if extract_root.exists():
            shutil.rmtree(extract_root)
        extract_root.mkdir(parents=True, exist_ok=True)

        self._extract_archive(archive_bytes, extract_root)

        # Locate the top-level repository folder inside the archive
        subdirs = [p for p in extract_root.iterdir() if p.is_dir()]
        repo_root = subdirs[0] if len(subdirs) == 1 else extract_root

        cc_dir = repo_root / "custom_components"
        if not cc_dir.exists() or not cc_dir.is_dir():
            raise GitHubError(
                f"Repository '{component.repo}' ({resolved.version_label}) does not contain "
                "a 'custom_components/' directory."
            )

        discovered: Dict[str, Path] = {}
        for child in sorted(cc_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if component.domain and child.name != component.domain:
                continue
            manifest_path = child / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(manifest_data, dict):
                    raise ValueError("manifest.json must be a JSON object")
            except Exception as exc:
                raise GitHubError(
                    f"Invalid manifest.json in '{component.repo}' domain '{child.name}': {exc}"
                ) from exc
            discovered[child.name] = child

        if not discovered:
            if component.domain:
                raise GitHubError(
                    f"Domain '{component.domain}' with a valid manifest.json was not found under "
                    f"'custom_components/' in '{component.repo}' ({resolved.version_label})."
                )
            raise GitHubError(
                f"No valid custom component domains (containing manifest.json) found under "
                f"'custom_components/' in '{component.repo}' ({resolved.version_label})."
            )

        return discovered

    @staticmethod
    def _extract_archive(archive_bytes: bytes, dest_dir: Path) -> None:
        """Safely extract a tar.gz or zip archive into dest_dir."""
        dest_resolved = dest_dir.resolve()
        bio = io.BytesIO(archive_bytes)

        if zipfile.is_zipfile(bio):
            bio.seek(0)
            with zipfile.ZipFile(bio) as zf:
                for member in zf.infolist():
                    target_path = (dest_resolved / member.filename).resolve()
                    if not str(target_path).startswith(str(dest_resolved)):
                        raise GitHubError(f"Unsafe path in zip archive: {member.filename}")
                zf.extractall(dest_resolved)
            return

        bio.seek(0)
        try:
            with tarfile.open(fileobj=bio, mode="r:*") as tf:
                safe_members = []
                for member in tf.getmembers():
                    target_path = (dest_resolved / member.name).resolve()
                    if not str(target_path).startswith(str(dest_resolved)):
                        raise GitHubError(f"Unsafe path in tar archive: {member.name}")
                    if member.issym() or member.islnk():
                        continue
                    safe_members.append(member)
                tf.extractall(dest_resolved, members=safe_members)
        except tarfile.TarError as exc:
            raise GitHubError(f"Downloaded file is not a valid tar or zip archive: {exc}") from exc
