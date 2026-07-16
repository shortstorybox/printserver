import os
import sys
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path
from waitress import serve
from logging import basicConfig, getLogger, Handler, INFO, StreamHandler
from logging.handlers import RotatingFileHandler
from printserver import api, index_page, allowlist_middleware, AllowDomainMiddleware
import argparse

DEFAULT_PORT = 2888
LOG_FORMAT = "{asctime} {levelname} {name} (at {filename}:{lineno}]): {message}"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

logger = getLogger(__name__)


def setup_logging():
    # Always log to stderr for interactive use. On macOS, also log to a
    # rotating file in the standard location: /Library/Logs when running as
    # the launchd daemon (root), ~/Library/Logs otherwise.
    handlers: list[Handler] = [StreamHandler()]
    log_dir = None
    if sys.platform == "darwin":
        if os.geteuid() == 0:
            log_dir = Path("/") / "Library" / "Logs" / "PrintServer"
        else:
            log_dir = Path.home() / "Library" / "Logs" / "PrintServer"
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            handlers.append(
                RotatingFileHandler(
                    log_dir / "PrintServer.log",
                    maxBytes=LOG_MAX_BYTES,
                    backupCount=LOG_BACKUP_COUNT,
                )
            )
        except OSError as error:
            basicConfig(level=INFO, style="{", format=LOG_FORMAT, handlers=handlers)
            logger.warning("Could not open log file in %s: %s", log_dir, error)
            return
    basicConfig(level=INFO, style="{", format=LOG_FORMAT, handlers=handlers)


def main():
    setup_logging()

    logger.info("Print server starting...")

    def parse_origin(http_origin: str):
        if AllowDomainMiddleware.format_is_valid(
            AllowDomainMiddleware.normalize_origin(http_origin)
        ):
            return http_origin
        else:
            raise argparse.ArgumentTypeError(
                f"Invalid Domain/Origin format: {http_origin}. Example: https://mydomain.com"
            )

    parser = argparse.ArgumentParser(description="The missing JavaScript Printer API")
    try:
        __version__ = version("printserver")
    except PackageNotFoundError:
        __version__ = "local-build"
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to listen on (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--enable-external-access",
        default=False,
        action="store_true",
        help="Allow external computers to access the printer API over the network. This is disabled by default.",
    )
    parser.add_argument(
        "--allow",
        action="append",
        type=parse_origin,
        help="Allow the given domain/HTTP origin to use the printer. "
        "This argument can be specified multiple times to allow multiple domains. "
        "Example: --allow https://mydomain.com",
    )
    args = parser.parse_args()
    index_page.enable_external_access = args.enable_external_access
    for http_origin in args.allow or []:
        allowlist_middleware.allowlist.add(http_origin)

    logger.info("Print server listening for jobs...")
    try:
        # This will run in a single process, but with multiple threads. The
        # number of threads is fairly large because non-async printjobs can
        # lock a single thread for up to 30 seconds.
        MAX_THREADS = 20
        bind_address = "0.0.0.0" if args.enable_external_access else "127.0.0.1"
        serve(api, host=bind_address, port=args.port, threads=MAX_THREADS)
    except KeyboardInterrupt:
        sys.stderr.write("\nExiting due to Ctrl-C\n")
        sys.stderr.flush()
        pass  # Fail silently for Ctrl-C
    except Exception:
        # Make sure crashes are recorded in the log file, since launchd
        # discards stderr.
        logger.exception("PrintServer unhandled exception", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
