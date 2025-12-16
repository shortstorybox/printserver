import platform
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
                    "<option value='%(name)s' %(selected)s>%(name)s (%(width)g x %(height)g %(units)s)</option>"
                    % dict(
                        name=escape(size.name),
                        width=float(size.width),
                        height=float(size.height),
                        units=escape(size.units.value),
                        selected="selected"
                        if size.name == printer.default_media_size
                        else "",
                    )
                    for size in printer.media_sizes
                )
                options_html = {
                    spec.keyword: "".join(
                        "<option value='%(choice)s' %(selected)s>%(keyword)s: %(choice)s</option>"
                        % dict(
                            choice=escape(x),
                            selected="selected" if x == spec.default_choice else "",
                            keyword=escape(spec.keyword),
                        )
                        for x in spec.choices
                    )
                    for spec in printer.supported_options
                }
                printers_html.append(
                    """
                    <form class="printer" onsubmit="event.preventDefault(); printFile(this)">
                      <input type="hidden" name="printerSelector[name]" value="%(name)s" />
                      <input type="hidden" name="printerSelector[printSystem]" value="%(print_system)s" />
                      <input type="hidden" name="printerSelector[namePrefix]" value="%(name)s" />
                      <input type="hidden" name="printerSelector[modelPrefix]" value="%(model)s" />

                      <h2>%(name)s</h2>
                      <p><b>Name:</b> %(name)s</p>
                      <p><b>Model:</b> %(model)s</p>
                      <p><b>Print System:</b> %(print_system)s</p>
                      <p><b>Printer State:</b> %(printer_state)s</p>
                      <p><b>State Reasons:</b> %(state_reasons)s</p>
                      <p>
                        <b>Media:</b>
                        <ul>
                          <li>Size
                            <select name="media[size][name]" class="media-size" onchange="updateMediaSize(this)">
                              <option/>
                              <option value="custom">custom</option>
                              %(sizes_html)s
                            </select>
                          </li>
                        </ul>
                      </p>
                      <p><b>Options:</b> <ul>%(options)s</ul></p>
                      <!-- file input -->
                      <p>
                        <b>File:</b>
                        <input type="file" name="files" accept="application/pdf" multiple required />
                      </p>
                      <p>
                        <button>Print</button>
                      </p>
                      <div class="print-job" style="display: none">
                        <div>Submitted with Status: <span class="job-status"></span></div>
                        <div>View Print-Job: <a target="_blank" href=""></a></div>
                        <pre class="warnings"></pre>
                      </div>
                    </form>
                """
                    % dict(
                        name=escape(printer.name),
                        model=escape(printer.model),
                        print_system=escape(printer.print_system),
                        printer_state=escape(printer.printer_state.name.lower()),
                        state_reasons=escape(
                            ", ".join(printer.state_reasons) or "None"
                        ),
                        sizes_html=sizes_html,
                        options="\n".join(
                            "<li>%(display_name)s <select name='options[%(key)s]'><option/>%(html)s</select></li>"
                            % dict(
                                key=escape(spec.keyword),
                                display_name=escape(spec.display_name),
                                html=options_html[spec.keyword],
                            )
                            for spec in printer.supported_options
                        )
                        or "None",
                    )
                )
        os_name = platform.system()
        if os_name == "Linux":
            virtual_printer_html = """
                <code>sudo apt-get install printer-driver-cups-pdf</code>
                (saves output to ~/PDF).
            """
        elif os_name == "Darwin":
            virtual_printer_html = """
                <a href="https://github.com/rodyager/RWTS-PDFwriter/releases/latest">RWTS-PDFWriter</a>
                or <code>brew install rwts-pdfwriter</code>
            """
        elif os_name == "Windows":
            virtual_printer_html = """
                <a href="https://www.pdfforge.org/pdfcreator">PDFCreator</a>.
            """
        else:
            virtual_printer_html = """
                one of
                <a href="https://github.com/rodyager/RWTS-PDFwriter/releases/latest">RWTS-PDFWriter</a>
                (macOS), <a href="https://www.pdfforge.org/pdfcreator">PDFCreator</a>
                (Windows), or <code>sudo apt-get install printer-driver-cups-pdf</code> (Linux).
            """
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
                      options: {'landscape': 'true'},
                      files: [
                        {fileUrl: 'https://pdfobject.com/pdf/sample.pdf'},
                        {text: '\n\n  Hello World!', contentType: 'text/plain'}
                      ]
                    })
                  }).then(r=>r.json())
                    .then(r=>alert(r.title || r.warnings || 'Printing...'))
                    .catch(alert);
                }
                function updateMediaSize(selector) {
                  const previous = selector.parentElement.querySelector('.custom-size');
                  if (previous) { previous.remove(); }
                  if (selector.value == 'custom') {
                    const customSize = document.createElement('span');
                    customSize.className = 'custom-size';
                    customSize.innerHTML = `
                      <label>Width:</label>
                      <input type="number" name="media[size][width]" min="0.1" step="any" value="8.5" required />
                      <label>Height:</label>
                      <input type="number" name="media[size][height]" min="0.1" step="any" value="11" required />
                      <select name="media[size][units]">
                        <option selected value="inches">inches</option>
                        <option value="mm">mm</option>
                        <option value="points">points</option>
                      </select>
                    `;
                    selector.parentElement.appendChild(customSize);
                  }
                }
                function printFile(form) {
                  const warnings = form.querySelector('.warnings');
                  warnings.innerText = '';

                  const printJob = form.querySelector('.print-job');
                  printJob.style.display = 'none';
                  printJob.querySelector('.job-status').innerText = '';

                  fetch('%(api_base)s/print-job', {
                    method: 'POST',
                    body: new FormData(form),
                  }).then(r => {
                    if (!r.ok) {
                      return r.json().then(j => Promise.reject(new Exception(j.title || 'Unknown Error')));
                    }
                    return r.json().then(j => {
                      printJob.style.display = 'block';
                      warnings.innerText = j.warnings || '';
                      printJob.querySelector('.job-status').innerText = j.jobState;
                      if (j.jobId) {
                        printJob.querySelector('a').style.display = 'inline';
                        printJob.querySelector('a').href = '/print-job/' + j.jobId;
                        printJob.querySelector('a').innerText = '/print-job/' + j.jobId;
                      } else {
                        printJob.querySelector('a').style.display = 'none';
                      }
                    });
                  }).catch(error => {
                    warnings.innerText = error.message || 'Unknown Error';
                    printJob.style.display = 'block';
                  });
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
                  user-select: none; font-weight: bold; float: right; margin-left: 1em;
                  background-color: #bbb; padding: 0.2em; border-radius: 0.2em;
                }
                pre > button.example {
                  padding: 0.2em; float: right; margin-left: 1em; clear: right; margin-top: 0.3em;
                }
                pre > form.example {
                  padding: 0.5em; float: right; margin-left: 1em; clear: right;
                  margin-top: 0.3em; background: white; border: 1px solid black;
                  border-radius: 3px; white-space: normal; user-select: none;
                }
                pre > form.example > input, pre > form.example > button { display: block; margin-top: 0.2em; }
                input[type=number] { width: 4em; margin: 0 0.5em; }
                input[type=number]:required:invalid { background-color: #f55; }
                .custom-size { display: block; padding-top: 5px; }
                .warnings:empty { display: none; }
                .warnings {
                  color: #f55; font-size: 0.8em; margin-top: 0.5em;
                  padding: 0.5em; border: 1px solid #f55; background-color: #fdd;
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
              <pre><span class="lang">JavaScript</span><button class="example" onClick="runTest();">Test Run</button>fetch('%(api_base)s/print-job', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
        options: {'landscape': 'true'},
        files: [
            {fileUrl: 'https://pdfobject.com/pdf/sample.pdf'},
            {text: '\n\n  Hello World!', contentType: 'text/plain'}
        ]
    })
}).then(r=>r.json())
  .then(r=>alert(r.title || r.warnings || 'Printing...'))
  .catch(alert);</pre>
              <pre><span class="lang">curl</span
                ><b style="user-select: none;">$ </b>curl %(api_base)s/printers<br/><br
                /><b style="user-select: none;">$ </b>curl %(api_base)s/print-job \
    -H 'Content-Type: application/json' \
    -d '{"files": [{"fileUrl": "https://pdfobject.com/pdf/sample.pdf"}]}'</pre>
              <pre><span class="lang">html</span
