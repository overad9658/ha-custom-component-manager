"""Command-line interface for Home Assistant Custom Component Manager (ha-ccm)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click

from ha_ccm.config import ConfigError, load_config
from ha_ccm.github import GitHubError
from ha_ccm.homeassistant import HomeAssistantAPIError, restart_home_assistant
from ha_ccm.manager import ComponentManager
from ha_ccm.models import AppConfig, SSHConfig, TargetMode
from ha_ccm.target import TargetError


def _apply_cli_overrides(
    config: AppConfig,
    mode: Optional[str] = None,
    target_dir: Optional[str] = None,
    ssh_host: Optional[str] = None,
    ssh_port: Optional[int] = None,
    ssh_user: Optional[str] = None,
    ssh_key: Optional[str] = None,
    ssh_password: Optional[str] = None,
    ssh_passphrase: Optional[str] = None,
    ha_url: Optional[str] = None,
    api_key: Optional[str] = None,
    github_token: Optional[str] = None,
    backup_dir: Optional[str] = None,
) -> AppConfig:
    """Apply CLI option overrides onto a loaded AppConfig."""
    ha = config.homeassistant

    if ssh_host or ssh_port or ssh_user or ssh_key or ssh_password or ssh_passphrase:
        existing_ssh = ha.ssh
        host = ssh_host or (existing_ssh.host if existing_ssh else "")
        if not host:
            raise ConfigError("--ssh-host is required when specifying SSH CLI options without ssh.host in YAML.")
        ha.ssh = SSHConfig(
            host=host,
            port=ssh_port or (existing_ssh.port if existing_ssh else 22),
            username=ssh_user or (existing_ssh.username if existing_ssh else "root"),
            password=ssh_password or (existing_ssh.password if existing_ssh else None),
            key_file=str(Path(ssh_key).expanduser()) if ssh_key else (existing_ssh.key_file if existing_ssh else None),
            passphrase=ssh_passphrase or (existing_ssh.passphrase if existing_ssh else None),
            strict_host_key_checking=existing_ssh.strict_host_key_checking if existing_ssh else False,
            connect_timeout=existing_ssh.connect_timeout if existing_ssh else 15,
        )
        if mode is None:
            ha.mode = TargetMode.SSH

    if mode is not None:
        ha.mode = TargetMode(mode.lower())
        if ha.mode == TargetMode.SSH and ha.ssh is None:
            raise ConfigError("Mode 'ssh' requires SSH host configuration in YAML or via --ssh-host.")

    if target_dir is not None:
        ha.custom_components_dir = target_dir

    if ha_url is not None:
        ha.url = ha_url.rstrip("/")

    if api_key is not None:
        ha.api_key = api_key.strip() or None

    if github_token is not None:
        config.github_token = github_token.strip() or None

    if backup_dir is not None:
        config.backup_dir = backup_dir

    return config


@click.group(invoke_without_command=True)
@click.version_option(package_name="ha-custom-component-manager")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Home Assistant Custom Component Manager (ha-ccm).

    Downloads custom components from GitHub (by branch or release version)
    defined in a YAML file, installs them locally or remotely over SSH into
    Home Assistant's custom_components directory, and optionally restarts
    Home Assistant via its REST API.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command("sync")
@click.option(
    "-c",
    "--config",
    "config_path",
    default="components.yaml",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Path to the YAML configuration file.",
)
@click.option(
    "--mode",
    type=click.Choice(["local", "ssh"], case_sensitive=False),
    default=None,
    help="Override deployment mode ('local' or 'ssh').",
)
@click.option(
    "--target-dir",
    default=None,
    help="Override the target custom_components directory path.",
)
@click.option("--ssh-host", default=None, help="Remote Home Assistant SSH host.")
@click.option("--ssh-port", default=None, type=int, help="Remote Home Assistant SSH port.")
@click.option("--ssh-user", default=None, help="Remote Home Assistant SSH username.")
@click.option("--ssh-key", default=None, help="Path to SSH private key file.")
@click.option("--ssh-password", default=None, help="SSH password.")
@click.option("--ssh-passphrase", default=None, help="Passphrase for an encrypted SSH private key.")
@click.option("--ha-url", default=None, help="Home Assistant base URL (e.g. http://homeassistant.local:8123).")
@click.option("--api-key", default=None, help="Home Assistant Long-Lived Access Token for restarting.")
@click.option("--github-token", default=None, help="GitHub Personal Access Token for higher API rate limits.")
@click.option("--force", is_flag=True, default=False, help="Force re-download and overwrite of all components.")
@click.option(
    "--prune/--no-prune",
    default=None,
    help="Remove previously managed components that are no longer in the YAML file.",
)
@click.option(
    "--backup/--no-backup",
    default=None,
    help="Back up existing custom component directories before updating or pruning.",
)
@click.option(
    "--backup-dir",
    default=None,
    help="Directory to store local component backups (default: ./backups).",
)
@click.option("--dry-run", is_flag=True, default=False, help="Simulate sync without writing files or restarting.")
@click.option(
    "--restart/--no-restart",
    "restart_on_change",
    default=True,
    show_default=True,
    help="Restart Home Assistant when components change.",
)
@click.option(
    "--always-restart",
    is_flag=True,
    default=False,
    help="Restart Home Assistant even if no components changed.",
)
def sync_cmd(
    config_path: str,
    mode: Optional[str],
    target_dir: Optional[str],
    ssh_host: Optional[str],
    ssh_port: Optional[int],
    ssh_user: Optional[str],
    ssh_key: Optional[str],
    ssh_password: Optional[str],
    ssh_passphrase: Optional[str],
    ha_url: Optional[str],
    api_key: Optional[str],
    github_token: Optional[str],
    force: bool,
    prune: Optional[bool],
    backup: Optional[bool],
    backup_dir: Optional[str],
    dry_run: bool,
    restart_on_change: bool,
    always_restart: bool,
) -> None:
    """Download and sync custom components to Home Assistant."""
    try:
        app_config = load_config(config_path)
        app_config = _apply_cli_overrides(
            app_config,
            mode=mode,
            target_dir=target_dir,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            ssh_key=ssh_key,
            ssh_password=ssh_password,
            ssh_passphrase=ssh_passphrase,
            ha_url=ha_url,
            api_key=api_key,
            github_token=github_token,
            backup_dir=backup_dir,
        )

        target_desc = (
            f"SSH ({app_config.homeassistant.ssh.username}@{app_config.homeassistant.ssh.host}:"
            f"{app_config.homeassistant.custom_components_dir})"
            if app_config.homeassistant.mode == TargetMode.SSH and app_config.homeassistant.ssh
            else f"Local ({app_config.homeassistant.custom_components_dir})"
        )
        click.echo(f"Target: {target_desc}")

        manager = ComponentManager(app_config, logger=click.echo)
        report = manager.sync(
            force=force,
            prune=prune,
            backup=backup,
            dry_run=dry_run,
            restart_on_change=restart_on_change,
            always_restart=always_restart,
        )

        installed_count = sum(1 for a in report.actions if a.action == "installed")
        updated_count = sum(1 for a in report.actions if a.action == "updated")
        uptodate_count = sum(1 for a in report.actions if a.action == "up_to_date")
        pruned_count = sum(1 for a in report.actions if a.action == "pruned")

        click.echo(
            f"\nSync summary: {installed_count} installed, {updated_count} updated, "
            f"{uptodate_count} up-to-date, {pruned_count} pruned."
        )

        if report.restart is not None:
            if report.restart.manual_restart_required:
                click.secho(f"\nNOTICE: {report.restart.message}", fg="yellow", bold=True)
            elif report.restart.restarted:
                click.secho(f"\n{report.restart.message}", fg="green", bold=True)
            else:
                click.echo(f"\n{report.restart.message}")

    except (ConfigError, GitHubError, TargetError, HomeAssistantAPIError) as exc:
        click.secho(f"Error: {exc}", fg="red", err=True)
        sys.exit(1)


@cli.command("status")
@click.option(
    "-c",
    "--config",
    "config_path",
    default="components.yaml",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Path to the YAML configuration file.",
)
@click.option(
    "--mode",
    type=click.Choice(["local", "ssh"], case_sensitive=False),
    default=None,
    help="Override deployment mode ('local' or 'ssh').",
)
@click.option("--target-dir", default=None, help="Override the target custom_components directory path.")
@click.option("--ssh-host", default=None, help="Remote Home Assistant SSH host.")
@click.option("--ssh-port", default=None, type=int, help="Remote Home Assistant SSH port.")
@click.option("--ssh-user", default=None, help="Remote Home Assistant SSH username.")
@click.option("--ssh-key", default=None, help="Path to SSH private key file.")
@click.option("--ssh-password", default=None, help="SSH password.")
@click.option("--ssh-passphrase", default=None, help="Passphrase for an encrypted SSH private key.")
@click.option("--github-token", default=None, help="GitHub Personal Access Token.")
def status_cmd(
    config_path: str,
    mode: Optional[str],
    target_dir: Optional[str],
    ssh_host: Optional[str],
    ssh_port: Optional[int],
    ssh_user: Optional[str],
    ssh_key: Optional[str],
    ssh_password: Optional[str],
    ssh_passphrase: Optional[str],
    github_token: Optional[str],
) -> None:
    """Check installed versions vs latest GitHub refs for configured components."""
    try:
        app_config = load_config(config_path)
        app_config = _apply_cli_overrides(
            app_config,
            mode=mode,
            target_dir=target_dir,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            ssh_key=ssh_key,
            ssh_password=ssh_password,
            ssh_passphrase=ssh_passphrase,
            github_token=github_token,
        )
        manager = ComponentManager(app_config)
        rows = manager.status()

        for row in rows:
            installed = row["installed_version"] or "not installed"
            latest = row["latest_version"]
            domains_str = ", ".join(row["domains"]) if row["domains"] else "-"
            click.echo(
                f"{row['name']} ({row['repo']}) [{row['ref_type']}:{row['requested_ref']}] -> "
                f"status={row['status']} | installed={installed} | remote={latest} | domains={domains_str}"
            )
    except (ConfigError, GitHubError, TargetError) as exc:
        click.secho(f"Error: {exc}", fg="red", err=True)
        sys.exit(1)


@cli.command("validate")
@click.option(
    "-c",
    "--config",
    "config_path",
    default="components.yaml",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Path to the YAML configuration file.",
)
def validate_cmd(config_path: str) -> None:
    """Validate the YAML configuration file."""
    try:
        app_config = load_config(config_path)
        click.secho(
            f"Configuration '{config_path}' is valid ({len(app_config.components)} component(s), "
            f"mode={app_config.homeassistant.mode.value}).",
            fg="green",
        )
    except ConfigError as exc:
        click.secho(f"Validation failed: {exc}", fg="red", err=True)
        sys.exit(1)


@cli.command("restart")
@click.option(
    "-c",
    "--config",
    "config_path",
    default="components.yaml",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Path to the YAML configuration file.",
)
@click.option("--ha-url", default=None, help="Home Assistant base URL (e.g. http://homeassistant.local:8123).")
@click.option("--api-key", default=None, help="Home Assistant Long-Lived Access Token.")
def restart_cmd(
    config_path: str,
    ha_url: Optional[str],
    api_key: Optional[str],
) -> None:
    """Restart Home Assistant via REST API (or show manual restart notice if no API key)."""
    try:
        if Path(config_path).exists():
            app_config = load_config(config_path)
            ha_config = app_config.homeassistant
        else:
            from ha_ccm.models import HomeAssistantConfig
            ha_config = HomeAssistantConfig()

        if ha_url is not None:
            ha_config.url = ha_url.rstrip("/")
        if api_key is not None:
            ha_config.api_key = api_key.strip() or None

        outcome = restart_home_assistant(ha_config)
        if outcome.manual_restart_required:
            click.secho(f"NOTICE: {outcome.message}", fg="yellow", bold=True)
        else:
            click.secho(outcome.message, fg="green", bold=True)
    except (ConfigError, HomeAssistantAPIError) as exc:
        click.secho(f"Error: {exc}", fg="red", err=True)
        sys.exit(1)


@cli.command("backups")
@click.argument("domain", required=False)
@click.option(
    "-c",
    "--config",
    "config_path",
    default="components.yaml",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Path to the YAML configuration file.",
)
@click.option("--backup-dir", default=None, help="Directory where component backups are stored.")
def backups_cmd(
    domain: Optional[str],
    config_path: str,
    backup_dir: Optional[str],
) -> None:
    """List saved component backups available for revert."""
    try:
        app_config = load_config(config_path)
        app_config = _apply_cli_overrides(app_config, backup_dir=backup_dir)
        manager = ComponentManager(app_config)
        items = manager.list_backups(domain=domain)
        if not items:
            click.echo("No backups found.")
            return
        for item in items:
            click.echo(
                f"{item['domain']} | backup_id={item['backup_id']} | "
                f"version={item['version_label']} | path={item['path']}"
            )
    except (ConfigError, TargetError) as exc:
        click.secho(f"Error: {exc}", fg="red", err=True)
        sys.exit(1)


@cli.command("revert")
@click.argument("domain")
@click.option(
    "--backup-id",
    default=None,
    help="Specific backup ID folder name to revert to (defaults to the most recent backup).",
)
@click.option(
    "-c",
    "--config",
    "config_path",
    default="components.yaml",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Path to the YAML configuration file.",
)
@click.option(
    "--mode",
    type=click.Choice(["local", "ssh"], case_sensitive=False),
    default=None,
    help="Override deployment mode ('local' or 'ssh').",
)
@click.option("--target-dir", default=None, help="Override the target custom_components directory path.")
@click.option("--ssh-host", default=None, help="Remote Home Assistant SSH host.")
@click.option("--ssh-port", default=None, type=int, help="Remote Home Assistant SSH port.")
@click.option("--ssh-user", default=None, help="Remote Home Assistant SSH username.")
@click.option("--ssh-key", default=None, help="Path to SSH private key file.")
@click.option("--ssh-password", default=None, help="SSH password.")
@click.option("--ssh-passphrase", default=None, help="Passphrase for an encrypted SSH private key.")
@click.option("--ha-url", default=None, help="Home Assistant base URL (e.g. http://homeassistant.local:8123).")
@click.option("--api-key", default=None, help="Home Assistant Long-Lived Access Token for restarting.")
@click.option("--backup-dir", default=None, help="Directory where component backups are stored.")
@click.option(
    "--restart/--no-restart",
    "restart_on_change",
    default=True,
    show_default=True,
    help="Restart Home Assistant after reverting.",
)
def revert_cmd(
    domain: str,
    backup_id: Optional[str],
    config_path: str,
    mode: Optional[str],
    target_dir: Optional[str],
    ssh_host: Optional[str],
    ssh_port: Optional[int],
    ssh_user: Optional[str],
    ssh_key: Optional[str],
    ssh_password: Optional[str],
    ssh_passphrase: Optional[str],
    ha_url: Optional[str],
    api_key: Optional[str],
    backup_dir: Optional[str],
    restart_on_change: bool,
) -> None:
    """Revert a custom component domain to a saved backup."""
    try:
        app_config = load_config(config_path)
        app_config = _apply_cli_overrides(
            app_config,
            mode=mode,
            target_dir=target_dir,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            ssh_key=ssh_key,
            ssh_password=ssh_password,
            ssh_passphrase=ssh_passphrase,
            ha_url=ha_url,
            api_key=api_key,
            backup_dir=backup_dir,
        )
        manager = ComponentManager(app_config, logger=click.echo)
        report = manager.revert(
            domain=domain,
            backup_id=backup_id,
            restart_on_change=restart_on_change,
        )
        click.secho(f"\nSuccessfully reverted '{domain}'.", fg="green", bold=True)
        if report.restart is not None:
            if report.restart.manual_restart_required:
                click.secho(f"\nNOTICE: {report.restart.message}", fg="yellow", bold=True)
            elif report.restart.restarted:
                click.secho(f"\n{report.restart.message}", fg="green", bold=True)
            else:
                click.echo(f"\n{report.restart.message}")
    except (ConfigError, TargetError, HomeAssistantAPIError) as exc:
        click.secho(f"Error: {exc}", fg="red", err=True)
        sys.exit(1)


def main() -> None:
    """Entry point for the ha-ccm CLI."""
    cli()


if __name__ == "__main__":
    main()
