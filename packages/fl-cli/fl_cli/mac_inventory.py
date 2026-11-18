"""Small terminal wizard used by ship/18-bringup-mac.sh on the destination Mac."""

import os
import re
import shutil
import socket
import sys
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from fl_common.models import BoardConfig
from serial.tools import list_ports

Reader = Callable[[str], str]


@dataclass(frozen=True)
class Port:
    device: str
    serial_number: str | None = None
    vid: int | None = None
    pid: int | None = None


def ask[T](reader: Reader, label: str, parse: Callable[[str], T], default: str = "") -> T:
    while True:
        answer = reader(f"{label}{f' [{default}]' if default else ''}: ").strip() or default
        try:
            return parse(answer)
        except ValueError as error:
            print(error)


def required(value: str) -> str:
    if not value:
        raise ValueError("Enter a value.")
    return value


def board_count(value: str) -> int:
    if value not in {"1", "2", "3"}:
        raise ValueError("Enter 1, 2 or 3 boards.")
    return int(value)


def backend_name(value: str) -> str:
    if value.lower() not in {"lilikoi", "sophialake", "mock"}:
        raise ValueError("Choose lilikoi (Fletcher Lake), sophialake or mock.")
    return value.lower()


def rail_limits(value: str) -> str:
    if not value:
        return ""
    items = [item.strip() for item in value.split(",")]
    if len(items) != 3:
        raise ValueError("Enter three MIN-MAX ranges in mV, or leave blank.")
    for item in items:
        match = re.fullmatch(r"([0-9]+)-([0-9]+)", item)
        if not match or not 0 < int(match[1]) <= int(match[2]):
            raise ValueError("Each range must have positive MIN <= MAX in mV.")
    return ",".join(items)


def select_device(label: str, candidates: Sequence[str], reader: Reader) -> str:
    candidates = sorted(set(candidates))
    if len(candidates) == 1:
        print(f"Detected {label}: {candidates[0]}")
        return candidates[0]
    for index, value in enumerate(candidates, 1):
        print(f"  {index}. {value}")

    def selected(value: str) -> str:
        required(value)
        if value.isdecimal() and 1 <= int(value) <= len(candidates):
            return candidates[int(value) - 1]
        if value.isdecimal() and candidates:
            raise ValueError("Choose a listed number or enter the device identifier/path.")
        return value

    return ask(reader, f"{label} (number or identifier/path)" if candidates else label, selected)


def build_overrides(ports: Sequence[Port], reader: Reader, host_id: str) -> dict[str, object]:
    print("Using attached USB devices. For multiple boards, pair each MCU with its FPGA UART-TSI.")
    count = ask(reader, "Number of boards", board_count, "1")
    boards: list[dict[str, object]] = []
    used_mcus: set[str] = set()
    used_ttys: set[str] = set()
    used_jtag: set[str] = set()
    for index in range(count):
        print(f"\nBoard {index + 1}")
        backend = ask(
            reader,
            "Board type: lilikoi (Fletcher Lake), sophialake or mock",
            backend_name,
            "lilikoi",
        )
        board: dict[str, object] = {
            "board_id": f"{host_id}-{index}",
            "backend": backend,
            "firmware_timeout_seconds": 7200,
        }
        if backend == "lilikoi":
            # Only use RP USB CDC devices for the MCU and the FPGA FT2232H channel A
            # (0403:6010) for TSI. The SoC debug FTDI (0403:6011) is a different port.
            mcus = [p.serial_number or p.device for p in ports if p.vid == 0x2E8A]
            mcu = select_device(
                "RP2350 MCU serial", [m for m in mcus if m not in used_mcus], reader
            )
            tsi_ports = [
                p
                for p in ports
                if p.vid == 0x0403
                and p.pid == 0x6010
                and p.device.endswith("0")
                and p.device not in used_ttys
                and p.serial_number not in used_jtag
            ]
            tty = select_device("FPGA UART-TSI port", [p.device for p in tsi_ports], reader)
            paired = next((p for p in tsi_ports if p.device == tty), None)
            jtag = paired.serial_number if paired else None
            if jtag:
                print(f"Detected FPGA JTAG serial: {jtag}")
            else:
                jtag = ask(reader, "FPGA JTAG USB serial", required)
            if mcu in used_mcus or tty in used_ttys or jtag in used_jtag:
                raise ValueError("Each board needs its own MCU, UART-TSI and JTAG devices.")
            used_mcus.add(mcu)
            used_ttys.add(tty)
            used_jtag.add(jtag)
            mapping = {
                "mcu": mcu,
                "tsi_tty": tty,
                "jtag_serial": jtag,
                "openocd": shutil.which("openocd") or "openocd",
                "openocd_cfg": "/opt/fl/packages/lilikoi/host/openocd/fletcherlake.cfg",
                "uart_tsi": "/opt/fl/packages/lilikoi/host/uart_tsi/uart_tsi",
                "run_dir": "/Library/Application Support/fl/lilikoi",
            }
            soc = ask(reader, "Installed chip/SoC name (job matching, e.g. penguin26)", required)
            limits = ask(
                reader,
                "Chip-owner voltage limits for VCORE0/1/2 in mV "
                "(MIN-MAX,MIN-MAX,MIN-MAX; blank disables voltage changes)",
                rail_limits,
            )
            if limits:
                mapping["rail_limits"] = limits
            board.update(
                device_mapping=mapping,
                num_vrails=3,
                num_vsense=8,
                num_isense=4,
                clock_source="fpga",
                fpgas=[{"model": "xc7k160t"}],
                socs=[{"name": soc}],
            )
            # Keep the MCU's current regulator routing: no core_sel override is invented.
        elif backend == "sophialake":
            print(
                "SophiaLake is an implementation template; its board will remain ERROR "
                "until hardware functions and device mappings are implemented."
            )
        BoardConfig.model_validate(board)
        boards.append(board)
        print(f"Board ID for job selection: {board['board_id']}")
    return {"boards": boards, "environment": {"rclone": {"path": "/opt/fl-tools/rclone"}}}


def write_overrides(target: Path, data: dict[str, object]) -> None:
    """Atomically replace only a fully constructed, validated inventory draft."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            yaml.safe_dump(data, output, sort_keys=False)
            output.flush()
            os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def main() -> None:
    target = Path(sys.argv[1]).expanduser()
    try:
        if target.exists():
            answer = input(f"Reuse existing {target}? [Y/n]: ").strip().lower()
            if answer in {"", "y", "yes"}:
                return
            if answer not in {"n", "no"}:
                raise ValueError("Answer y or n; the existing file has not been changed.")
        ports = [
            Port(p.device, p.serial_number, p.vid, p.pid)
            for p in list_ports.comports()
            if p.device.startswith("/dev/cu.")
        ]
        host = re.sub(r"[^A-Za-z0-9_.-]", "-", socket.gethostname().split(".")[0])[:40].strip("._-")
        host_id = f"{host or 'mac'}-{uuid.getnode():012x}"
        data = build_overrides(ports, input, host_id)
        write_overrides(target, data)
        print(f"\nSaved {target}; host facts will be detected during enrollment.")
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("Configuration cancelled; no new configuration was written.") from None
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
