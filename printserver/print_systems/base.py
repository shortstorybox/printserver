from abc import ABC, abstractmethod
from enum import Enum
from dataclasses import dataclass

import falcon
from typing import Optional


@dataclass
class PrintFile:
    content_type: str
    content: bytes


class JobState(Enum):
    PENDING = 3
    PENDING_HELD = 4
    PROCESSING = 5
    STOPPED = 6
    CANCELED = 7
    ABORTED = 8
    COMPLETED = 9


class PrinterState(Enum):
    IDLE = 3
    PROCESSING = 4
    STOPPED = 5


@dataclass
class PrintJob:
    job_id: str
    job_state: JobState
    job_state_reasons: list[str]


@dataclass
class PrinterDetails:
    name: str
    model: str
    identifier: str
    printer_state: PrinterState
    state_reasons: list[str]
    print_system: str
    supported_options: dict[str, list[str]]
    default_options: dict[str, str]

    def get_warnings(self, user_supplied_options: dict[str, str]) -> list[str]:
        warnings = []
        for option, value in user_supplied_options.items():
            if option not in self.supported_options:
                warnings.append(f"Printer does not support option: {repr(option)}")
            elif value not in self.supported_options[option]:
                supported_repr = ", ".join(map(repr, self.supported_options[option]))
                warnings.append(
                    f"Printer does not support option {repr(option)}={repr(value)}'. "
                    f"Supported values are: {supported_repr}"
                )
        return warnings


@dataclass
class PrinterSelector:
    name: Optional[str] = None
    print_system: Optional[str] = None
    name_prefix: str = ""
    model_prefix: str = ""

    @staticmethod
    def parse(selector: dict) -> "PrinterSelector":
        """
        Parses a PrinterSelector in the format specified by the HTTP API.
        """
        selector = selector.copy()
        result = PrinterSelector(
            name=selector.pop("name", None) or None,
            name_prefix=selector.pop("namePrefix", "") or "",
            model_prefix=selector.pop("modelPrefix", "") or "",
            print_system=selector.get("printSystem", None) or None,
        )
        if selector:
            raise falcon.HTTPBadRequest(
                description=f"Unknown printerSelector field: {', '.join(selector.keys())}"
            )
        if result.name is not None and not isinstance(result.name, str):
            raise falcon.HTTPBadRequest(
                description=f"Invalid value for printerSelector.name: {result.name}"
            )
        if not isinstance(result.name_prefix, str):
            raise falcon.HTTPBadRequest(
                description=f"Invalid value for printerSelector.namePrefix: {result.name_prefix}"
            )
        if not isinstance(result.model_prefix, str):
            raise falcon.HTTPBadRequest(
                description=f"Invalid value for printerSelector.modelPrefix: {result.model_prefix}"
            )

        from printserver.print_systems import all_print_systems

        system_names = [x.system_name() for x in all_print_systems]

        if result.print_system and result.print_system not in system_names:
            raise falcon.HTTPBadRequest(
                description=f"Invalid value for printerSelector.printSystem. Must be null, or one of {', '.join(system_names)}"
            )
        return result


class PrintSystem(ABC):
    @classmethod
    @abstractmethod
    def system_name(cls) -> str:
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def is_supported(cls) -> bool:
        raise NotImplementedError

    @abstractmethod
    def get_printers(self, printer_selector: PrinterSelector) -> list[PrinterDetails]:
        raise NotImplementedError

    @abstractmethod
    def print(
        self,
        printer: PrinterDetails,
        files: list[PrintFile],
        job_title: str,
        is_async: bool,
        options: dict[str, str],
    ) -> PrintJob:
        raise NotImplementedError

    @abstractmethod
    def get_job(self, job_id: str) -> Optional[PrintJob]:
        raise NotImplementedError
