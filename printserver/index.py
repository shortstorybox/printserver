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
                sizes_html = "".join(
                    "<option value='%(name)s'>%(name)s (%(width)s x %(height)s %(units)s)</option>"
                    % dict(
                        name=escape(size.name),
                        width=float(size.width),
                        height=float(size.height),
                        units=escape(size.units.value),
                    )
                    for size in printer.media_sizes
                )
                options_html = {
                    key: "".join(
                        "<option value='%(choice)s' %(selected)s>%(key)s: %(choice)s</option>"
                        % dict(
                            choice=escape(x),
                            selected="selected" if x == spec.default_choice else "",
                            key=escape(key),
                        )
                        for x in spec.choices
                    )
                    for key, spec in printer.supported_options.items()
                }
                printers_html.append(
                    """
                    <div class="printer">
                        <h2>%(name)s</h2>
                        <p><b>Name:</b> %(name)s</p>
                        <p><b>Model:</b> %(model)s</p>
                        <p><b>Print System:</b> %(print_system)s</p>
                        <p><b>Printer State:</b> %(printer_state)s</p>
                        <p><b>State Reasons:</b> %(state_reasons)s</p>
                        <p><b>Media Size:</b> <select><option/>%(sizes_html)s</select></p>
                        <p><b>Options:</b> <ul>%(options)s</ul></p>
                    </div>
                """
                    % dict(
                        name=escape(printer.name),
                        model=escape(printer.model),
                        print_system=escape(printer.print_system),
                        printer_state=escape(printer.printer_state.name),
                        state_reasons=escape(
                            ", ".join(printer.state_reasons) or "None"
                        ),
                        sizes_html=sizes_html,
                        options="\n".join(
                            "<li>%(display_name)s <select><option/>%(html)s</select></li>"
                            % dict(
                                display_name=escape(option_spec.display_name),
                                option_name=escape(option_name),
                                html=options_html[option_name],
                            )
                            for option_name, option_spec in printer.supported_options.items()
                        )
                        or "None",
                    )
                )
        response.status = falcon.HTTP_200
        response.content_type = "text/html"
        response.text = r"""<!DOCTYPE html>
            <html>
            <head>
              <title>Short Story Print Server</title>
              <script>
                function addDomain() {
                  const domainInput = document.getElementById('domain');
                  const domain = domainInput.value.toLowerCase().trim();
                  domainInput.value = '';
                  window.open(
                    '/domains/approve?origin=' + encodeURIComponent(domain),
                    'Approve Domain',
                    'width=600,height=400,scrollbars=no,resizable=no,' +
                    'menubar=no,toolbar=no,location=no,status=no'
                  );
                }
                function runTest() {
                  fetch('%(api_base)s/print-job', {
                      method: 'POST',
                      headers: {'Content-Type': 'application/json'},
                      body: JSON.stringify({
                          async: true,
                          options: {'landscape': 'true'},
                          files: [
                              {fileUrl: 'https://pdfobject.com/pdf/sample.pdf'},
                              {text: '\n\n  Hello World!', contentType: 'text/plain'}
                          ]
                      })
                  }).then(r=>r.json())
                    .then(r=>alert(r.description || r.title || 'Submitted successfully.'))
                    .catch(alert);
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
                pre button {
                  padding: 0.2em; float: right; margin-left: 1em; clear: right; margin-top: 0.3em;
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
              <form onSubmit="addDomain(); return false;">
                <input type="text" id="domain" placeholder="Domain to authorize" />
                <button>Add Domain</button>
              </form>
              <h2>API Usage Examples</h2>
              <pre><span class="lang">JavaScript</span><button onClick="runTest();">Test Run</button>fetch('%(api_base)s/print-job', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
        async: true,
        options: {'landscape': 'true'},
        files: [
            {fileUrl: 'https://pdfobject.com/pdf/sample.pdf'},
            {text: '\n\n  Hello World!', contentType: 'text/plain'}
        ]
    })
}).then(r=>r.json())
  .then(r=>alert(r.description || r.title || 'Submitted successfully.'))
  .catch(alert);</pre>
              <pre><span class="lang">curl</span><b style="user-select: none;">$ </b>curl %(api_base)s/printers</pre>
              <pre><span class="lang">curl</span><b style="user-select: none;">$ </b>curl %(api_base)s/print-job \
    -H 'Content-Type: application/json' \
    -d '{"files": [{"fileUrl": "https://pdfobject.com/pdf/sample.pdf"}]}'</pre>
              <h2>Printer List</h2>
              <div id="printer-list">%(printers)s</div>
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
