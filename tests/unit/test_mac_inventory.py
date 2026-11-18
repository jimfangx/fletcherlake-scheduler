from pathlib import Path

import pytest
import yaml
from fl_cli.mac_inventory import Port, build_overrides, main, write_overrides
from fl_common.models import BoardConfig


def answers(*values):
    remaining = iter(values)
    prompts = []

    def reader(prompt):
        prompts.append(prompt)
        return next(remaining)

    return reader, prompts


def test_single_board_detects_devices_and_does_not_use_soc_debug_port(tmp_path):
    reader, prompts = answers("", "", "penguin26", "")
    ports = [
        Port("/dev/cu.usbmodem1", "RP2350-serial", 0x2E8A, 0x000A),
        Port("/dev/cu.usbserial-FPGA0", "FPGA", 0x0403, 0x6010),
        Port("/dev/cu.usbserial-FPGA1", "FPGA", 0x0403, 0x6010),
        Port("/dev/cu.usbserial-SOC0", "SOC", 0x0403, 0x6011),
    ]
    data = build_overrides(ports, reader, "mac-unique")
    target = tmp_path / "cluster-overrides.yaml"
    write_overrides(target, data)
    saved = yaml.safe_load(target.read_text())
    board = BoardConfig.model_validate(saved["boards"][0])
    assert board.board_id == "mac-unique-0"
    assert board.device_mapping["mcu"] == "RP2350-serial"
    assert board.device_mapping["tsi_tty"] == "/dev/cu.usbserial-FPGA0"
    assert board.device_mapping["jtag_serial"] == "FPGA"
    assert "rail_limits" not in board.device_mapping
    assert "core_sel" not in board.device_mapping
    assert board.socs[0].name == "penguin26"
    assert board.fpgas[0].model == "xc7k160t"
    assert saved["environment"]["rclone"]["path"] == "/opt/fl-tools/rclone"
    assert "os" not in saved and "mac_address" not in saved
    assert target.stat().st_mode & 0o777 == 0o600
    assert len(prompts) == 4  # count/type defaults, chip name, optional voltage limits


def test_multiple_boards_require_pairing_and_reserve_each_device_once():
    reader, prompts = answers(
        "2", "", "2", "1", "chip-b", "650-900,650-900,650-900", "", "chip-a", ""
    )
    ports = [Port(f"/dev/cu.mcu{i}", f"MCU{i}", 0x2E8A) for i in (1, 2)]
    ports += [Port(f"/dev/cu.usbserial-FPGA{i}0", f"FPGA{i}", 0x0403, 0x6010) for i in (1, 2)]
    data = build_overrides(ports, reader, "mac")
    first, second = [BoardConfig.model_validate(b) for b in data["boards"]]
    assert first.device_mapping["mcu"] == "MCU2"
    assert first.device_mapping["jtag_serial"] == "FPGA1"
    assert second.device_mapping["mcu"] == "MCU1"
    assert second.device_mapping["jtag_serial"] == "FPGA2"
    assert first.device_mapping["rail_limits"] == "650-900,650-900,650-900"
    assert sum("number or identifier" in p for p in prompts) == 2


def test_missing_detection_accepts_manual_devices_and_reprompts_invalid_values():
    reader, _ = answers(
        "4",
        "1",
        "unknown",
        "lilikoi",
        "MCU",
        "/dev/cu.custom",
        "JTAG",
        "",
        "chip",
        "bad",
        "900-650,650-900,650-900",
        "",
    )
    data = build_overrides([], reader, "mac")
    board = BoardConfig.model_validate(data["boards"][0])
    assert board.device_mapping["jtag_serial"] == "JTAG"
    assert "rail_limits" not in board.device_mapping


@pytest.mark.parametrize("backend", ["mock", "sophialake"])
def test_other_backends_do_not_prompt_for_lilikoi_hardware(backend):
    reader, prompts = answers("1", backend)
    data = build_overrides([], reader, "mac")
    assert BoardConfig.model_validate(data["boards"][0]).backend == backend
    assert len(prompts) == 2


def test_cancel_keeps_existing_config_unchanged(tmp_path, monkeypatch):
    target = tmp_path / "cluster-overrides.yaml"
    original = "boards: [null]\n"
    target.write_text(original)
    responses = iter(["n"])

    def cancelled(prompt):
        try:
            return next(responses)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("sys.argv", ["mac_inventory", str(target)])
    monkeypatch.setattr("builtins.input", cancelled)
    monkeypatch.setattr("fl_cli.mac_inventory.list_ports.comports", lambda: [])
    with pytest.raises(SystemExit, match="cancelled"):
        main()
    assert target.read_text() == original
    assert list(tmp_path.iterdir()) == [target]


def test_reuse_does_not_probe_or_rewrite_existing_config(tmp_path, monkeypatch):
    target: Path = tmp_path / "cluster-overrides.yaml"
    target.write_text("boards: [null]\n")
    monkeypatch.setattr("sys.argv", ["mac_inventory", str(target)])
    monkeypatch.setattr("builtins.input", lambda prompt: "")

    def unexpected_probe():
        pytest.fail("Should not enumerate devices when reusing configuration")

    monkeypatch.setattr("fl_cli.mac_inventory.list_ports.comports", unexpected_probe)
    main()
    assert target.read_text() == "boards: [null]\n"


def test_fresh_cli_creates_enrollable_overrides_with_a_valid_generated_id(tmp_path, monkeypatch):
    target = tmp_path / "cluster-overrides.yaml"
    reader, _ = answers("", "", "penguin26", "")
    monkeypatch.setattr("sys.argv", ["mac_inventory", str(target)])
    monkeypatch.setattr("builtins.input", reader)
    monkeypatch.setattr("fl_cli.mac_inventory.socket.gethostname", lambda: "---.local")
    monkeypatch.setattr("fl_cli.mac_inventory.uuid.getnode", lambda: 0x1234)
    monkeypatch.setattr(
        "fl_cli.mac_inventory.list_ports.comports",
        lambda: [
            Port("/dev/cu.mcu", "MCU", 0x2E8A),
            Port("/dev/cu.usbserial-FPGA0", "FPGA", 0x0403, 0x6010),
        ],
    )
    main()
    from fl.macos.inventory import inventory

    config = inventory(
        target,
        detector=lambda: {
            "os": {"name": "Darwin", "release": "13.0"},
            "apple_model": "Macmini",
            "mac_address": "00:00:00:00:12:34",
        },
    )
    assert config.boards[0].board_id == "mac-000000001234-0"
    assert config.boards[0].socs[0].name == "penguin26"
