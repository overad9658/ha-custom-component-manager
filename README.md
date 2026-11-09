# Home Assistant Custom Component Manager (`ha-ccm`)

A Python CLI application that manages Home Assistant `custom_components` from GitHub repositories defined in a YAML file.

## Features

- **YAML-Driven Configuration**: Define all your custom components in [`components.yaml`](components.yaml) with support for environment variable interpolation (`${HA_API_KEY}`, `${GITHUB_TOKEN}`, `${HA_SSH_PASSWORD}`).
- **Branch or Versioned Release Downloads**:
  - Track a named branch (`main`, `dev`, `release`, etc.) or pass a GitHub tree URL (e.g. `https://github.com/mvdwetering/huesyncbox/tree/dev/custom_components`).
  - Pin to a specific release version/tag (`v1.45.0`, `1.45.0`) or `latest` GitHub release.
- **Standard HACS Layout Support**: Automatically locates and validates `custom_components/<domain>/manifest.json` inside each downloaded repository archive.
- **Local or Remote SSH Deployment**:
  - **Local mode**: Installs directly into a local `custom_components` directory.
  - **SSH mode**: Connects to a remote Home Assistant host over SSH/SFTP (supporting SSH keys or passwords) and syncs directly to `/config/custom_components`.
- **Lockfile & Pruning**: Tracks installed commit SHAs / release tags in `.ha-ccm-lock.json` inside `custom_components` so unchanged components are skipped, with optional `--prune` support to remove deleted components.
- **Automatic or Manual Restart Handling**:
  - When a Home Assistant Long-Lived Access Token (`api_key`) is supplied, automatically calls `POST /api/services/homeassistant/restart` when components change.
  - When no `api_key` is supplied, outputs a clear notice reminding you to manually restart Home Assistant.

---

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 setup.py develop --no-deps
```

---

## Configuration (`components.yaml`)

```yaml
homeassistant:
  # "local" or "ssh"
  mode: local

  # Path to custom_components (local path or remote path over SSH)
  custom_components_dir: ./custom_components

  # Home Assistant URL and optional Long-Lived Access Token
  url: http://homeassistant.local:8123
  api_key: "${HA_API_KEY:-}"

  # Required when mode is "ssh"
  ssh:
    host: homeassistant.local
    port: 22
    username: root
    key_file: ~/.ssh/id_ed25519
    # password: "${HA_SSH_PASSWORD}"
    strict_host_key_checking: false

# Remove previously installed managed components when removed from YAML
prune: true

# Optional GitHub token to avoid unauthenticated API rate limits
github_token: "${GITHUB_TOKEN:-}"

components:
  # 1. Named branch example
  - name: Philips Hue Play HDMI Sync Box
    repo: mvdwetering/huesyncbox
    branch: dev

  # 2. Direct GitHub tree URL example (auto-detects repo and 'dev' branch)
  # - repo: https://github.com/mvdwetering/huesyncbox/tree/dev/custom_components

  # 3. Versioned release example ("latest" or a specific version tag)
  # - name: HACS
  #   repo: hacs/integration
  #   version: v2.0.1
```

---

## Usage

### Sync Custom Components

```bash
# Sync using default components.yaml
ha-ccm sync -c components.yaml

# Preview changes without modifying files or restarting
ha-ccm sync -c components.yaml --dry-run

# Force re-download all components and prune removed ones
ha-ccm sync -c components.yaml --force --prune

# Override to deploy over SSH from the CLI
ha-ccm sync -c components.yaml \
  --mode ssh \
  --ssh-host homeassistant.local \
  --ssh-user root \
  --ssh-key ~/.ssh/id_ed25519 \
  --target-dir /config/custom_components

# Supply a Home Assistant API key via CLI to automatically restart on changes
ha-ccm sync -c components.yaml --ha-url http://homeassistant.local:8123 --api-key "YOUR_LONG_LIVED_TOKEN"
```

### Check Status

```bash
ha-ccm status -c components.yaml
```

### List Backups & Revert a Component

Before installing or updating an existing custom component on the target (local or SSH), `ha-ccm` automatically downloads a backup copy into `./backups/<domain>/<timestamp>_<version>/`.

```bash
# List all saved backups (or filter by domain)
ha-ccm backups -c components.yaml
ha-ccm backups huesyncbox -c components.yaml

# Revert a component domain to its most recent backup (or a specific --backup-id)
ha-ccm revert huesyncbox -c components.yaml
ha-ccm revert huesyncbox --backup-id 20261108_150805_v3.2.3 -c components.yaml
```

### Validate Configuration

```bash
ha-ccm validate -c components.yaml
```

### Trigger Restart Manually

```bash
ha-ccm restart -c components.yaml --api-key "YOUR_LONG_LIVED_TOKEN"
```

---

## Local Development & Testing with Docker Compose

A self-contained Docker Compose environment ([`docker-compose.yml`](docker-compose.yml)) is included for local development and end-to-end testing (also executed in GitHub Actions):

- **`ha-instance`**: Runs a real OpenSSH/SFTP server (`/config/custom_components`) with both password and SSH key authentication (unencrypted + passphrase-encrypted) alongside a mock Home Assistant REST API (`http://ha-instance:8123`).
- **`github-mock`**: Serves a mock GitHub REST API and Codeload tarball archive server (`http://github-mock:8000`) for deterministic, offline testing of branch commits, release tags, updates, backups, reverts, and pruning.
- **`test`**: Mounts your local workspace (`.:/workspace`) so code edits can be tested immediately without rebuilding.
- **`ha-ccm`**: Interactive CLI service pre-configured to talk to `ha-instance` and `github-mock` via [`docker/components.docker.yaml`](docker/components.docker.yaml).

### Run the Full Test Suite (Unit + Docker SSH/API Integration Tests)

```bash
# Build and run all unit and Docker integration tests
docker compose run --rm --build test

# After editing files in src/ or tests/, re-run immediately without rebuilding
docker compose run --rm test

# Stop and clean up containers and volumes
docker compose down -v
```

### Interactive CLI Testing in Docker Compose

```bash
# Sync components to the containerized Home Assistant SSH target
docker compose run --rm ha-ccm sync -c docker/components.docker.yaml

# Check component status against the mock GitHub server
docker compose run --rm ha-ccm status -c docker/components.docker.yaml

# List backups and revert a component over SSH
docker compose run --rm ha-ccm backups -c docker/components.docker.yaml
docker compose run --rm ha-ccm revert huesyncbox -c docker/components.docker.yaml
```

### Running Unit Tests Directly in a Local Virtualenv

```bash
.venv/bin/pytest -v
```