><form class="example" onsubmit="event.preventDefault(); printFiles(this)">
  <select name="options[landscape]">
    <option value="true">Landscape</option>
    <option value="false">Portrait</option>
  </select>
  <input multiple required type="file" name="files" style="width: 200px"/>
  <button>Test Run</button>
</form><script>
  function printFiles(form) {
    fetch('%(api_base)s/print-job', {
      method: 'POST',
      body: new FormData(form),
    }).then(r=>r.json())
      .then(r=>alert(r.title || r.warnings || 'Printing...'))
      .catch(alert);
  }
</script
>&lt;form onsubmit="event.preventDefault(); printFiles(this)"&gt;
  &lt;select name="options[landscape]"&gt;
    &lt;option value="true"&gt;Landscape&lt;/option&gt;
    &lt;option value="false"&gt;Portrait&lt;/option&gt;
  &lt;/select&gt;
  &lt;input multiple required type="file"
    name="files"/&gt;
  &lt;button&gt;Test Run&lt;/button&gt;
&lt;/form&gt;
&lt;script&gt;
  function printFiles(form) {
    fetch('%(api_base)s/print-job', {
       method: 'POST',
       body: new FormData(form),
    }).then(r=>r.json())
      .then(r=>alert(r.title || r.warnings || 'Printing...'))
      .catch(alert);
  }
&lt;/script&gt;</pre>
              <h2>Printer List</h2>
              <div id="printer-list">%(printers)s</div>
              <div>
              <p>
                If you need a virtual PDF printer for testing, try %(virtual_printer_html)s
              </p>
              </div>
            </body>
            </html>
        """ % dict(
            version=escape(__version__),
            printers="\n".join(printers_html)
            or '<div class="printer">No active printers were detected.</div>',
            remote="<b>enabled</b> (local network)"
            if self.enable_external_access
            else "<b>disabled</b> (localhost access only)",
            api_base=escape(api_base),
            virtual_printer_html=virtual_printer_html,
        )
