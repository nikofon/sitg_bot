import re
from urllib.parse import urlencode, urlsplit, urlunsplit

from sitg_bot.services.launch_references import LaunchReference

_PLAYER_PROFILE_ROUTE = re.compile(r"players/[0-9a-fA-F-]{36}")


def mini_app_route_url(base_url: str, route: str, *, query: dict[str, str] | None = None) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Mini App base URL must be HTTPS")
    normalized_route = route.strip("/")
    if normalized_route not in {"tournaments", "history", "library", "admin/suspicion"} and (
        _PLAYER_PROFILE_ROUTE.fullmatch(normalized_route) is None
    ):
        raise ValueError("Unsupported Mini App route")
    path = f"{parsed.path.rstrip('/')}/{normalized_route}"
    # A stable transition URL bypasses pre-cache-policy HTML while permitting reuse.
    launch_query = {**(query or {}), "_launch": "1"}
    return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(launch_query), ""))


def mini_app_launch_url(base_url: str, route: str, reference: LaunchReference) -> str:
    """Build a Mini App URL containing only the service-issued opaque launch reference."""

    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Mini App base URL must be HTTPS")
    normalized_route = route.strip("/")
    if normalized_route not in {
        "lobbies",
        "reports",
        "manager/tournaments",
        "manager/tournament-management",
        "manager/packets",
    }:
        raise ValueError("Unsupported Mini App launch route")
    if normalized_route == "manager/tournaments":
        path = f"{parsed.path.rstrip('/')}/manager/tournaments/{reference.value}/settings"
    elif normalized_route == "manager/tournament-management":
        path = f"{parsed.path.rstrip('/')}/manager/tournaments/{reference.value}/management"
    elif normalized_route == "manager/packets":
        path = f"{parsed.path.rstrip('/')}/manager/packets/{reference.value}/edit"
    else:
        path = f"{parsed.path.rstrip('/')}/{normalized_route}/{reference.value}"
    query = urlencode({"tgWebAppStartParam": reference.telegram_payload})
    return urlunsplit((parsed.scheme, parsed.netloc, path, query, ""))


def website_url(base_url: str, *segments: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Website base URL must be HTTPS")
    path = "/".join((parsed.path.rstrip("/"), *(segment.strip("/") for segment in segments)))
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
