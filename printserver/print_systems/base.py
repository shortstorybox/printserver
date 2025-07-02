from abc import ABC, abstractmethod
from enum import Enum
from dataclasses import dataclass

from falcon import HTTPBadRequest
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
class PrintOption:
    keyword: str
    default_choice: Optional[str]
    choices: list[str]
    display_name: str


class SizeUnit(Enum):
    POINTS = "points"
    INCHES = "inches"
    MILLIMETERS = "mm"


@dataclass
class MediaSize:
    name: str
    width: float
    height: float
    units: SizeUnit
    full_identifier: str


@dataclass
class PrinterDetails:
    name: str
    model: str
    identifier: str
    printer_state: PrinterState
    state_reasons: list[str]
    print_system: str
    default_media_size: str  # this MUST match the name of one of the media_sizes
    media_sizes: list[MediaSize]
    supported_options: list[PrintOption]


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
            raise HTTPBadRequest(
                title=f"Unknown printerSelector field: {', '.join(selector.keys())}"
            )
        if result.name is not None and not isinstance(result.name, str):
            raise HTTPBadRequest(
                title=f"Invalid value for printerSelector[name]: {result.name}"
            )
        if not isinstance(result.name_prefix, str):
            raise HTTPBadRequest(
                title=f"Invalid value for printerSelector[namePrefix]: {result.name_prefix}"
            )
        if not isinstance(result.model_prefix, str):
            raise HTTPBadRequest(
                title=f"Invalid value for printerSelector[modelPrefix]: {result.model_prefix}"
            )
        PrinterSelector.validate_print_system(result.print_system)
        return result

    @staticmethod
    def validate_print_system(print_system) -> None:
        from printserver.print_systems import all_print_systems

        system_names = [x.system_name() for x in all_print_systems]
        if print_system and print_system not in system_names:
            raise HTTPBadRequest(
                title=f"Invalid value for printerSelector[printSystem]. Must be null, or one of {', '.join(system_names)}"
            )


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
        media_size: Optional[MediaSize],
        options: dict[str, str],
    ) -> PrintJob:
        raise NotImplementedError

    @abstractmethod
    def get_job(self, job_id: str) -> Optional[PrintJob]:
        raise NotImplementedError
