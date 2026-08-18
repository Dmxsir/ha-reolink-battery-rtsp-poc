"""Optional secret-safe diagnostics upload to GitHub.

The uploader writes only the already-sanitized PoC diagnostics document to the
repository's dedicated diagnostics branch. The GitHub token is read from the
config entry options and is never included in diagnostics or log messages.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_GITHUB_DIAGNOSTICS_ENABLED,
    CONF_GITHUB_TOKEN,
    GITHUB_API_VERSION,
    GITHUB_DIAGNOSTICS_BRANCH,
    GITHUB_DIAGNOSTICS_PATH,
    GITHUB_REPOSITORY,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class GitHubUploadState:
    attempted: bool = False
    success: bool = False
    http_status: int | None = None
    failure_type: str = ""
    last_attempt_time: str | None = None
    last_success_time: str | None = None


_STATES: dict[str, GitHubUploadState] = {}


def github_upload_state(entry_id: str) -> GitHubUploadState:
    return _STATES.setdefault(entry_id, GitHubUploadState())


def github_upload_configured(entry) -> bool:
    return bool(
        entry.options.get(CONF_GITHUB_DIAGNOSTICS_ENABLED, False)
        and entry.options.get(CONF_GITHUB_TOKEN)
    )


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "Home-Assistant-Reolink-Battery-RTSP-PoC",
    }


async def async_upload_diagnostics(hass, entry, diagnostics: dict[str, Any]) -> bool:
    """Upload sanitized diagnostics to diagnostics/latest.json.

    Upload failures are deliberately non-fatal to the camera probe. They are
    retained in local telemetry so the probe result is never masked by GitHub.
    """
    state = github_upload_state(entry.entry_id)
    state.attempted = True
    state.success = False
    state.http_status = None
    state.failure_type = ""
    state.last_attempt_time = datetime.now(UTC).isoformat()

    if not github_upload_configured(entry):
        state.failure_type = "NOT_CONFIGURED"
        return False

    token = str(entry.options[CONF_GITHUB_TOKEN])
    session = async_get_clientsession(hass)
    api_url = (
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/"
        f"{GITHUB_DIAGNOSTICS_PATH}"
    )
    headers = _headers(token)

    try:
        current_sha: str | None = None
        async with session.get(
            api_url,
            headers=headers,
            params={"ref": GITHUB_DIAGNOSTICS_BRANCH},
        ) as response:
            if response.status == 200:
                body = await response.json()
                sha = body.get("sha") if isinstance(body, dict) else None
                current_sha = sha if isinstance(sha, str) else None
            elif response.status != 404:
                state.http_status = response.status
                state.failure_type = "READ_FAILED"
                return False

        payload_document = {
            "uploaded_at": datetime.now(UTC).isoformat(),
            "source": "reolink_battery_rtsp_poc",
            "diagnostics": diagnostics,
        }
        raw = json.dumps(
            payload_document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        body: dict[str, Any] = {
            "message": "Update RTSP PoC live-probe diagnostics",
            "content": base64.b64encode(raw).decode("ascii"),
            "branch": GITHUB_DIAGNOSTICS_BRANCH,
        }
        if current_sha is not None:
            body["sha"] = current_sha

        async with session.put(api_url, headers=headers, json=body) as response:
            state.http_status = response.status
            if response.status not in (200, 201):
                state.failure_type = "WRITE_FAILED"
                return False

        state.success = True
        state.last_success_time = datetime.now(UTC).isoformat()
        return True
    except Exception as err:  # Network/API failure must never break the probe.
        state.failure_type = type(err).__name__
        _LOGGER.warning("GitHub diagnostics upload failed: %s", type(err).__name__)
        return False
    finally:
        token = ""
