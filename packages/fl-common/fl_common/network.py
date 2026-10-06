"""Shared validation for public service origins."""

from urllib.parse import urlsplit


def https_origin(value: str) -> str:
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
    ):
        raise ValueError("Service must be an HTTPS origin without credentials or a path")
    return value.rstrip("/")
