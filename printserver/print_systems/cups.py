import re
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

import falcon
import cups
from typing import Optional


# CUPS-specific options that are not printer-specific
GENERIC_OPTIONS = {
    "copies": PrintOption(
        display_name="Number of Copies",
        default_choice="1",
        choices=[str(x) for x in range(1, 101)],
    ),
    "collate": PrintOption(
        display_name="Collate Copies",
        default_choice="false",
        choices=["true", "false"],
    ),
    "fit-to-page": PrintOption( # Shorthand for print-scaling=fill
        display_name="Scale to Fill Page",
        default_choice="false",
        choices=["true", "false"],
    ),
    "mirror": PrintOption(
        display_name="Flip Horizontally",
        default_choice="false",
        choices=["true", "false"],
    ),
    "landscape": PrintOption(
        display_name="Landscape",
        default_choice="false",
        choices=["true", "false"],
    ),
    "outputorder": PrintOption(
        display_name="Sheet Order",
        default_choice="normal",
        choices=["normal", "reverse"],
    ),
    "page-border": PrintOption(
        display_name="Border",
        default_choice="none",
        choices=["none", "single", "single-thick", "double", "double-thick"],
    ),
    "number-up": PrintOption(
        display_name="Pages per Sheet",
        default_choice="1",
        choices=["1", "2", "4", "6", "9", "16"],
    ),
    "number-up-layout": PrintOption(
        display_name="Layout Direction",
        default_choice="lrtb",
        choices=["lrtb", "btlr", "btrl", "lrbt", "rlbt", "rltb", "tblr", "tbrl"],
    ),
    "print-scaling": PrintOption(
        display_name="Scale to Fit Paper Size",
        default_choice="none",
        choices=["auto", "auto-fit", "fill", "fit", "none"],
    ),
    "job-sheets": PrintOption(
        display_name="Banner/Trailer Page",
        default_choice="none,none",
        choices=[
            "none,none",
            "classified,none",
            "confidential,none",
            "secret,none",
            "standard,none",
            "topsecret,none",
            "unclassified,none",
            "none,classified",
            "none,confidential",
            "none,secret",
            "none,standard",
            "none,topsecret",
            "none,unclassified",
        ],
    ),
}

# CUPS-specific options that are disallowed for security
DISALLOWED_GENERIC_OPTIONS = {
    "job-priority",
    "job-hold-until",
    "job-cancel-after",
    "notify-lease-duration",
    "notify-events",
    "media", # Use the top-level mediaSize param instead
    "document-format", # Filled automatically
    "prettyprint", # Deprecated, and only works for text-only files
    "orientation-requested", # Use "landscape" option instead
}


