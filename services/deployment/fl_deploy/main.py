"""Linux deployment rendering from a reviewed non-secret YAML inventory."""

import argparse
from pathlib import Path

import yaml
from pydantic import ValidationError

from .render import load, render, write_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--templates", type=Path, default=Path("services"))
    args = parser.parse_args()
    try:
        manifest = load(args.manifest)
    except ValidationError as error:
        # A misplaced secret field must not be echoed into terminal/CI logs.
        issues = [
            f"{'.'.join(str(part) for part in item['loc'])}: {item['type']}"
            for item in error.errors(include_input=False, include_context=False, include_url=False)
        ]
        parser.error("Invalid deployment inventory: " + "; ".join(issues))
    except yaml.YAMLError:
        parser.error("Deployment inventory must be valid YAML")
    write_bundle(render(manifest, args.templates), args.output)
    print(f"Rendered review bundle: {args.output}")


if __name__ == "__main__":
    main()
