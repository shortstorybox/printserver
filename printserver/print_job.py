from .print_systems.base import PrinterSelector, PrintFile, MediaSize, SizeUnit, PrinterDetails
from requests.exceptions import HTTPError, ConnectionError
from math import isfinite
import re
import base64
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Union

import falcon
import requests
from .print_systems import PrintSystemProvider
from logging import getLogger

logger = getLogger(__name__)
executor = ThreadPoolExecutor(max_workers=8)


class ListPrintJobApi:
    def __init__(self, print_systems: PrintSystemProvider):
        self.print_systems = print_systems

    @staticmethod
    def validate_options(options):
        if not isinstance(options, dict):
            raise falcon.HTTPBadRequest(
                description="Invalid 'options' parameter: must be a dictionary"
            )

        if len(options) > 1023:
            raise falcon.HTTPBadRequest(
                description="Invalid 'options' parameter: too many options (max 1023)"
            )

        for option_name, value in options.items():
            # Option name
            if not isinstance(option_name, str):
                raise falcon.HTTPBadRequest(
                    description=f"Invalid 'options' parameter: option names must be strings. Got {type(option_name).__name__}"
                )
            if len(option_name) > 255:
                raise falcon.HTTPBadRequest(
                    description="Invalid 'options' parameter: option name too long (max 255)"
                )
            if not re.match(r"^[!-~]+$", option_name):
                raise falcon.HTTPBadRequest(
                    description=f"Invalid 'options' parameter: Invalid option name {repr(option_name)}"
                )

            # Option value
            if not isinstance(value, str):
                raise falcon.HTTPBadRequest(
                    description=f"Invalid parameter options.{option_name}: Expected string, but got {type(value).__name__}"
                )
            if len(value) > 1023:
                raise falcon.HTTPBadRequest(
                    description=f"Invalid parameter options.{option_name}: Length too long (max 1023 characters)"
                )
            if not re.match(r"^[!-~]+$", value):
                raise falcon.HTTPBadRequest(
                    description=f"Invalid parameter options.{option_name}: {repr(value)}"
                )

    @staticmethod
    def remove_unsupported_options(printer, options) -> dict[str, str]:
        '''
        TBD: We currently exclude options that are not supported by the
        printer. The original rationale was security, but we need to consider
        whether it's worth allowing the user to specify arbitrary options
        in case the printer doesn't declare all its supported options.
        '''
        result = {}
        for option_name, choice in options.items():
            if option_name in printer.supported_options:
                # Media is a special attribute that permits custom size values
                if choice in printer.supported_options[option_name].choices:
                    result[option_name] = choice
        return result

    @staticmethod
    def get_warnings(printer, media_size: Optional[MediaSize], user_supplied_options: dict[str, str]) -> list[str]:
        warnings = []

        # Media Size
        if media_size and media_size.width is not None and media_size.name and media_size.name.lower() != 'custom':
            name, width, height, units = (media_size.name, media_size.width, media_size.height, media_size.units)
            for x in printer.media_sizes:
                if name == x.name:
                    if (width, height, units) != (x.width, x.height, x.units):
                        warnings.append(
                            f"Media size {name} doesn't match size specified by printer. "
                            f"Expected {x.width}x{x.height}{x.units.value}, but got "
                            f"{width}x{height}{units.value}"
                        )
                    break

        # Options
        for option, value in user_supplied_options.items():
            if option not in printer.supported_options:
                warnings.append(f"Printer does not support option: {repr(option)}")
            elif value not in printer.supported_options[option].choices:
                supported_repr = ", ".join(
                    map(repr, printer.supported_options[option].choices)
                )
                warnings.append(
                    f"Printer does not support option {option}={repr(value)}'. "
                    f"Supported choices are: {supported_repr}"
                )
        return warnings

    @staticmethod
    def parse_media_size(printer: PrinterDetails, media_size_param: dict[str, Union[float, str]]) -> Optional[MediaSize]:
        if media_size_param is None:
            return None
        if not isinstance(media_size_param, dict):
            raise ValueError(
                f"If specified, mediaSize must be a dictionary continaing 'name' and/or 'width', 'height', and 'units'. Got: {media_size_param}"
            )
        name, width, height, units_ = (
            media_size_param.get('name'),
            media_size_param.get('width'),
            media_size_param.get('height'),
            media_size_param.get('units'),
        )
        if not name and not width and not height and not units_:
            return None
        if name is not None and not isinstance(name, str):
            raise ValueError(
                f"Invalid value for mediaSize.name: {name}"
            )
        if (width is None) != (height is None):
            raise ValueError(
                f"Must specify both mediaSize.width and mediaSize.height, or neither"
            )
        if (width is None) != (units_ is None):
            raise ValueError(
                f"mediaSize.units should be specified along with mediaSize.width/mediaSize.height"
            )
        if width is not None and height is not None:
            if not isinstance(width, float) or width <= 0 or not isfinite(width) or \
                    not isinstance(height, float) or height <= 0 or not isfinite(height):
                raise ValueError(
                    f"Invalid value for mediaSize.width/mediaSize.height: {width}/{height}"
                )
        if units_ is None:
            units = None
        else:
            if units_ in SizeUnit.__members__:
                units = SizeUnit[units_]
            else:
                raise ValueError(
                    f"Invalid value for mediaSize.units: {repr(units)}"
                )

        for x in printer.media_sizes:
            if name and name.lower() != 'custom':
                if width is None or (width, height, units) == (x.width, x.height, x.units):
                    full_identifier = x.full_identifier
                    break
        else:
            units_str = (
                    '' if units is SizeUnit.POINTS else
                    'mm' if units is SizeUnit.MILLIMETERS else
                    'inches' if units is SizeUnit.INCHES else None
            )
            # Hack: This identifier format is IPP-specific, whereas this file should
            # be independent of print system. Consider refactoring this in future.
            full_identifier = f"custom_{width}x{height}{units_str}_{width}x{height}{units_str}"
        return MediaSize(
            name=name or 'custom',
            width=width,
            height=height,
            units=SizeUnit[units] if units else None,
            full_identifier=full_identifier,
        )

    def on_post(self, request, response):
        job_title = request.media.get("jobTitle") or ""
        if not isinstance(job_title, str):
            raise falcon.HTTPBadRequest(
                description=f"Invalid value for jobTitle: {job_title}"
            )

        options = request.media.get("options") or {}
        self.validate_options(options)

        is_async = request.media.get("async") or False
        files = request.media.get("files")
        if not files:
            raise falcon.HTTPBadRequest(description="Must specify a list of files")

        futures = {}
        for file in files:
            file_url = file.get("fileUrl")
            if not file_url:
                continue
            if not isinstance(file_url, str):
                raise falcon.HTTPBadRequest(description=f"Invalid fileUrl: {file_url}")
            if file_url in futures:
                continue # file is already downloading

            parsed_url = requests.utils.urlparse(file_url)
            if parsed_url.scheme not in ("http", "https"):
                raise falcon.HTTPBadRequest(
                    description=f"fileUrl must start with http:// or https:// - Got: {repr(file_url)}"
                )
            futures[file_url] = executor.submit(requests.get, parsed_url.geturl())

        downloaded_files = []
        for file in files:
            if "fileUrl" in file:
                try:
                    http_response = futures[file["fileUrl"]].result()
                    http_response.raise_for_status()
                except (HTTPError, ConnectionError) as e:
                    raise falcon.HTTPBadRequest(description=f"Error fetching file: {e}")
                downloaded_files.append(
                    PrintFile(
                        http_response.headers["content-type"], http_response.content
                    )
                )
            else:
                content_type = file.get("contentType")
                if not content_type:
                    raise falcon.HTTPBadRequest(
                        description="Must specify files[].contentType or files[].fileUrl"
                    )
                elif not isinstance(content_type, str):
                    raise falcon.HTTPBadRequest(
                        description=f"Invalid value for files[].contentType: {content_type}"
                    )

                encoded_content = file.get("base64")
                text_content = file.get("text")
                if text_content and encoded_content:
                    raise falcon.HTTPBadRequest(
                        description="Cannot specify both files[].base64 or files[].text"
                    )
                elif encoded_content:
                    if not isinstance(encoded_content, str):
                        raise falcon.HTTPBadRequest(
                            description=f"Invalid value for files[].base64: {encoded_content}"
                        )
                    try:
                        content = base64.b64decode(encoded_content, validate=True)
                    except ValueError as e:
                        raise falcon.HTTPBadRequest(
                            description=f"Value for files[].base64 is invalid: {e}"
                        )
                elif text_content:
                    if not isinstance(text_content, str):
                        raise falcon.HTTPBadRequest(
                            description=f"Invalid value for files[].text: {text_content}"
                        )
                    try:
                        content = text_content.encode("utf-8")
                    except UnicodeEncodeError as e:
                        raise falcon.HTTPBadRequest(
                            description=f"Value for files[].text contains invalid Unicode characters: {e}"
                        )
                else:
                    raise falcon.HTTPBadRequest(
                        description="Must specify one of files[].contentType, files[].base64, or files[].text"
                    )

                downloaded_files.append(
                    PrintFile(content_type=content_type, content=content)
                )

        printer_selector = PrinterSelector.parse(
            request.media.get("printerSelector") or {}
        )
        print_system, printer = None, None
        for system in self.print_systems.supported_systems:
            for p in system.get_printers(printer_selector):
                print_system, printer = system, p
                break
            if printer:
                break
        if not printer or not print_system:
            raise falcon.HTTPBadRequest(
                description="No matching printer is attached"
                if printer_selector
                else "No printer is attached"
            )

        try:
            media_size = self.parse_media_size(printer, request.media.get('mediaSize', {}))
        except ValueError as e:
            raise falcon.HTTPBadRequest(
                description=e.args[0]
            )

        warnings = self.get_warnings(printer, media_size, options)
        for warning in warnings:
            logger.warning("%s", warning)
        logger.info(
            "Printing %s file(s) to printer: %s (%s)",
            len(files),
            printer.name,
            print_system.system_name(),
        )
        print_job = print_system.print(
            printer, downloaded_files, job_title, is_async, media_size, self.remove_unsupported_options(printer, options)
        )

        logger.info(
            "Print job %s state: %s (%s API call)",
            print_job.job_id,
            print_job.job_state.name,
            "async" if is_async else "synchronous",
        )
        response.status = falcon.HTTP_201
        response.location = f"/print-jobs/{print_job.job_id}"
        response.media = {
            "jobId": print_job.job_id,
            "jobState": print_job.job_state.name.lower(),
            "jobStateReasons": print_job.job_state_reasons,
            # Report a warning for any options the printer doesn't support
            "warnings": warnings,
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
            raise falcon.HTTPNotFound(description=f"Unrecognized job ID: {job_id}")
        elif len(job_results) > 1:
            raise falcon.HTTPInternalServerError(
                description="Multiple print systems returned job results"
            )

        (print_job,) = job_results
        response.status = falcon.HTTP_200
        response.media = {
            "jobId": print_job.job_id,
            "jobState": print_job.job_state.name.lower(),
            "jobStateReasons": print_job.job_state_reasons,
        }
