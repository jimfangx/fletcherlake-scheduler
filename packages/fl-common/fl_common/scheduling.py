"""Resource matching and capped queue cost, independent from priority ordering."""

from collections.abc import Iterable
from dataclasses import dataclass

from fl_common.models import BoardConfig, ResourceConstraints


def matches(board: BoardConfig, request: ResourceConstraints) -> bool:
    return (
        (request.board is None or request.board == board.board_id)
        and (request.soc is None or request.soc in {soc.name for soc in board.socs})
        and (request.fpga is None or request.fpga in {fpga.model for fpga in board.fpgas})
    )


@dataclass(frozen=True)
class QueuePolicy:
    running_cap: float = 6 * 3600
    queued_cap: float = 2 * 3600
    count_weight: float = 10
    runtime_normalization: float = 3600

    def score(self, queued_estimates: Iterable[float], running_remaining: float = 0) -> float:
        estimates = list(queued_estimates)
        capped = min(max(0, running_remaining), self.running_cap)
        capped += sum(min(max(0, estimate), self.queued_cap) for estimate in estimates)
        return len(estimates) * self.count_weight + capped / self.runtime_normalization
