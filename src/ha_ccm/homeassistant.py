"""Home Assistant REST API client for restarting Home Assistant."""

from __future__ import annotations

from typing import Optional

import requests

from ha_ccm.models import HomeAssistantConfig, RestartOutcome

MANUAL_RESTART_MESSAGE = (
    "No Home Assistant API key was supplied. "
    "You will need to manually restart Home Assistant for custom component changes to take effect."
)


class HomeAssistantAPIError(RuntimeError):
    """Raised when calling the Home Assistant REST API fails."""


def restart_home_assistant(
    ha_config: HomeAssistantConfig,
    session: Optional[requests.Session] = None,
    timeout: int = 20,
) -> RestartOutcome:
    """Trigger a Home Assistant restart via REST API if an API key is configured.

    If no API key is supplied, returns a RestartOutcome indicating that the user
    must manually restart Home Assistant.
    """
    api_key = (ha_config.api_key or "").strip()
    if not api_key:
        return RestartOutcome(
            attempted=False,
            restarted=False,
            manual_restart_required=True,
            message=MANUAL_RESTART_MESSAGE,
        )

    base_url = (ha_config.url or "http://localhost:8123").rstrip("/")
    endpoint = f"{base_url}/api/services/homeassistant/restart"
    http = session or requests.Session()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        resp = http.post(
            endpoint,
            headers=headers,
            json={},
            timeout=timeout,
            verify=ha_config.verify_ssl,
        )
    except requests.RequestException as exc:
        raise HomeAssistantAPIError(
            f"Failed to connect to Home Assistant API at '{endpoint}' to restart: {exc}. "
            "Please manually restart Home Assistant."
        ) from exc

    if resp.status_code in (200, 201):
        return RestartOutcome(
            attempted=True,
            restarted=True,
            manual_restart_required=False,
            message=f"Successfully triggered Home Assistant restart via {base_url}.",
        )

    if resp.status_code in (401, 403):
        raise HomeAssistantAPIError(
            f"Home Assistant API rejected the supplied API key (HTTP {resp.status_code}). "
            "Please verify your Long-Lived Access Token or manually restart Home Assistant."
        )

    raise HomeAssistantAPIError(
        f"Home Assistant restart API call failed with HTTP {resp.status_code}: {resp.text[:200]}. "
        "Please manually restart Home Assistant."
    )