class CupsPrintSystem(PrintSystem):
    def __init__(self):
        self.conn = cups.Connection()

    @classmethod
    def system_name(cls) -> str:
        return "cups"

    @classmethod
    def is_supported(cls) -> bool:
        """Check if CUPS is supported on this machine"""
        if os.name != "posix":
            return False
        try:
            cups.Connection().getPrinters()
        except cups.IPPError:
            return False
        return True

    def get_printers(self, printer_selector: PrinterSelector) -> list[PrinterDetails]:
        """Return the list of available CUPS printers that match the given selector"""
        printers = self.conn.getPrinters()
        results = []
        for printer_name, printer in printers.items():
            if PrinterState(printer["printer-state"]) not in [
                PrinterState.IDLE,
                PrinterState.PROCESSING,
            ]:
                continue
            if "offline-report" in printer["printer-state-reasons"]:
                continue
            if not printer["printer-make-and-model"].lower().startswith(
                printer_selector.model_prefix.lower()
            ) or not printer["printer-info"].lower().startswith(
                printer_selector.name_prefix.lower()
            ):
                continue

            supported_options = {}

            # Parse IPP options
            ipp_attributes = self.conn.getPrinterAttributes(printer_name)
            job_attributes = [
                key
                for key in ipp_attributes.get("job-creation-attributes-supported", [])
                if not key.endswith("-col") # IPP collections are not supported
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

                parsed_choices = [self.parse_ipp_attribute(option_name, x) for x in choices]
                if default_choice and default_choice not in parsed_choices:
                    parsed_choices = [default_choice] + parsed_choices
                if not parsed_choices:
                    continue  # Skip options that are missing a spec
                supported_options[option_name] = PrintOption(
                    display_name=option_name.replace("-", " ")
                    .replace("_", " ")
                    .capitalize(),
                    default_choice=default_choice,
                    choices=parsed_choices,
                )

            # Parse PPD options
            try:
                ppd_file = self.conn.getPPD(printer_name)
            except cups.IPPError:
                ppd_file = None
            if ppd_file:
                try:
                    ppd = cups.PPD(ppd_file)
                except RuntimeError:
                    ppd = None
                if ppd:
                    groups = list(ppd.optionGroups)
                    for group in groups:
                        for subgroup in group.subgroups:
                            groups.append(subgroup)
                        for option in group.options:
                            default_choice = option.defchoice or None
                            choices = [x['choice'] for x in option.choices]
                            if default_choice and default_choice not in choices:
                                choices = [default_choice] + choices
                            supported_options[option.keyword] = PrintOption(
                                display_name=option.text,
                                default_choice=default_choice,
                                choices=choices,
                            )

            supported_options.update(GENERIC_OPTIONS) # Non-printer-specific options
            for option_name in DISALLOWED_GENERIC_OPTIONS:
                supported_options.pop(option_name, None)

            # Parse supported media sizes
            media_sizes = []
            media_names = ipp_attributes.get('media-supported') or []
            for media_id in media_names:
                if not re.match(r'^[^_]*_[^_]*_[^_]*$', media_id):
                    continue
                world_region, size_name, dimensions = media_id.split('_')
                dimensions = dimensions.lower()
                if dimensions.endswith('in'):
                    dimensions = dimensions[:-2]
                    units = SizeUnit.INCHES
                elif dimensions.lower().endswith('mm'):
                    dimensions = dimensions[:-2]
                    units = SizeUnit.MILLIMETERS
                else:
                    units = SizeUnit.POINTS
                if not re.match(r'^[0-9]*[.]?[0-9]+x[0-9]*[.]?[0-9]+$', dimensions):
                    continue
                width, height = dimensions.split('x')
                media_sizes.append(MediaSize(
                    name=size_name,
                    width=float(width),
                    height=float(height),
                    units=units,
                    full_identifier=media_id,
                ))

            results.append(
                PrinterDetails(
                    name=printer["printer-info"],
                    model=printer["printer-make-and-model"],
                    identifier=printer_name,
                    printer_state=PrinterState(printer["printer-state"]),
                    state_reasons=printer["printer-state-reasons"],
                    print_system=self.system_name(),
                    media_sizes=media_sizes,
                    supported_options=supported_options,
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
            job_attributes = self.conn.getJobAttributes(cups_job_id)
        except cups.IPPError as e:
            code, reason = e.args
            if code == cups.IPP_NOT_FOUND:
                return None  # Job does not exist, or has expired
            raise falcon.HTTPInternalServerError(
                description=f"Failed to get job attributes from CUPS: {e}"
            )
        job_state_integer = job_attributes.get("job-state")
        if not job_state_integer:
            raise falcon.HTTPInternalServerError(
                description="Failed to get job state from CUPS"
            )
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
            if option_name in DISALLOWED_GENERIC_OPTIONS:
                raise falcon.HTTPBadRequest(
                    description=f"Option {option_name} is not permitted"
                )

        options_ = {k: v.default_choice for k, v in GENERIC_OPTIONS.items()}
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
                raise falcon.HTTPBadRequest(
                    description=f"Unknown file type: {file.content_type}"
                )
        if content_type:
            options_["document-format"] = content_type
        if media_size:
            options_['media'] = media_size.full_identifier

        with ExitStack() as stack:
            tempfiles = []
            for file in files:
                f = stack.enter_context(NamedTemporaryFile())
                f.write(file.content)
                f.flush()
                tempfiles.append(f)
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
                raise falcon.HTTPInternalServerError(
                    description="Failed to get job attributes from CUPS"
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
