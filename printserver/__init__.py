from printserver.domains import (
    AllowDomainMiddleware,
    DomainsApprovePage,
    DomainsSubmitApi,
)
from printserver.index import IndexPage

import falcon
from logging import getLogger
from printserver.print_systems import PrintSystemProvider
from printserver.print_job import PrintJobApi, ListPrintJobApi
from printserver.printers import ListPrintersApi

__all__ = ["api", "allowlist_middleware", "index_page"]
logger = getLogger(__name__)

print_systems = PrintSystemProvider()
allowlist_middleware = AllowDomainMiddleware()
index_page = IndexPage(print_systems, allowlist_middleware)

api = falcon.App(middleware=[allowlist_middleware])
api.add_route("/", index_page)
api.add_route("/printers", ListPrintersApi(print_systems))
api.add_route("/print-job", ListPrintJobApi(print_systems))
api.add_route("/print-job/{job_id}", PrintJobApi(print_systems))
api.add_route("/domains/approve", DomainsApprovePage())
api.add_route("/domains/submit", DomainsSubmitApi(allowlist_middleware))

# Deprecated routes
api.add_route("/print/shipping-label", ListPrintJobApi(print_systems))  # DEPRECATED
