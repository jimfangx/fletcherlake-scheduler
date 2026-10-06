"""Compatibility exports for the shared macOS private-network client."""

from fl.macos.networking import CommandRunner, TailscaleClient, run_command

__all__ = ["CommandRunner", "TailscaleClient", "run_command"]
