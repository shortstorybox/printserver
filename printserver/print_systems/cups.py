import os
import os.path
import re
import time
from contextlib import ExitStack
from dataclasses import dataclass
from logging import getLogger
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import Optional

from cups import IPP_NOT_AUTHORIZED, IPP_NOT_FOUND, PPD, Connection, IPPError
from falcon import HTTPBadRequest, HTTPInternalServerError

from printserver.print_systems.base import (
    JobState,
    MediaSize,
    PrinterDetails,
    PrinterSelector,
    PrinterState,
    PrintFile,
    PrintJob,
    PrintOption,
    PrintSystem,
    SizeUnit,
)

logger = getLogger(__name__)


# CUPS-specific options that are not printer-specific
GENERIC_OPTIONS = [
    PrintOption(
        keyword="copies",
        display_name="Number of Copies",
        default_choice="1",
        choices=[str(x) for x in range(1, 101)],
    ),
    PrintOption(
        keyword="collate",
        display_name="Collate Copies",
        default_choice="false",
        choices=["true", "false"],
    ),
    PrintOption(  # Shorthand for print-scaling=fill
        keyword="fit-to-page",
        display_name="Scale to Fill Page",
        default_choice="false",
        choices=["true", "false"],
    ),
    PrintOption(
        keyword="mirror",
        display_name="Flip Horizontally",
        default_choice="false",
        choices=["true", "false"],
    ),
    PrintOption(
        keyword="landscape",
        display_name="Landscape",
        default_choice="false",
        choices=["true", "false"],
    ),
    PrintOption(
        keyword="outputorder",
        display_name="Sheet Order",
        default_choice="normal",
        choices=["normal", "reverse"],
    ),
    PrintOption(
        keyword="page-border",
        display_name="Border",
        default_choice="none",
        choices=["none", "single", "single-thick", "double", "double-thick"],
    ),
    PrintOption(
        keyword="number-up",
        display_name="Pages per Sheet",
        default_choice="1",
        choices=["1", "2", "4", "6", "9", "16"],
    ),
    PrintOption(
        keyword="number-up-layout",
        display_name="Layout Direction",
        default_choice="lrtb",
        choices=["lrtb", "btlr", "btrl", "lrbt", "rlbt", "rltb", "tblr", "tbrl"],
    ),
    PrintOption(
        keyword="print-scaling",
        display_name="Scale to Fit Paper Size",
        default_choice="none",
        choices=["auto", "auto-fit", "fill", "fit", "none"],
    ),
]

# CUPS-specific options that are disallowed for security
DISALLOWED_OPTIONS = {
    "job-priority",
    "job-hold-until",
    "job-cancel-after",
    "notify-lease-duration",
    "notify-events",
    "media",  # Use the top-level media.size param instead
    "PageSize",  # Deprecated PPD-only. Use top-level media.size param instead.
    "PageRegion",  # Deprecated PPD-only, even more out-of-date than PageSize.
    "document-format",  # Filled automatically
    "prettyprint",  # Deprecated, and only works for text-only files
    "orientation-requested",  # Use "landscape" option instead
}

# Access to the cups C library is serialized by a lock to prevent GIL errors
cups_lock = Lock()


@dataclass
class PPDCacheEntry:
    config_change_time: Optional[int]
    options: list[PrintOption]


