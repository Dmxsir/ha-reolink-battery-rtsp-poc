"""Automatic go2rtc bridge provisioning for the Argus live source."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientTimeout

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .http_stream import HTTP_AAC_PATH, HTTP_H264_PATH

GO2RTC_STREAM_NAME = "argus2e_poc"
GO2RTC_API_PORT = 1984
GO2RTC_RTSP_PORT = 8554


@dataclass(slots=True)
class Go2RtcBridgeState:
    """Secret-safe state of the best-effort go2rtc bridge setup."""

    attempted: bool = False
    success: bool = False
    stream_name: str = GO2RTC_STREAM_NAME
    api_url: str | None = None
    rtsp_url: str | None = None
    http_status: int | None = None
    failure_type: str | None = None
    sources_registered: int = 0


def _host_for_url(hostname: str) -> str:
    """Return a hostname suitable for embedding in a URL."""
    return f"[{hostname}]" if ":" in hostname else hostname


def _derive_urls(hass: HomeAssistant) -> tuple[str, str, str]:
    """Return HA base URL, go2rtc API base URL and RTSP stream URL."""
    instance_url = get_url(
        hass,
        allow_internal=True,
        allow_external=False,
        allow_cloud=False,
        allow_ip=True,
        prefer_external=False,
    ).rstrip("/")
    parsed = urlsplit(instance_url)
    if not parsed.hostname:
        raise ValueError("Home Assistant internal URL has no hostname")

    host = _host_for_url(parsed.hostname)
    api_url = f"http://{host}:{GO2RTC_API_PORT}"
    rtsp_url = (
        f"rtsp://{host}:{GO2RTC_RTSP_PORT}/{GO2RTC_STREAM_NAME}"
        "?video=h264&audio=aac"
    )
    return instance_url, api_url, rtsp_url


async def async_ensure_go2rtc_bridge(hass: HomeAssistant) -> Go2RtcBridgeState:
    """Best-effort create/update the persistent go2rtc stream configuration.

    The stream carries three tracks/sources:
    - raw H264 for zero-copy video;
    - raw AAC/ADTS for RTSP/HLS/recording consumers;
    - AAC transcoded to Opus only for WebRTC consumers.

    Failure is intentionally non-fatal because an existing/manual go2rtc stream
    can still be used by the camera entity.
    """
    state = Go2RtcBridgeState(attempted=True)

    try:
        ha_base, api_url, rtsp_url = _derive_urls(hass)
    except (NoURLAvailableError, ValueError) as err:
        state.failure_type = type(err).__name__
        return state

    state.api_url = api_url
    state.rtsp_url = rtsp_url

    h264_source = f"{ha_base}{HTTP_H264_PATH}"
    aac_source = f"{ha_base}{HTTP_AAC_PATH}"
    opus_source = f"ffmpeg:{aac_source}#audio=opus"
    sources = (h264_source, aac_source, opus_source)

    params: list[tuple[str, str]] = [("name", GO2RTC_STREAM_NAME)]
    params.extend(("src", source) for source in sources)

    try:
        session = async_get_clientsession(hass)
        async with session.put(
            f"{api_url}/api/streams",
            params=params,
            timeout=ClientTimeout(total=8),
        ) as response:
            state.http_status = response.status
            if 200 <= response.status < 300:
                state.success = True
                state.sources_registered = len(sources)
            else:
                state.failure_type = f"HTTP_{response.status}"
    except (ClientError, TimeoutError, OSError) as err:
        state.failure_type = type(err).__name__

    return state
