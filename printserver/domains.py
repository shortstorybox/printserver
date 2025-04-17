import os
from html import escape
import os.path
from platformdirs import user_config_dir
from urllib.parse import quote
import re

import falcon
from logging import getLogger

logger = getLogger(__name__)


class AllowDomainMiddleware:
    CONFIG_FILE = "allowed_domains.txt"

    def __init__(self):
        # Read allowlist from config file
        self.allowlist = set()
        try:
            with open(AllowDomainMiddleware.config_file(), "r") as f:
                for line in f:
                    line_origin = self.normalize_origin(line.strip())
                    if line_origin and self.format_is_valid(line_origin):
                        self.allowlist.add(line_origin)
        except FileNotFoundError:
            pass

    def is_allowed(self, http_origin):
        http_origin = self.normalize_origin(http_origin)
        if not http_origin or not self.format_is_valid(http_origin):
            return False
        if http_origin in self.allowlist:
            return True

    @staticmethod
    def config_file():
        folder = user_config_dir("printserver", ensure_exists=True)
        return os.path.join(folder, AllowDomainMiddleware.CONFIG_FILE)

    @classmethod
    def format_is_valid(cls, http_origin: str) -> bool:
        if http_origin.endswith(":80") or http_origin.endswith(":443"):
            # Ports :80 and :443 are implicit and should be removed by
            # normalization
            return False
        return bool(
            re.match(
                r"^https?://([-a-z0-9.]+|\[[:a-f0-9.]+\])(:[0-9]+)?$",
                cls.normalize_origin(http_origin),
            )
        )

    @staticmethod
    def normalize_origin(http_origin: str) -> str:
        result = http_origin.lower().rstrip("/")

        # Remove the port number if it's the default port for http/https
        if result.endswith(":80"):
            result = result[: len(":80")]
            if not result.startswith("http://"):
                # Disallow port :80 with ssl
                result = f"http://{result}"
        elif result.endswith(":443"):
            result = result[: len(":443")]
            if not result.startswith("https://"):
                # Disallow port :443 with non-ssl
                result = f"https://{result}"
        return result

    def process_request(self, request, response) -> None:
        if request.method == "OPTIONS":
            response.status = falcon.HTTP_200
            response.complete = True
            return

        origin = self.normalize_origin(request.get_header("Origin", default=""))
        if not origin:
            # Allow all requests that have no Origin header set. However, in
            # this case do not enable CORS in case a browser is misconfigured
            # and doesn't send the Origin header while relying on CORS for
            # access control.
            pass
        elif not self.format_is_valid(origin):
            raise falcon.HTTPBadRequest(
                description=f"Origin header cannot be parsed: {repr(origin)}"
            )
        elif not self.is_allowed(origin):
            api_base = request.forwarded_prefix
            raise falcon.HTTPForbidden(
                description=f"Visit {api_base}/domains/approve?origin={quote(origin)} to allow this domain to use the printer.",
                href=f"{api_base}/domains/approve?origin={quote(origin)}",
            )
        else:
            pass  # Domain is allowed. Allow the request to proceed.

    def process_response(self, request, response, resource, req_succeeded) -> None:
        origin = request.get_header("Origin", default="")
        if not origin:
            # Our domain-whitelisting code allows all requests with a blank
            # Origin header. Therefore, do not enable CORS for this type of
            # request, in case a browser is attempting to restrict access via
            # CORS, but without Origin header. (This would be unusual.)
            pass
        else:
            response.set_header("Access-Control-Allow-Origin", origin)
            response.set_header(
                "Access-Control-Allow-Methods",
                "GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD",
            )
            response.set_header("Access-Control-Max-Age", "86400")
            response.set_header("Access-Control-Allow-Headers", "Content-Type")


