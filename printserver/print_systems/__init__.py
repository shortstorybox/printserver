from functools import cached_property
from .base import PrintSystem
from .brother_ql import BrotherQLPrintSystem
from .cups import CupsPrintSystem

__all__ = ["all_print_systems", "PrintSystemProvider"]

all_print_systems = [
    # Sorted from most-preferred to least-preferred printer system
    CupsPrintSystem,
    BrotherQLPrintSystem,
]


class PrintSystemProvider:
    @cached_property
    def supported_systems(self) -> list[PrintSystem]:
        """Return the list of all print systems that are supported on this machine"""
        return [system() for system in all_print_systems if system.is_supported()]
