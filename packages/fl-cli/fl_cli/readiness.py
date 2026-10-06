"""Compatibility export for the shared daemon startup gate."""

from fl.macos.readiness import wait_ready

__all__ = ["wait_ready"]