class DomainsSubmitApi:
    def __init__(self, allowlist_middleware: AllowDomainMiddleware):
        self.allowlist_middleware = allowlist_middleware

    def on_post(self, request, response):
        origin = AllowDomainMiddleware.normalize_origin(
            request.media.get("origin") or ""
        )
        if not origin:
            raise falcon.HTTPBadRequest(description="No origin parameter specified")
        if not AllowDomainMiddleware.format_is_valid(origin):
            raise falcon.HTTPBadRequest(description="The origin parameter is invalid")

        # NOTE: This only works correctly because all uvicorn worker threads
        # are in the same Python process. If this were running in a multi-process
        # configuration, the allowlist would not be updated for other processes
        # and we'd need to continually re-load the config file.
        DomainsSubmitApi.add_to_config_file(origin)
        self.allowlist_middleware.allowlist.add(origin)
        response.media = {}

    @staticmethod
    def add_to_config_file(origin: str):
        origin = AllowDomainMiddleware.normalize_origin(origin)
        if not origin:
            raise ValueError("HTTP origin is empty")
        if not AllowDomainMiddleware.format_is_valid(origin):
            raise ValueError(f"Invalid HTTP origin format: {origin}")
        with open(AllowDomainMiddleware.config_file(), "a+") as f:
            f.seek(0)
            if origin + "\n" in f:
                return  # Domain is already present in the allowlist file
            f.write(origin + "\n")
        logger.info(
            "HTTP origin is now permanently allowed to use printers: %s", origin
        )


class DomainsApprovePage:
    def on_get(self, request, response):
        origin = AllowDomainMiddleware.normalize_origin(
            request.params.get("origin") or ""
        )
        if not origin:
            raise falcon.HTTPBadRequest(description="Must specify an origin parameter")
        if not AllowDomainMiddleware.format_is_valid(origin):
            raise falcon.HTTPBadRequest(description="Origin parameter is invalid")
        response.status = falcon.HTTP_200
        response.content_type = "text/html"
        response.set_header(
            "Content-Security-Policy",
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'",
        )
        response.set_header("Cross-Origin-Opener-Policy", "same-origin")
        response.set_header("X-Frame-Options", "DENY")
        response.text = """
            <!DOCTYPE html>
            <html>
            <head>
              <title>Printer Permissions</title>
              <script>
                function showError(message) {
                  document.getElementById('error').style.display = 'block';
                  document.getElementById('error').textContent = message;
                }
                function allowDomain() {
                  document.getElementById('error').style.display = 'none';
                  document.getElementById('submit').disabled = true;
                  fetch('/domains/submit', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ origin: document.querySelector('#origin').value })
                  }).then(function (result) {
                    if (!result.ok) {
                      if (result.headers.get('content-type').toLowerCase() === 'application/json') {
                        result.json().then(function (x) {
                          showError(x.description || x.title || JSON.stringify(x));
                        }).catch(showError);
                      } else {
                        result.text().then(function (text) { showError(text || result.statusText); });
                      }
                      return;
                    }
                    document.querySelector('#request').style.display = 'none';
                    document.querySelector('#success').style.display = 'block';
                    if (window.opener) { window.opener.postMessage('permission-granted', '*'); }
                    setTimeout(function () {
                      // Attempt to close the window after 2 seconds. This isn't guaranteed
                      // to succeed, so display a message to the user as backup.
                      window.close();
                      document.querySelector('#close-window').style.display = 'block';
                    }, 2000);
                  }).catch(function (error) {
                    showError(error.message || error);
                  });
                }
              </script>
              <style>
                body { font-family: sans-serif; margin: 0px; padding: 1em; }
                h1 { margin-top: 0; font-size: 1.5em; }
                #request { max-width: 400px; }
                #submit { font-weight: bold; }
                #cancel { color: #777; }
                button { padding: 0.5em 1em; margin-right: 0.5em; font-size: 1em; }
                pre { background-color: #eee; padding: 0.5em; overflow-wrap: anywhere; white-space: pre-wrap; }
                #error { color: #f00; }
              </style>
            </head>
            <body>
              <div id="request">
                <h1>Connect to Printer?</h1>
                <p>This website is trying to print:</p>
                <pre>%(origin)s</pre>
                <input type="hidden" id="origin" value="%(origin)s">
                <button id="cancel" onclick="document.body.innerHTML='Cancelled.'; window.close();">Cancel</button>
                <button id="submit" onclick="allowDomain()">Allow</button>
                <p id="error"></p>
              </div>
              <div id="success" style="display: none;">
                <h1>Success!</h1>
                <p id="close-window" style="display: none;">
                  You can now close this window.
                </p>
              </div>
            </body>
            </html>
        """ % {"origin": escape(origin)}
