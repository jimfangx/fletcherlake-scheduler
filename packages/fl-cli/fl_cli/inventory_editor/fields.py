"""Standard YAML fields and concise validation messages shared by inventory forms."""

from typing import Any

import yaml
from pydantic import ValidationError
from textual.widgets import TextArea


def yaml_text(value: Any) -> str:
    return str(yaml.safe_dump(value, sort_keys=False))


def device_names(board: dict[str, Any] | None, field: str, label: str) -> str:
    if board is None:
        return "—"
    values = board.get(field, [])
    if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
        return "Invalid definition (edit)"
    return ", ".join(str(item.get(label, "")) for item in values) or "—"


def yaml_field(name: str, value: Any) -> TextArea:
    return TextArea(yaml_text(value), id=name, soft_wrap=False, show_line_numbers=True)


def read_yaml(area: TextArea, *, empty: Any = None) -> Any:
    try:
        value = yaml.safe_load(area.text)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        line = f" at line {mark.line + 1}" if mark else ""
        raise ValueError(f"{area.id}: Invalid YAML{line}") from error
    return empty if value is None and not area.text.strip() else value


def validation_message(error: ValidationError | ValueError) -> str:
    if isinstance(error, ValidationError):
        return "\n".join(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors(include_url=False)[:5]
        )
    return str(error)
