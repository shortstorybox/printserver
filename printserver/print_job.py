from dataclasses import dataclass
from falcon.media.multipart import MultipartForm, BodyPart
from urllib.parse import urlparse
from .print_systems.base import (
    PrinterSelector,
    PrintFile,
    MediaSize,
    SizeUnit,
    PrinterDetails,
)
from requests.exceptions import HTTPError, ConnectionError
from math import isfinite
import re
import base64
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple

import falcon
from falcon import HTTPBadRequest, HTTPNotFound, HTTPInternalServerError
import requests
from .print_systems import PrintSystemProvider
from logging import getLogger

logger = getLogger(__name__)
executor = ThreadPoolExecutor(max_workers=8)


@dataclass
class PrintRequest:
    # Both JSON and multipart/form-data requests are supported. They are parsed
    # into a PrintRequest object.

    job_title: str
    options: dict[str, str]
    is_async: bool
    files: list[PrintFile]
    printer_selector: PrinterSelector
    media_size_name: Optional[str]
    media_width: Optional[float]
    media_height: Optional[float]
    media_units: Optional[str]


class ListPrintJobApi:
    def __init__(self, print_systems: PrintSystemProvider):
        self.print_systems = print_systems

    @staticmethod
    def validate_options(options):
        if not isinstance(options, dict):
            raise HTTPBadRequest(
                title="Invalid 'options' parameter: must be a dictionary"
            )

        if len(options) > 1023:
            raise HTTPBadRequest(
                title="Invalid 'options' parameter: too many options (max 1023)"
            )

        for option_name, value in options.items():
            # Option name
            if not isinstance(option_name, str):
                raise HTTPBadRequest(
                    title=f"Invalid 'options' parameter: option names must be strings. Got {type(option_name).__name__}"
                )
            if len(option_name) > 255:
                raise HTTPBadRequest(
                    title="Invalid 'options' parameter: option name too long (max 255)"
                )
            if not re.match(r"^[!-~]+$", option_name):
                raise HTTPBadRequest(
                    title=f"Invalid 'options' parameter: Invalid option name {repr(option_name)}"
                )

            # Option value
            if not isinstance(value, str):
                raise HTTPBadRequest(
                    title=f"Invalid parameter options[{option_name}]: Expected string, but got {type(value).__name__}"
                )
            if len(value) > 1023:
                raise HTTPBadRequest(
                    title=f"Invalid parameter options[{option_name}]: Length too long (max 1023 characters)"
                )
            if not re.match(r"^[ !-~]+$", value):
                raise HTTPBadRequest(
                    title=f"Invalid parameter options[{option_name}]: {repr(value)}"
                )

    @staticmethod
    def remove_unsupported_options(printer, options) -> dict[str, str]:
        """
        TBD: We currently exclude options that are not supported by the
        printer. The original rationale was security, but we need to consider
        whether it's worth allowing the user to specify arbitrary options
        in case the printer doesn't declare all its supported options.
        """
        supported_choices = {x.keyword: x.choices for x in printer.supported_options}

        result = {}
        for key, value in options.items():
            if key in supported_choices and value in supported_choices[key]:
                result[key] = value
        return result

    @staticmethod
    def get_warnings(printer, user_supplied_options: dict[str, str]) -> list[str]:
        warnings = []
        supported_choices = {x.keyword: x.choices for x in printer.supported_options}
        for key, value in user_supplied_options.items():
            if key not in supported_choices:
                warnings.append(f"Printer does not support option: {repr(key)}")
            elif value not in supported_choices[key]:
                supported_repr = ", ".join(map(repr, supported_choices[key]))
                warnings.append(
                    f"Printer does not support option {key}={repr(value)}'. "
                    f"Supported choices are: {supported_repr}"
                )
        return warnings

    @staticmethod
    def parse_media_size(
        printer: PrinterDetails,
        name: Optional[str],
        width: Optional[float],
        height: Optional[float],
        units: Optional[str],
    ) -> Tuple[Optional[MediaSize], Optional[str]]:
        # Returns the determined MediaSize, if it can be determined from the
        # request parameters. If there are warnings (e.g. mismatch between
        # request & printer size definition), these will be returned in the
        # second param.

        if not isinstance(name, (str, type(None))):
            raise ValueError(f"Invalid value for media[size][name]: {name}")
        if not name or name.lower() == "custom":
            name = None
        if name:
            reason = None
            if "_" in name:
                reason = "Underscores are not allowed."
            elif len(name) > 255:
                reason = "Length is too long."
            elif not re.match(r"^[!-~]+$", name):
                reason = "Only printable ascii characters are allowed."
            if reason:
                raise ValueError(
                    f"Invalid value for media[size][name]: {name}. " + reason
                )
        if name and len(name) > 255:
            raise ValueError(
                f"Invalid value for media[size][name]: {name}. Length too long (max 255 characters)"
            )

        if not name and not width and not height and not units:
            return None, None
        if (width is None) != (height is None):
            raise ValueError(
                "Must specify both media[size][width] and media[size][height], or neither"
            )
        if (width is None) != (units is None):
            raise ValueError(
                "media[size][units] should be specified along with media[size][width]/media[size][height]"
            )
        if width is not None and height is not None:
            if (
                not isinstance(width, (float, int))
                or width <= 0
                or not isfinite(width)
                or not isinstance(height, (float, int))
                or height <= 0
                or not isfinite(height)
            ):
                raise ValueError(
                    f"Invalid value for media[size][width]/media[size][height]: {width}/{height}"
                )
            width = float(width)  # if int, convert to float
            height = float(height)  # if int, convert to float
        if units is None:
            units_enum = None
        elif not isinstance(units, str):
            raise ValueError(f"Invalid value for media[size][units]: {repr(units)}")
        else:
            if units in [u.value for u in SizeUnit]:
                units_enum = SizeUnit(units)
            else:
                raise ValueError(f"Invalid value for media[size][units]: {repr(units)}")

        if name:
            # Find the printer's definition for the given size name
            for size_spec in printer.media_sizes:
                if name == size_spec.name:
                    warning = None
                    if (
                        width is not None
                        and height is not None
                        and units_enum is not None
                    ):
                        if (width, height, units_enum) != (
                            size_spec.width,
                            size_spec.height,
                            size_spec.units,
                        ):
                            warning = (
                                f"Media size {name} doesn't match the specification for printer "
                                f"{printer.name}. Expected "
                                f"{size_spec.width:g}x{size_spec.height:g} {size_spec.units.value}, "
                                f"but got {width:g}x{height:g} {units_enum.value}"
                            )
                    return size_spec, warning
            else:
                # This printer doesn't have a specification for this size.
                # That's ok; we'll treat it as a custom size below.
                if width is None:
                    warning = (
                        f"Printer {printer.name} does not know how to "
                        f"interpret media size {name} without "
                        f"media[size][width]/media[size][height]"
                    )
                    return None, warning

        if (
            width is None or height is None or units_enum is None
        ):  # This implies that height and units are also None
            return None, None

        ipp_units = (
            ""
            if units_enum is SizeUnit.POINTS
            else "mm"
            if units_enum is SizeUnit.MILLIMETERS
            else "in"
            if units_enum is SizeUnit.INCHES
            else None
        )
        if not name:
            name = f"{width}x{height}{ipp_units}"
        # Hack: This format is IPP-specific, whereas this function should
        # technically be independent of print system. Consider refactoring.
        full_identifier = f"custom_{name}_{width}x{height}{ipp_units}"

        return MediaSize(
            name=name,
            width=width,
            height=height,
            units=units_enum,
            full_identifier=full_identifier,
        ), None

    def parse_multipart_request(self, form_data: MultipartForm) -> PrintRequest:
        job_title: str = ""
        options: dict[str, str] = {}
        is_async: bool = False
        media_size_name: Optional[str] = None
        media_width: Optional[float] = None
        media_height: Optional[float] = None
        media_units: Optional[str] = None
        selector_name: Optional[str] = None
        selector_print_system: Optional[str] = None
        selector_name_prefix: str = ""
        selector_model_prefix: str = ""
        files: list[PrintFile] = []

        for part in form_data:
            part: BodyPart
            if part.name is None:
                pass
            elif part.name == "jobTitle":
                job_title = part.text
            elif part.name == "options":
                raise HTTPBadRequest(
                    title="Invalid parameter: options. Use options[key] instead"
                )
            elif part.name.startswith("options["):
                key = part.name[len("options[") : -1]
                value = part.text
                if key and value:
                    options[key] = value
            elif part.name == "async":
                is_async = part.text.lower() not in ("false", "off", "no")
            elif part.name == "media" or part.name == "media[size]":
                raise HTTPBadRequest(
                    title=f"Invalid parameter: {part.name}. Use media[size][key] instead"
                )
            elif part.name == "media[size][name]":
                media_size_name = part.text
            elif part.name == "media[size][width]":
                try:
                    media_width = float(part.text)
                except ValueError:
                    raise HTTPBadRequest(
                        title=f"Invalid value for media[size][width]: {part.text}"
                    )
            elif part.name == "media[size][height]":
                try:
                    media_height = float(part.text)
                except ValueError:
                    raise HTTPBadRequest(
                        title=f"Invalid value for media[size][height]: {part.text}"
                    )
            elif part.name == "media[size][units]":
                media_units = part.text
            elif part.name == "printerSelector":
                raise HTTPBadRequest(
                    title="Invalid parameter: printerSelector. Use printerSelector[key] instead"
                )
            elif part.name == "printerSelector[name]":
                selector_name = part.text
            elif part.name == "printerSelector[printSystem]":
                PrinterSelector.validate_print_system(part.text)
                selector_print_system = part.text
            elif part.name == "printerSelector[namePrefix]":
                selector_name_prefix = part.text
            elif part.name == "printerSelector[modelPrefix]":
                selector_model_prefix = part.text
            elif part.name == "files":
                if not part.content_type:
                    raise HTTPBadRequest(title="Missing content type for file upload")
                files.append(
                    PrintFile(
                        content_type=part.content_type,
                        content=part.data,
                    )
                )
            else:
                pass  # skip unknown params
        return PrintRequest(
            job_title=job_title,
            options=options,
            is_async=is_async,
            media_size_name=media_size_name,
            media_width=media_width,
            media_height=media_height,
            media_units=media_units,
            printer_selector=PrinterSelector(
                name=selector_name,
                name_prefix=selector_name_prefix,
                model_prefix=selector_model_prefix,
                print_system=selector_print_system,
            ),
            files=files,
        )

    def parse_json_request(self, json_data: dict) -> PrintRequest:
        job_title = json_data.get("jobTitle") or ""
        if not isinstance(job_title, str):
            raise HTTPBadRequest(title=f"Invalid value for jobTitle: {job_title}")

        options = json_data.get("options") or {}
        if not isinstance(options, dict):
            raise HTTPBadRequest(
                title=f"Invalid value for options: {options}. Must be a dictionary"
            )

        is_async = json_data.get("async") or False
        if not isinstance(is_async, bool):
            raise HTTPBadRequest(title=f"Invalid value for async: {is_async}")

        media_size = json_data.get("media", {}).get("size") or {}
        if not isinstance(media_size, dict):
            raise HTTPBadRequest(title=f"Invalid value for media[size]: {media_size}")
        media_size_name = media_size.get("name") or None
        media_width = media_size.get("width")
        media_height = media_size.get("height")
        media_units = media_size.get("units") or None
        if not isinstance(media_size_name, (str, type(None))):
            raise HTTPBadRequest(
                title=f"Invalid value for media[size][name]: {media_size_name}"
            )
        if not isinstance(media_width, (float, int, type(None))):
            raise HTTPBadRequest(
                title=f"Invalid value for media[size][width]: {media_width}"
            )
        if not isinstance(media_height, (float, int, type(None))):
            raise HTTPBadRequest(
                title=f"Invalid value for media[size][height]: {media_height}"
            )
        if not isinstance(media_units, (str, type(None))):
            raise HTTPBadRequest(
                title=f"Invalid value for media[size][units]: {media_units}"
            )

        selector_dict = json_data.get("printerSelector") or {}
        if not isinstance(selector_dict, dict):
            raise HTTPBadRequest(
                title=f"Invalid value for printerSelector: {selector_dict}"
            )
        printer_selector = PrinterSelector.parse(selector_dict)

        files = json_data.get("files")
        if not isinstance(files, list) or len(files) == 0:
            raise HTTPBadRequest(title="Must specify a list of files")

        files_to_download = []
        futures = {}
        for file in files:
            if not isinstance(file, dict):
                raise HTTPBadRequest(
                    title=f"Invalid file: {file}. Must be a dictionary."
                )
            file_url = file.get("fileUrl")
            if not file_url:
                continue
            if not isinstance(file_url, str):
                raise HTTPBadRequest(title=f"Invalid fileUrl: {file_url}")
            if file_url in futures:
                continue  # file is already downloading

            parsed_url = urlparse(file_url)
            if parsed_url.scheme not in ("http", "https"):
                raise HTTPBadRequest(
                    title=f"fileUrl must start with http:// or https:// - Got: {repr(file_url)}"
                )
            files_to_download.append(file_url)

        # Don't start downloading files until we've confirmed that all URLs are valid
        for url in files_to_download:
            futures[url] = executor.submit(requests.get, urlparse(url).geturl())

        downloaded_files = []
        for file in files:
            if "fileUrl" in file:
                try:
                    http_response = futures[file["fileUrl"]].result()
                    http_response.raise_for_status()
                except (HTTPError, ConnectionError) as e:
                    raise HTTPBadRequest(title=f"Error fetching file: {e}")
                downloaded_files.append(
                    PrintFile(
                        http_response.headers["content-type"], http_response.content
                    )
                )
            else:
                content_type = file.get("contentType")
                if not content_type:
                    raise HTTPBadRequest(
                        title="Must specify files[].contentType or files[].fileUrl"
                    )
                elif not isinstance(content_type, str):
                    raise HTTPBadRequest(
                        title=f"Invalid value for files[].contentType: {content_type}"
                    )

                encoded_content = file.get("base64")
                text_content = file.get("text")
                if text_content and encoded_content:
                    raise HTTPBadRequest(
                        title="Cannot specify both files[].base64 or files[].text"
                    )
                elif encoded_content:
                    if not isinstance(encoded_content, str):
                        raise HTTPBadRequest(
                            title=f"Invalid value for files[].base64: {encoded_content}"
                        )
                    try:
                        content = base64.b64decode(encoded_content, validate=True)
                    except ValueError as e:
                        raise HTTPBadRequest(
                            title=f"Value for files[].base64 is invalid: {e}"
                        )
                elif text_content:
                    if not isinstance(text_content, str):
                        raise HTTPBadRequest(
                            title=f"Invalid value for files[].text: {text_content}"
                        )
                    try:
                        content = text_content.encode("utf-8")
                    except UnicodeEncodeError as e:
                        raise HTTPBadRequest(
                            title=f"Value for files[].text contains invalid Unicode characters: {e}"
                        )
                else:
                    raise HTTPBadRequest(
                        title="Must specify one of files[].contentType, files[].base64, or files[].text"
                    )

                downloaded_files.append(
                    PrintFile(content_type=content_type, content=content)
                )
        return PrintRequest(
            job_title=job_title,
            options=options,
            is_async=is_async,
            files=downloaded_files,
            printer_selector=printer_selector,
            media_size_name=media_size_name,
            media_width=media_width,
            media_height=media_height,
            media_units=media_units,
        )

    def on_post(self, request, response):
        if request.content_type == "application/json":
            data = self.parse_json_request(request.media)
        elif request.content_type.startswith("multipart/form-data"):
            data = self.parse_multipart_request(request.media)
        else:
            raise HTTPBadRequest(
                title=f"Unsupported content type: {request.content_type}"
            )

        self.validate_options(data.options)
        print_system, printer = None, None
        for system in self.print_systems.supported_systems:
            for p in system.get_printers(data.printer_selector):
                print_system, printer = system, p
                break
            if printer:
                break
        if not printer or not print_system:
            raise HTTPBadRequest(
                title="No matching printer is attached"
                if data.printer_selector
                else "No printer is attached"
            )

        try:
            media_size, size_warning = self.parse_media_size(
                printer,
                data.media_size_name,
                data.media_width,
                data.media_height,
                data.media_units,
            )
        except ValueError as e:
            raise HTTPBadRequest(title=e.args[0])

        warnings = self.get_warnings(printer, data.options)
        if size_warning:
            warnings.append(size_warning)
        for warning in warnings:
            logger.warning("%s", warning)
        logger.info(
            "Printing %s file(s) to printer: %s (%s)",
            len(data.files),
            printer.name,
            print_system.system_name(),
        )
        print_job = print_system.print(
            printer,
            data.files,
            data.job_title.encode()[:255].decode("utf-8", errors="ignore"),
            data.is_async,
            media_size,
            self.remove_unsupported_options(printer, data.options),
        )

        logger.info(
            "Print job %s state: %s (%s API call)",
            print_job.job_id,
            print_job.job_state.name,
            "async" if data.is_async else "synchronous",
        )
        response.status = falcon.HTTP_201
        response.location = f"/print-jobs/{print_job.job_id}"
        response.media = {
            "jobId": print_job.job_id,
            "jobState": print_job.job_state.name.lower(),
            "jobStateReasons": print_job.job_state_reasons,
            # Report a warning for any options the printer doesn't support
            "warnings": "\n".join(warnings) or None,
        }


class PrintJobApi:
    def __init__(self, print_systems: PrintSystemProvider):
        self.print_systems = print_systems

    def on_get(self, request, response, job_id: str):
        job_results = []
        for system in self.print_systems.supported_systems:
            job = system.get_job(job_id)
            if job:
                job_results.append(job)
        if not job_results:
            raise HTTPNotFound(title=f"Unrecognized job ID: {job_id}")
        elif len(job_results) > 1:
            raise HTTPInternalServerError(
                title="Multiple print systems returned job results"
            )

        (print_job,) = job_results
        response.status = falcon.HTTP_200
        response.media = {
            "jobId": print_job.job_id,
            "jobState": print_job.job_state.name.lower(),
            "jobStateReasons": print_job.job_state_reasons,
        }
