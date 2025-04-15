from importlib.metadata import version, PackageNotFoundError
from html import escape
from .domains import AllowDomainMiddleware
from .print_systems import PrintSystemProvider
from .print_systems.base import PrinterSelector

import falcon


class IndexPage:
    def __init__(
        self,
        print_systems: PrintSystemProvider,
        allowlist_middleware: AllowDomainMiddleware,
    ):
        self.print_systems = print_systems
        self.allowlist_middleware = allowlist_middleware
        self.enable_external_access = False

    def on_get(self, request, response):
        try:
            __version__ = version("printserver")
        except PackageNotFoundError:
            __version__ = "local-build"
        api_base = request.forwarded_prefix
        printers_html = []
        for system in self.print_systems.supported_systems:
            for printer in system.get_printers(PrinterSelector()):
                options_html = {
                    key: ", ".join(
                        f"<b><u>{escape(v)}</u></b>"
                        if v == printer.default_options[key]
                        else escape(v)
                        for v in values
                    )
                    for key, values in printer.supported_options.items()
                }
                printers_html.append(
                    """
                    <div class="printer">
                        <h2>%(name)s</h2>
                        <p><b>Name:</b> %(name)s</p>
                        <p><b>Model:</b> %(model)s</p>
                        <p><b>Print System:</b> %(printSystem)s</p>
                        <p><b>Printer State:</b> %(printerState)s</p>
                        <p><b>State Reasons:</b> %(stateReasons)s</p>
                        <p><b>Options (default is underlined):</b> <ul>%(options)s</ul></p>
                    </div>
                """
                    % {
                        "name": escape(printer.name),
                        "model": escape(printer.model),
                        "printSystem": escape(printer.print_system),
                        "printerState": escape(printer.printer_state.name),
                        "stateReasons": escape(
                            ", ".join(printer.state_reasons) or "None"
                        ),
                        "options": "\n".join(
                            f"<li>{escape(k)}: {v_html}</li>"
                            for k, v_html in sorted(options_html.items())
                        )
                        or "None",
                    }
                )
        response.status = falcon.HTTP_200
        response.content_type = "text/html"
        response.text = r"""
            <!DOCTYPE html>
            <html>
            <head>
              <title>Short Story Print Server</title>
              <script>
                function addDomain() {
                  const origin = prompt("Enter the domain to authorize. Include the https:// prefix:");
                  if (origin.trim()) {
                    fetch('/domains/submit', {
                      method: 'POST',
                      headers: {'Content-Type': 'application/json'},
                      body: JSON.stringify({ origin: origin })
                    }).then(r => r.json())
                      .then(j => alert(j.description || 'Domain authorized successfully.'))
                      .catch(alert);
                  }
                }
              </script>
              <style>
                body { font-family: sans-serif; margin: 0px; padding: 1em; max-width: 600px; }
                h1 { margin-top: 0; font-size: 1.5em; }
                h2 { margin-top: 2em; font-size: 1.2em; }
                #printer-list { max-width: 800px; }
                .printer { border: 1px solid #ccc; padding: 1em; margin-bottom: 1em; }
                .printer h2 { margin-top: 0; font-size: 1.2em; }
                .printer p { margin: 0.5em 0; }
                pre { background-color: #eee; padding: 0.5em; overflow-wrap: anywhere; white-space: pre-wrap; }
                .lang {
                  user-select: none; font-weight: bold; float: right; margin-left: 1em; background-color: #bbb;
                  padding: 0.2em; border-radius: 0.2em;
                }
              </style>
            </head>
            <body>
              <h1>Short Story Print Server</h1>
              <p>
                The missing JavaScript Printer API.<br/>
                The printserver is running and is accepting print requests.<br/>
                Remote access is %(remote)s.
              </p>
              <p>Version: %(version)s</p>
              <h2>Authorized Domains</h2>
              <p>Only authorized domains are allowed to print.</p>
              <button onclick="addDomain()">Add Domain</button>
              <h2>Printer List</h2>
              <div id="printer-list">%(printers)s</div>
              <h2>API Usage Examples</h2>
              <pre><span class="lang">JavaScript</span>fetch('%(api_base)s/print-job', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
        files: [{fileUrl: 'https://httpbin.org/image/png'}]
    })
}).then(r=>r.json())
  .then(r=>alert(r.description || 'Print job submitted successfully.'))
  .catch(alert);</pre>
              <pre><span class="lang">curl</span><b style="user-select: none;">$ </b>curl %(api_base)s/printers</pre>
              <pre><span class="lang">curl</span><b style="user-select: none;">$ </b>curl %(api_base)s/print-job \
    -H 'Content-Type: application/json' \
    -d '{"files": [{"fileUrl": "https://httpbin.org/image/png"}]}'</pre>
            </body>
            </html>
        """ % dict(
            version=escape(__version__),
            printers="\n".join(printers_html) or "No active printers were detected.",
            remote="<b>enabled</b> (local network)"
            if self.enable_external_access
            else "<b>disabled</b> (localhost access only)",
            api_base=escape(api_base),
        )
