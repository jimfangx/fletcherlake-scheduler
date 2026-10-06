"""Human-readable sizes and times; displayed ages use the snapshot's clock."""

from datetime import datetime


def size(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{value} B"
        amount /= 1024
    raise AssertionError("Unreachable size unit")


def duration(seconds: float) -> str:
    days, remainder = divmod(max(0, int(seconds)), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds_int = divmod(remainder, 60)
    clock = f"{hours:02}:{minutes:02}:{seconds_int:02}"
    return f"{days}d {clock}" if days else clock


def timestamp(value: datetime | None) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z") if value else "—"


def expiry(value: datetime | None, now: datetime) -> str:
    if value is None:
        return "After completion"
    seconds = (value - now).total_seconds()
    return "Due" if seconds <= 0 else duration(seconds)
