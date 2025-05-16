from typing import Optional
import usb.core
from printserver.print_systems.base import (
    PrintSystem,
    PrinterSelector,
    PrintFile,
    PrintJob,
    PrinterDetails,
    JobState,
    PrinterState,
    MediaSize,
    SizeUnit,
)
import re
from io import BytesIO
from usb.core import NoBackendError

import falcon
import pdf2image
from PIL import Image, ImageOps


class BrotherQLPrintSystem(PrintSystem):
    def __init__(self):
        # Delay importing until here, since importing brother_ql causes annoying warnings on the console output
        import brother_ql
        import brother_ql.backends.helpers
        import brother_ql.conversion
        import brother_ql.devicedependent
        import brother_ql.labels

        self.brother_ql = brother_ql

        # Ugly hack: monkey-patch the brother_ql library to include the label size we're using
        self.LABEL = brother_ql.labels.Label(
            "103x164",  # type: ignore[reportCallIssue]
            (104, 164),
            brother_ql.labels.FormFactor.DIE_CUT,
            (1200, 1804),
            (1164, 1660),
            12,
            feed_margin=0,
            restricted_to_models=["QL-1050", "QL-1060N"],
            color=brother_ql.labels.Color.BLACK_WHITE,
        )
        if self.LABEL not in brother_ql.labels.ALL_LABELS:
            brother_ql.labels.ALL_LABELS += (self.LABEL,)
            brother_ql.labels.LabelsManager.DEFAULT_ELEMENTS += (self.LABEL,)  # type: ignore[reportAttributeAccessIssue]
            brother_ql.devicedependent._populate_label_legacy_structures()

    @classmethod
    def system_name(cls) -> str:
        return "brother_ql"

    @classmethod
    def is_supported(cls) -> bool:
        """Check if Brother QL is supported"""
        try:
            usb.core.find()
        except NoBackendError:
            # To use non-Cups Brother QL USB printers, install libusb:
            #  - Debian/Ubuntu: sudo apt-get install libusb-1.0-0
            #  - macOS: brew install libusb
            return False
        return True

    @staticmethod
    def fix_brother_url(printer_url):
        # Replace _ with / due to a bug in how brother_ql formats the printer URL
        return re.sub(r"(0x[0-9a-fA-F]+):(0x[0-9a-fA-F]+)_", r"\1:\2/", printer_url)

    def get_printers(self, printer_selector: PrinterSelector) -> list[PrinterDetails]:
        """Return the list of available CUPS printers that match the given selector"""
        printers = self.brother_ql.backends.helpers.discover("pyusb")

        results = []
        for printer in printers:
            if (
                printer["instance"]
                and printer["instance"]
                .product.lower()
                .startswith(printer_selector.model_prefix.lower())
                and printer["instance"]
                .manufacturer.lower()
                .startswith(printer_selector.name_prefix.lower())
            ):
                instance = printer["instance"]
                results.append(
                    PrinterDetails(
                        name=f"{instance.manufacturer} {instance.product} ({instance.serial_number})",
                        model=instance.product,
                        identifier=self.fix_brother_url(printer["identifier"]),
                        printer_state=PrinterState.IDLE,
                        state_reasons=[],
                        print_system=self.system_name(),
                        media_sizes=[
                            MediaSize(
                                name='103x164',
                                width=103,
                                height=164,
                                units='mm',
                                full_identifier='custom_103x164mm_103x164mm',
                            )
                        ],
                        supported_options={},
                    )
                )
        return results

    def get_job(self, job_id: str) -> Optional[PrintJob]:
        return None  # Brother QL does not support job management

    def print(
        self,
        printer: PrinterDetails,
        files: list[PrintFile],
        job_title: str,
        is_async: bool,
        media_size: Optional[MediaSize],
        options: dict[str, str],
    ) -> PrintJob:
        raster = self.brother_ql.BrotherQLRaster("QL-1050")
        raster.exception_on_warning = True
        width, height = self.LABEL.dots_printable

        images = []
        for file in files:
            if file.content_type.startswith("application/pdf"):
                # Requires poppler to be installed on the system
                images.extend(
                    pdf2image.convert_from_bytes(
                        file.content, single_file=True, size=(width, height)
                    )
                )
            elif file.content_type.startswith("image/"):
                images.append(Image.open(BytesIO(file.content)))
            else:
                raise falcon.HTTPBadRequest(
                    description=f"Unknown file type: {file.content_type}"
                )

        pages = [ImageOps.fit(image, (width, height)) for image in images]
        instructions = self.brother_ql.conversion.convert(
            qlr=raster, images=pages, label=self.LABEL.identifier, cut=True, hq=True
        )
        result = self.brother_ql.backends.helpers.send(
            instructions=instructions,
            printer_identifier=printer.identifier,
            backend_identifier="pyusb",
            blocking=not is_async,
        )
        if is_async:
            return PrintJob(job_id="", job_state=JobState.PENDING, job_state_reasons=[])
        elif result.get("did_print"):
            return PrintJob(
                job_id="", job_state=JobState.COMPLETED, job_state_reasons=[]
            )
        else:
            return PrintJob(
                job_id="",
                job_state=JobState.ABORTED,
                job_state_reasons=(result.get("printer_state") or {}).get("errors", []),
            )
