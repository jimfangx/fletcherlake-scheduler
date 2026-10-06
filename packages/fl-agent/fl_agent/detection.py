"""Best-effort macOS environment detection; explicit configuration takes precedence."""

import os
import platform
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from fl_common.models import EnvironmentConfig, OSInfo
from fl_common.models.cluster import ToolInfo


def output(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return (result.stdout or result.stderr).strip() or None


def detect_tool(name: str, executable: str, version_flag: str = "--version") -> ToolInfo | None:
    path = shutil.which(executable)
    if path is None and name == "vivado":
        for root in (Path("/opt/Xilinx/Vivado"), Path("/Applications/Xilinx/Vivado")):
            installed = sorted(root.glob("*/bin/vivado"), reverse=True)
            if installed:
                path = str(installed[0])
                break
    if path is None:
        return None
    version = output([path, version_flag])
    return ToolInfo(path=path, version=version.splitlines()[0] if version else None)


def detect_environment() -> EnvironmentConfig:
    return EnvironmentConfig(
        vivado=detect_tool("vivado", "vivado", "-version"),
        openocd=detect_tool("openocd", "openocd"),
        openfpgaloader=detect_tool("openfpgaloader", "openFPGALoader", "--Version"),
        riscv_toolchain=detect_tool("riscv_toolchain", "riscv64-unknown-elf-gcc"),
        gcc=detect_tool("gcc", "gcc"),
        clang=detect_tool("clang", "clang"),
        bbcp=detect_tool("bbcp", "bbcp", "-V"),
        chipyard=os.environ.get("CHIPYARD"),
    )


def detect_host() -> dict[str, Any]:
    system = platform.system()
    release = platform.mac_ver()[0] if system == "Darwin" else platform.release()
    build = output(["/usr/bin/sw_vers", "-buildVersion"]) if system == "Darwin" else None
    model = output(["/usr/sbin/sysctl", "-n", "hw.model"]) if system == "Darwin" else None
    # Prefer a real interface MAC on macOS; uuid.getnode can fall back to a random node.
    mac = None
    if system == "Darwin":
        interfaces = output(["/sbin/ifconfig", "en0"])
        if interfaces:
            for line in interfaces.splitlines():
                if line.strip().startswith("ether "):
                    mac = line.strip().split()[1]
                    break
    if mac is None:
        node = uuid.getnode()
        if not node & (1 << 40):
            mac = ":".join(f"{node:012x}"[index : index + 2] for index in range(0, 12, 2))
    return {
        "environment": detect_environment().model_dump(mode="json"),
        "os": OSInfo(name=system, release=release, build=build).model_dump(mode="json"),
        "apple_model": model,
        "mac_address": mac,
    }


def merge_overrides(detected: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Merge only explicitly supplied keys; nested tool overrides retain detected defaults."""
    result = dict(detected)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_overrides(result[key], value)
        else:
            result[key] = value
    return result
