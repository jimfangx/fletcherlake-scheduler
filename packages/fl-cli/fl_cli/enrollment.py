"""Compatibility exports; enrollment is shared library code without terminal behavior."""

from fl.macos.enrollment import MacEnrollment, SetupReceipt, checked, unregister

__all__ = ["MacEnrollment", "SetupReceipt", "checked", "unregister"]
