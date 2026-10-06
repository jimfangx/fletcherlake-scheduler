"""Create an explicit, confirmed mock cluster in a disposable development directory."""

import argparse
from pathlib import Path

import yaml
from fl_agent.configuration import confirm_config, write_config
from fl_common.models import ClusterConfig


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, default=Path("/tmp/fl-demo"))
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("cluster.yaml"))
    args = parser.parse_args()
    if (args.state_root / "agent.db").exists():
        parser.error(
            "Choose a fresh state directory; existing durable history will not be overwritten"
        )
    config = ClusterConfig.model_validate(yaml.safe_load(args.config.read_text()))
    if any(board.backend != "mock" for board in config.boards if board is not None):
        parser.error("This utility accepts only explicit mock boards")
    write_config(args.state_root, config)
    confirm_config(args.state_root)
    print(f"Mock configuration initialized in {args.state_root}")


if __name__ == "__main__":
    main()
