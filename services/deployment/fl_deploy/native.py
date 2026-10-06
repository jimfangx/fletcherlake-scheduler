"""Quote literal paths for native parsers; systemd specifiers are escaped too."""

import re


def substitute(source: str, replacements: dict[str, str]) -> str:
    """Replace template tokens once, without rewriting inserted values."""
    pattern = "|".join(re.escape(key) for key in sorted(replacements, key=len, reverse=True))
    return re.sub(pattern, lambda match: replacements[match.group()], source)


def quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def systemd(value: str) -> str:
    return quoted(value.replace("%", "%%"))
