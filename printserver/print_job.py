from .print_systems.base import PrinterSelector, PrintFile
import base64
from concurrent.futures import ThreadPoolExecutor

import falcon
import requests
from .print_systems import PrintSystemProvider
from logging import getLogger

logger = getLogger(__name__)
executor = ThreadPoolExecutor(max_workers=8)


class ListPrintJobApi:
    def __init__(self, print_systems: PrintSystemProvider):
        self.print_systems = print_systems

    def on_post(self, request, response):
        job_title = request.media.get("jobTitle") or ""
        if not isinstance(job_title, str):
            raise falcon.HTTPBadRequest(
                description=f"Invalid value for jobTitle: {job_title}"
            )

        options = request.media.get("options") or {}
        if not options:
            options = request.media.get("cupsOptions") or {}  # DEPRECATED
        if (
            not isinstance(options, dict)
            or any(not x or not isinstance(x, str) for x in options)
            or any(not y or not isinstance(y, str) for y in options.values())
        ):
            raise falcon.HTTPBadRequest(
                description="Invalid value for 'options' parameter: must be a dictionary of strings"
            )

        is_async = request.media.get("async") or False
        files = request.media.get("files")
        if not files:
            request.media.get("printJobs")  # DEPRECATED
        if not files:
            raise falcon.HTTPBadRequest(description="Must specify a list of files")

        futures = {}
        for file in files:
            file_url = file.get("fileUrl")
            if not file_url or file_url in futures:
                continue
            if not isinstance(file_url, str):
                raise falcon.HTTPBadRequest(description=f"Invalid fileUrl: {file_url}")
            if not file_url or not isinstance(file_url, str):
                raise falcon.HTTPBadRequest(
                    description=f"No file_url specified for {file}"
                )
            futures[file_url] = executor.submit(requests.get, file_url)

        downloaded_files = []
        for file in files:
            if "fileUrl" in file:
                http_response = futures[file["fileUrl"]].result()
                try:
                    http_response.raise_for_status()
                except requests.exceptions.HTTPError as e:
                    raise falcon.HTTPBadRequest(description=f"Error fetching file: {e}")
                downloaded_files.append(
                    PrintFile(
                        http_response.headers["content-type"], http_response.content
                    )
                )
            else:
                content_type = file.get("contentType")
                encoded_content = file.get("base64")
                if not content_type:
                    raise falcon.HTTPBadRequest(
                        description="Must specify files[].contentType or files[].fileUrl"
                    )
                elif not isinstance(content_type, str):
                    raise falcon.HTTPBadRequest(
                        description=f"Invalid value for files[].contentType: {content_type}"
                    )
                elif not encoded_content:
                    raise falcon.HTTPBadRequest(
                        description="Must specify files[].contentType or files[].base64"
                    )
                elif not isinstance(encoded_content, str):
                    raise falcon.HTTPBadRequest(
                        description=f"Invalid value for files[].base64: {encoded_content}"
                    )

                try:
                    content = base64.b64decode(encoded_content, validate=True)
                except ValueError as e:
                    raise falcon.HTTPBadRequest(
                        description=f"Value for files[].base64 is invalid: {e}"
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

        validated_options = {
            key: value
            for key, value in options.items()
            if value and value in printer.supported_options.get(key, [])
        }

        logger.info(
            "Printing %s file(s) to printer: %s (%s)",
            len(files),
            printer.name,
            print_system.system_name(),
        )
        print_job = print_system.print(
            printer, downloaded_files, job_title, is_async, validated_options
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
            "warnings": printer.get_warnings(options),
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