class CupsPrintSystem(PrintSystem):
    def __init__(self):
        with cups_lock:
            self.conn = Connection()
        # Cache of PPD-derived options, keyed by printer name. The first tuple
        # value is the printer-config-change-time the options were parsed at.
        # See _ppd_options().
        self.ppd_options_cache: dict[str, PPDCacheEntry] = {}

    @classmethod
    def system_name(cls) -> str:
        return "cups"

    @classmethod
    def is_supported(cls) -> bool:
        """Check if CUPS is supported on this machine"""
        if os.name != "posix":
            return False
        try:
            with cups_lock:
                Connection().getPrinters()
        except IPPError:
            return False
        return True

    def get_printers(self, printer_selector: PrinterSelector) -> list[PrinterDetails]:
        """Return the list of available CUPS printers that match the given selector"""
        with cups_lock:
            printers = self.conn.getPrinters()
        results = []
        for printer_name, printer in printers.items():
            if "offline-report" in printer["printer-state-reasons"]:
                continue
            if PrinterState(printer["printer-state"]) not in [
                PrinterState.IDLE,
                PrinterState.PROCESSING,
            ]:
                if (
                    PrinterState(printer["printer-state"]) == PrinterState.STOPPED
                    and "paused" in printer["printer-state-reasons"]
                ):
                    # The only reason the printer is paused is because CUPS
                    # disabled it. This is usually recoverable, so we allow
                    # it to be used.
                    pass
                else:
                    continue

            if (
                printer_selector.name
                and printer["printer-info"].lower() != printer_selector.name.lower()
            ):
                continue

            if printer_selector.model_prefix and not printer[
                "printer-make-and-model"
            ].lower().startswith(printer_selector.model_prefix.lower()):
                continue

            if printer_selector.name_prefix and not printer[
                "printer-info"
            ].lower().startswith(printer_selector.name_prefix.lower()):
                continue

            supported_options = []

            # Parse IPP options
            with cups_lock:
                ipp_attributes = self.conn.getPrinterAttributes(printer_name)
            job_attributes = [
                key
                for key in ipp_attributes.get("job-creation-attributes-supported", [])
                if not key.endswith("-col")  # IPP collections are not supported
            ]
            for option_name in job_attributes:
                default_choice = None
                if option_name + "-default" in ipp_attributes:
                    default_choice = self.parse_ipp_attribute(
                        option_name, ipp_attributes[option_name + "-default"]
                    )
                choices = ipp_attributes.get(option_name + "-supported", [])
                if (
                    isinstance(choices, tuple)
                    and len(choices) == 2
                    and isinstance(choices[0], int)
                    and isinstance(choices[1], int)
                ):
                    # This is an integer-range option. To avoid overflowing the
                    # response, support a maximum of 100 choices.
                    max_value = min(choices[0] + 99, choices[1])
                    choices = [str(x) for x in range(choices[0], max_value + 1)]
                elif isinstance(choices, list):
                    pass
                else:
                    # We do not support tuple or singleton attribute specs,
                    # because pycups doesn't parse the required data
                    continue

                parsed_choices = [
                    self.parse_ipp_attribute(option_name, x) for x in choices
                ]
                if default_choice not in parsed_choices:
                    default_choice = None
                if not parsed_choices:
                    continue  # Skip options that are missing a spec
                display_name = re.sub(
                    r"([a-z])([A-Z])",
                    r"\1 \2",
                    option_name.replace("-", " ").replace("_", " "),
                ).title()
                supported_options.append(
                    PrintOption(
                        keyword=option_name,
                        default_choice=default_choice,
                        choices=parsed_choices,
                        display_name=display_name,
                    )
                )

            # Parse PPD options
            supported_options.extend(
                self._ppd_options(
                    printer_name, ipp_attributes.get("printer-config-change-time")
                )
            )

            supported_options.extend(GENERIC_OPTIONS)

            # Remove duplicates and disallowed options, while retaining the
            # same ordering. Only keep the first occurrence of each option.
            exclude = set(DISALLOWED_OPTIONS)
            supported_options_dedupe = []
            for option in supported_options:
                if option.keyword not in exclude:
                    exclude.add(option.keyword)
                    supported_options_dedupe.append(option)

            # Default media size
            default_media_identifier = ipp_attributes.get("media-default")
            default_media_size = None  # This value is determened futher below

            # Parse supported media sizes
            media_sizes = []
            media_identifiers = ipp_attributes.get("media-supported") or []
            size_names = set()
            for identifier in media_identifiers:
                if not re.match(r"^[^_]*_[^_]*_[^_]*$", identifier):
                    continue
                world_region, size_name, dimensions = identifier.split("_")
                dimensions = dimensions.lower()
                if dimensions.endswith("in"):
                    dimensions = dimensions[:-2]
                    units = SizeUnit.INCHES
                elif dimensions.lower().endswith("mm"):
                    dimensions = dimensions[:-2]
                    units = SizeUnit.MILLIMETERS
                else:
                    units = SizeUnit.POINTS
                if not re.match(r"^[0-9]*[.]?[0-9]+x[0-9]*[.]?[0-9]+$", dimensions):
                    continue
                width, height = dimensions.split("x")
                if identifier == default_media_identifier:
                    default_media_size = size_name
                if size_name in size_names:
                    continue  # Duplicate size
                media_sizes.append(
                    MediaSize(
                        name=size_name,
                        width=float(width),
                        height=float(height),
                        units=units,
                        full_identifier=identifier,
                    )
                )
            if len(media_sizes) == 0:
                # No valid media sizes found. To prevent unexpected behavior, we
                # fall back on making at least one size available.
                media_sizes = [
                    MediaSize(
                        name="letter",
                        width=8.5,
                        height=11,
                        units=SizeUnit.INCHES,
                        full_identifier="na_letter_8.5x11in",
                    )
                ]
            if not default_media_size:
                # No default media size found. Use the first one as a fallback
                # to prevent unexpected behavior.
                default_media_size = media_sizes[0].name

            results.append(
                PrinterDetails(
                    name=printer["printer-info"],
                    model=printer["printer-make-and-model"],
                    identifier=printer_name,
                    printer_state=PrinterState(printer["printer-state"]),
                    state_reasons=printer["printer-state-reasons"],
                    print_system=self.system_name(),
                    default_media_size=default_media_size,
                    media_sizes=media_sizes,
                    supported_options=supported_options_dedupe,
                )
            )

        return results

    def _ppd_options(
        self, printer_name: str, config_change_time: Optional[int]
    ) -> list[PrintOption]:
        """Return the options declared by a printer's PPD file.

        The result is cached per printer, and re-parsed only when CUPS reports a
        new printer-config-change-time. Caching is required for correctness, not
        just speed: pycups' PPD() opens the underlying /etc/cups/ppd/*.ppd file
        and never closes it, so parsing on every request leaks a file descriptor
        each time and eventually fails the whole process with EMFILE.
        """
        cached = self.ppd_options_cache.get(printer_name)
        if cached is not None and cached.config_change_time == config_change_time:
            return cached.options

        logger.info(
            "Retrieving PPD options for printer %s, time %d",
            printer_name,
            config_change_time,
        )

        # Get the PPD file for the printer
        try:
            with cups_lock:
                ppd_file = self.conn.getPPD(printer_name)
        except IPPError:
            logger.exception(
                "Failed to retrieve PPD file for printer %s, time %d",
                printer_name,
                config_change_time,
            )
            ppd_file = None

        # Parse the PPD file
        options: list[PrintOption] = []
        if ppd_file:
            try:
                try:
                    ppd = PPD(ppd_file)
                except RuntimeError:
                    logger.exception(
                        "Failed to parse PPD file for printer %s, time %d",
                        printer_name,
                        config_change_time,
                    )
                    ppd = None

                if ppd:
                    options = self.parse_options_from_ppd_file(ppd)
            finally:
                # getPPD() writes a temporary copy that the caller owns.
                try:
                    os.unlink(ppd_file)
                except OSError:
                    pass

        logger.info(
            "Found %d PPD options for printer %s, time %d",
            len(options),
            printer_name,
            config_change_time,
        )
        self.ppd_options_cache[printer_name] = PPDCacheEntry(
            config_change_time, options
        )

        return options

    @staticmethod
    def parse_options_from_ppd_file(ppd) -> list[PrintOption]:
        options: list[PrintOption] = []
        groups = list(ppd.optionGroups)
        for group in groups:
            # An options group can contain subgroups in addition to it's own options.
            # Traverse and parse all discovered groups.
            for subgroup in group.subgroups:
                groups.append(subgroup)

            # Discover the options in this group and add them to the list of options.
            for option in group.options:
                default_choice = option.defchoice or None
                choices = [x["choice"] for x in option.choices]
                if default_choice and default_choice not in choices:
                    choices = [default_choice] + choices
                options.append(
                    PrintOption(
                        keyword=option.keyword,
                        default_choice=default_choice,
                        choices=choices,
                        display_name=option.text,
                    )
                )
        return options

    @staticmethod
    def parse_ipp_attribute(option_name, value):
        """Convert pycups printer attributes to IPP-compatible strings."""
        if isinstance(value, tuple):
            # Handle resolution tuples (X, Y, 3) -> "XxYdpi"
            if "resolution" in option_name and len(value) == 3 and value[2] == 3:
                w, h, _ = value
                return f"{w}x{h}dpi"
            else:
                return " ".join(map(str, value))  # Generic tuple -> space-separated
        elif isinstance(value, list):
            # Convert list to comma-separated string (IPP expects this format)
            return ",".join(map(str, value))
        elif isinstance(value, bool):
            # Convert booleans to lowercase string values (IPP standard)
            return "true" if value else "false"
        elif isinstance(value, int):
            # Convert integers to strings (IPP uses string representations)
            return str(value)
        elif isinstance(value, str):
            # Strings are already in the correct format
            return value
        else:
            # Unknown situation: Convert to string as fallback
            return str(value)

    def get_job(self, job_id: str) -> Optional[PrintJob]:
        if not job_id.isdigit():
            return None
        cups_job_id = int(job_id)
        try:
            with cups_lock:
                job_attributes = self.conn.getJobAttributes(cups_job_id)
        except IPPError as e:
            code, reason = e.args
            if code == IPP_NOT_FOUND:
                return None  # Job does not exist, or has expired
            raise HTTPInternalServerError(
                title=f"Failed to get job attributes from CUPS: {e}"
            )
        job_state_integer = job_attributes.get("job-state")
        if not job_state_integer:
            raise HTTPInternalServerError(title="Failed to get job state from CUPS")
        job_state = JobState(job_state_integer)

        reasons = job_attributes.get("job-state-reasons") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        elif not isinstance(reasons, list):
            raise TypeError(
                f"Unexpected type {type(reasons)} for printer state reasons: {reasons}"
            )
        if reasons == ["none"]:
            reasons = []

        return PrintJob(
            job_id=str(cups_job_id),
            job_state=job_state,
            job_state_reasons=reasons,
        )

    def print(
        self,
        printer: PrinterDetails,
        files: list[PrintFile],
        job_title: str,
        is_async: bool,
        media_size: Optional[MediaSize],
        options: dict[str, str],
    ) -> PrintJob:
        if not files:
            raise ValueError()

        for option_name in options:
            if option_name in DISALLOWED_OPTIONS:
                raise HTTPBadRequest(title=f"Option {option_name} is not permitted")

        options_ = {
            spec.keyword: spec.default_choice
            for spec in printer.supported_options
            if spec.default_choice and spec.default_choice in spec.choices
        }
        options_.update(options)

        content_type = re.sub(r";.*", "", files[0].content_type)
        for file in files:
            if file.content_type != content_type:
                # Files have inconsistent content type. PyCups does not support
                # this, so fall back on CUPS content-type auto-detection.
                content_type = None

            if file.content_type.startswith("application/pdf"):
                pass
            elif file.content_type.startswith("image/"):
                pass
            elif file.content_type == "text/plain":
                pass
            else:
                raise HTTPBadRequest(title=f"Unknown file type: {file.content_type}")
        if content_type:
            options_["document-format"] = content_type
        if media_size:
            options_["media"] = media_size.full_identifier

        # If the printer is disabled, we need CUPS to enable it prior to use
        if (
            printer.printer_state == PrinterState.STOPPED
            and "paused" in printer.state_reasons
        ):
            try:
                with cups_lock:
                    self.conn.enablePrinter(printer.identifier)
            except IPPError as e:
                code, reason = e.args
                if code == IPP_NOT_AUTHORIZED:
                    raise HTTPBadRequest(
                        title=f"Printer {printer.name} is paused and cannot be enabled"
                    )
                raise HTTPInternalServerError(
                    title=f"Printer {printer.name} is paused and could not be re-enabled: {e}"
                )

        with ExitStack() as stack:
            tempfiles = []
            for file in files:
                f = stack.enter_context(NamedTemporaryFile())
                f.write(file.content)
                f.flush()
                tempfiles.append(f)
            with cups_lock:
                job_id = str(
                    self.conn.printFiles(
                        printer.identifier,
                        [f.name for f in tempfiles],
                        job_title,
                        options_,
                    )
                )

        # Wait for up to 30 seconds for the job to complete
        start_time = time.time()
        sleep_timer = 0.1  # Short sleep for the initial check
        MAX_WAIT_TIME = 25.0  # Wait at most 25 seconds because many web servers have a 30-second timeout.
        while True:
            print_job = self.get_job(job_id)
            if print_job is None:
                raise HTTPInternalServerError(
                    title="Failed to get job attributes from CUPS"
                )

            waited_time = time.time() - start_time
            if (
                is_async
                or waited_time + sleep_timer > MAX_WAIT_TIME
                or not print_job
                or print_job.job_state not in [JobState.PENDING, JobState.PROCESSING]
            ):
                return print_job

            time.sleep(sleep_timer)
            sleep_timer = 1.0  # Longer sleep for subsequent checks
