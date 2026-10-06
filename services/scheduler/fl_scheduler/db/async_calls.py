"""Finish a database thread before propagating cancellation to its async caller."""

from fl_common.async_calls import background_call as database_call

__all__ = ["database_call"]
