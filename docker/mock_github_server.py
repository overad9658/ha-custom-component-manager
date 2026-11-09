"""Mock GitHub API and Codeload archive server for local development and integration tests."""

from __future__ import annotations

import copy
import io
import json
import os
import re
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import urlparse


def build_tarball_bytes(repo_slug: str, ref_suffix: str, domains: Dict[str, Dict[str, Any]]) -> bytes:
    """Create an in-memory .tar.gz archive with standard HACS custom_components layout."""
    repo_name = repo_slug.split("/")[-1]
    safe_ref = re.sub(r"[^A-Za-z0-9._-]+", "-", ref_suffix)
    prefix = f"{repo_name}-{safe_ref}"

    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w:gz") as tf:
        for domain, files in domains.items():
            for rel_path, content in files.items():
                full_name = f"{prefix}/custom_components/{domain}/{rel_path}"
                data = (
                    json.dumps(content, indent=2).encode("utf-8")
                    if isinstance(content, dict)
                    else str(content).encode("utf-8")
                )
                info = tarfile.TarInfo(name=full_name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return bio.getvalue()


DEFAULT_REPOS: Dict[str, Dict[str, Any]] = {
    "mvdwetering/huesyncbox": {
        "branches": {
            "dev": {
                "sha": "1111111222222233333334444444555555566666",
                "domains": {
                    "huesyncbox": {
                        "manifest.json": {
                            "domain": "huesyncbox",
                            "name": "Philips Hue Play HDMI Sync Box",
                            "version": "1.30.0",
                            "documentation": "https://github.com/mvdwetering/huesyncbox",
                            "codeowners": ["@mvdwetering"],
                        },
                        "__init__.py": '"""Hue Sync Box integration."""\nVERSION = "1.30.0"\n',
                        "sensor.py": '"""Sensor platform for Hue Sync Box."""\n',
                        "translations/en.json": {"title": "Philips Hue Play HDMI Sync Box"},
                    }
                },
            },
            "main": {
                "sha": "aaaaaaa222222233333334444444555555566666",
                "domains": {
                    "huesyncbox": {
                        "manifest.json": {
                            "domain": "huesyncbox",
                            "name": "Philips Hue Play HDMI Sync Box",
                            "version": "1.29.0",
                        },
                        "__init__.py": '"""Hue Sync Box integration."""\nVERSION = "1.29.0"\n',
                    }
                },
            },
        },
        "releases": {
            "v1.30.0": {
                "tag_name": "v1.30.0",
                "latest": True,
                "domains": {
                    "huesyncbox": {
                        "manifest.json": {
                            "domain": "huesyncbox",
                            "name": "Philips Hue Play HDMI Sync Box",
                            "version": "1.30.0",
                        },
                        "__init__.py": '"""Hue Sync Box release."""\nVERSION = "1.30.0"\n',
                    }
                },
            }
        },
    },
    "hacs/integration": {
        "branches": {
            "main": {
                "sha": "bbbbbbb222222233333334444444555555577777",
                "domains": {
                    "hacs": {
                        "manifest.json": {
                            "domain": "hacs",
                            "name": "HACS",
                            "version": "2.0.1",
                        },
                        "__init__.py": '"""HACS integration."""\nVERSION = "2.0.1"\n',
                    }
                },
            }
        },
        "releases": {
            "v2.0.0": {
                "tag_name": "v2.0.0",
                "latest": False,
                "domains": {
                    "hacs": {
                        "manifest.json": {
                            "domain": "hacs",
                            "name": "HACS",
                            "version": "2.0.0",
                        },
                        "__init__.py": '"""HACS integration."""\nVERSION = "2.0.0"\n',
                    }
                },
            },
            "v2.0.1": {
                "tag_name": "v2.0.1",
                "latest": True,
                "domains": {
                    "hacs": {
                        "manifest.json": {
                            "domain": "hacs",
                            "name": "HACS",
                            "version": "2.0.1",
                        },
                        "__init__.py": '"""HACS integration."""\nVERSION = "2.0.1"\n',
                    }
                },
            },
        },
    },
}


class MockGitHubState:
    """Thread-safe in-memory state for the mock GitHub API & Codeload server."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.repos: Dict[str, Dict[str, Any]] = copy.deepcopy(DEFAULT_REPOS)

    def reset(self) -> None:
        with self._lock:
            self.repos = copy.deepcopy(DEFAULT_REPOS)

    def update_branch(
        self,
        repo: str,
        branch: str,
        sha: str,
        domains: Dict[str, Dict[str, Any]],
    ) -> None:
        with self._lock:
            repo_entry = self.repos.setdefault(repo, {"branches": {}, "releases": {}})
            repo_entry.setdefault("branches", {})[branch] = {
                "sha": sha,
                "domains": domains,
            }

    def update_release(
        self,
        repo: str,
        tag_name: str,
        domains: Dict[str, Dict[str, Any]],
        latest: bool = True,
    ) -> None:
        with self._lock:
            repo_entry = self.repos.setdefault(repo, {"branches": {}, "releases": {}})
            releases = repo_entry.setdefault("releases", {})
            if latest:
                for rel in releases.values():
                    rel["latest"] = False
            releases[tag_name] = {
                "tag_name": tag_name,
                "latest": latest,
                "domains": domains,
            }

    def get_branch(self, repo: str, branch: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self.repos.get(repo, {}).get("branches", {}).get(branch))

    def get_latest_release(self, repo: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            releases = self.repos.get(repo, {}).get("releases", {})
            for rel in releases.values():
                if rel.get("latest"):
                    return copy.deepcopy(rel)
            if releases:
                last_key = list(releases.keys())[-1]
                return copy.deepcopy(releases[last_key])
            return None

    def get_release_by_tag(self, repo: str, tag: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self.repos.get(repo, {}).get("releases", {}).get(tag))

    def get_tags(self, repo: str) -> list[Dict[str, Any]]:
        with self._lock:
            releases = self.repos.get(repo, {}).get("releases", {})
            return [{"name": tag} for tag in reversed(list(releases.keys()))]

    def resolve_archive_domains(self, repo: str, ref_path: str) -> Optional[Dict[str, Dict[str, Any]]]:
        """Find domain file tree for a commit SHA, branch ref, or tag ref."""
        with self._lock:
            repo_data = self.repos.get(repo)
            if not repo_data:
                return None

            cleaned = ref_path.strip("/")
            if cleaned.startswith("refs/heads/"):
                branch_name = cleaned[len("refs/heads/") :]
                b_entry = repo_data.get("branches", {}).get(branch_name)
                return copy.deepcopy(b_entry["domains"]) if b_entry else None

            if cleaned.startswith("refs/tags/"):
                tag_name = cleaned[len("refs/tags/") :]
                r_entry = repo_data.get("releases", {}).get(tag_name)
                return copy.deepcopy(r_entry["domains"]) if r_entry else None

            # Match by commit SHA or branch name or tag name directly
            for b_name, b_entry in repo_data.get("branches", {}).items():
                if b_entry.get("sha") == cleaned or b_name == cleaned:
                    return copy.deepcopy(b_entry["domains"])

            for t_name, r_entry in repo_data.get("releases", {}).items():
                if t_name == cleaned:
                    return copy.deepcopy(r_entry["domains"])

            return None


def make_github_handler(state: MockGitHubState) -> type[BaseHTTPRequestHandler]:
    """Create a request handler bound to a MockGitHubState instance."""

    class MockGitHubHandler(BaseHTTPRequestHandler):
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

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            if path == "/health":
                self._send_json(200, {"status": "ok"})
                return

            # 1. Commit resolution: /repos/<owner>/<repo>/commits/<branch>
            m_commit = re.match(r"^/repos/([^/]+/[^/]+)/commits/(.+)$", path)
            if m_commit:
                repo, branch = m_commit.group(1), m_commit.group(2)
                b_data = state.get_branch(repo, branch)
                if b_data is None:
                    self._send_json(404, {"message": "Branch not found"})
                    return
                self._send_json(200, {"sha": b_data["sha"], "commit": {"message": f"Mock commit on {branch}"}})
                return

            # 2. Latest release: /repos/<owner>/<repo>/releases/latest
            m_latest = re.match(r"^/repos/([^/]+/[^/]+)/releases/latest$", path)
            if m_latest:
                repo = m_latest.group(1)
                rel = state.get_latest_release(repo)
                if rel is None:
                    self._send_json(404, {"message": "Not Found"})
                    return
                self._send_json(200, {"tag_name": rel["tag_name"], "name": rel["tag_name"]})
                return

            # 3. Release by tag: /repos/<owner>/<repo>/releases/tags/<tag>
            m_rel_tag = re.match(r"^/repos/([^/]+/[^/]+)/releases/tags/(.+)$", path)
            if m_rel_tag:
                repo, tag = m_rel_tag.group(1), m_rel_tag.group(2)
                rel = state.get_release_by_tag(repo, tag)
                if rel is None:
                    self._send_json(404, {"message": "Not Found"})
                    return
                self._send_json(200, {"tag_name": rel["tag_name"], "name": rel["tag_name"]})
                return

            # 4. Git ref tag: /repos/<owner>/<repo>/git/ref/tags/<tag>
            m_git_tag = re.match(r"^/repos/([^/]+/[^/]+)/git/ref/tags/(.+)$", path)
            if m_git_tag:
                repo, tag = m_git_tag.group(1), m_git_tag.group(2)
                rel = state.get_release_by_tag(repo, tag)
                if rel is None:
                    self._send_json(404, {"message": "Not Found"})
                    return
                self._send_json(200, {"ref": f"refs/tags/{rel['tag_name']}"})
                return

            # 5. Tags list: /repos/<owner>/<repo>/tags
            m_tags = re.match(r"^/repos/([^/]+/[^/]+)/tags$", path)
            if m_tags:
                repo = m_tags.group(1)
                tags = state.get_tags(repo)
                self._send_json(200, tags)
                return

            # 6. Codeload tarball download: /codeload/<owner>/<repo>/tar.gz/<ref...>
            # Also support without /codeload prefix if CODELOAD_BASE points to root
            m_tar = re.match(r"^(?:/codeload)?/([^/]+/[^/]+)/tar\.gz/(.+)$", path)
            if m_tar:
                repo, ref_path = m_tar.group(1), m_tar.group(2)
                domains = state.resolve_archive_domains(repo, ref_path)
                if domains is None:
                    self._send_json(404, {"message": f"Archive not found for {repo} @ {ref_path}"})
                    return
                tar_bytes = build_tarball_bytes(repo, ref_path, domains)
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Length", str(len(tar_bytes)))
                self.end_headers()
                self.wfile.write(tar_bytes)
                return

            self._send_json(404, {"message": f"Unhandled mock GitHub path: {path}"})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length > 0 else b"{}"
            try:
                data = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except Exception:
                data = {}

            if path == "/_test/reset":
                state.reset()
                self._send_json(200, {"status": "reset"})
                return

            if path == "/_test/update_branch":
                repo = data["repo"]
                branch = data.get("branch", "dev")
                sha = data["sha"]
                domains = data["domains"]
                state.update_branch(repo, branch, sha, domains)
                self._send_json(200, {"status": "updated", "repo": repo, "branch": branch, "sha": sha})
                return

            if path == "/_test/update_release":
                repo = data["repo"]
                tag_name = data["tag_name"]
                domains = data["domains"]
                latest = bool(data.get("latest", True))
                state.update_release(repo, tag_name, domains, latest=latest)
                self._send_json(200, {"status": "updated", "repo": repo, "tag_name": tag_name})
                return

            self._send_json(404, {"message": f"Unhandled POST path: {path}"})

    return MockGitHubHandler


def start_mock_github_server(host: str = "0.0.0.0", port: int = 8000) -> tuple[ThreadingHTTPServer, MockGitHubState]:
    """Create and return a (ThreadingHTTPServer, MockGitHubState) tuple."""
    state = MockGitHubState()
    server = ThreadingHTTPServer((host, port), make_github_handler(state))
    return server, state


def main() -> None:
    host = os.environ.get("MOCK_GITHUB_HOST", "0.0.0.0")
    port = int(os.environ.get("MOCK_GITHUB_PORT", "8000"))
    server, _ = start_mock_github_server(host, port)
    print(f"Mock GitHub API & Codeload server listening on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
