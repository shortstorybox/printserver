import re
from threading import Lock
from printserver.print_systems.base import (
    PrintSystem,
    PrinterSelector,
    PrintFile,
    PrintJob,
    PrinterDetails,
    JobState,
    PrinterState,
    PrintOption,
    SizeUnit,
    MediaSize,
)
import os
import os.path
from contextlib import ExitStack
from tempfile import NamedTemporaryFile
import time

from falcon import HTTPInternalServerError, HTTPBadRequest
from cups import Connection, IPPError, PPD, IPP_NOT_FOUND, IPP_NOT_AUTHORIZED
from typing import Optional


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


class CupsPrintSystem(PrintSystem):
    def __init__(self):
        with cups_lock:
            self.conn = Connection()

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
            try:
                with cups_lock:
                    ppd_file = self.conn.getPPD(printer_name)
            except IPPError:
                ppd_file = None
            if ppd_file:
                try:
                    ppd = PPD(ppd_file)
                except RuntimeError:
                    ppd = None
                if ppd:
                    groups = list(ppd.optionGroups)
                    for group in groups:
                        for subgroup in group.subgroups:
                            groups.append(subgroup)
                        for option in group.options:
                            default_choice = option.defchoice or None
                            choices = [x["choice"] for x in option.choices]
                            if default_choice and default_choice not in choices:
                                choices = [default_choice] + choices
                            supported_options.append(
                                PrintOption(
                                    keyword=option.keyword,
                                    default_choice=default_choice,
                                    choices=choices,
                                    display_name=option.text,
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
