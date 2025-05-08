from printserver.print_systems.base import (
    PrintSystem,
    PrinterSelector,
    PrintFile,
    PrintJob,
    PrinterDetails,
    JobState,
    PrinterState,
)
import os
import os.path
from contextlib import ExitStack
from tempfile import NamedTemporaryFile
import time

import falcon
import cups
from typing import Optional
from pdf2image import convert_from_bytes
from PIL import Image


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

            all_attributes = self.conn.getPrinterAttributes(printer_name)
            job_attributes = [
                key
                for key in all_attributes.get("job-creation-attributes-supported", [])
                if key + "-default" in all_attributes
                and key + "-supported" in all_attributes
            ]
            results.append(
                PrinterDetails(
                    name=printer["printer-info"],
                    model=printer["printer-make-and-model"],
                    identifier=printer_name,
                    printer_state=PrinterState(printer["printer-state"]),
                    state_reasons=printer["printer-state-reasons"],
                    print_system=self.system_name(),
                    supported_options={
                        key: [
                            self.parse_ipp_attribute(key, x)
                            for x in all_attributes[key + "-supported"]
                        ]
                        for key in job_attributes
                    },
                    default_options={
                        key: self.parse_ipp_attribute(
                            key, all_attributes[key + "-default"]
                        )
                        for key in job_attributes
                    },
                )
            )
        return results

    @staticmethod
    def parse_ipp_attribute(key, value):
        """Convert pycups printer attributes to IPP-compatible strings."""
        if isinstance(value, tuple):
            # Handle resolution tuples (X, Y, 3) -> "XxYdpi"
            if "resolution" in key and len(value) == 3 and value[2] == 3:
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
        options: dict[str, str],
    ) -> PrintJob:
        for file in files:
            if not file.content_type.startswith(
                "application/pdf"
            ) and not file.content_type.startswith("image/"):
                raise falcon.HTTPBadRequest(
                    description=f"Unknown file type: {file.content_type}"
                )

        with ExitStack() as stack:
            tempfiles = []

            for file in files:
                # Convert PDF bytes to a list of images (one per page)
                pages = convert_from_bytes(file.content, dpi=203)

                for i, page in enumerate(pages):
                    # Rotate each image 90 degrees clockwise
                    rotated = page.rotate(270, expand=True)

                    # Save to a temporary PNG file
                    f = stack.enter_context(NamedTemporaryFile(suffix=".png", delete=False))
                    rotated.save(f, format="PNG", dpi=(203, 203))
                    f.flush()
                    tempfiles.append(f)

            # Set CUPS options
            cups_options = options.copy()
            cups_options["fit-to-page"] = "true"

            # Print all image files
            job_id = str(
                self.conn.printFiles(
                    printer.identifier,
                    [f.name for f in tempfiles],
                    job_title,
                    cups_options,
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
