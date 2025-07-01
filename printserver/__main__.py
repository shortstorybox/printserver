import sys
from importlib.metadata import version, PackageNotFoundError
from waitress import serve
from logging import basicConfig, INFO
from printserver import api, index_page, allowlist_middleware, AllowDomainMiddleware
import argparse

DEFAULT_PORT = 3888


def main():
    basicConfig(level=INFO)  # set up logging

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


if __name__ == "__main__":
    main()
