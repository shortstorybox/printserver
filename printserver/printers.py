from .print_systems.base import PrinterSelector
from .print_systems import PrintSystemProvider


class ListPrintersApi:
    def __init__(self, print_systems: PrintSystemProvider):
        self.print_systems = print_systems

    def on_get(self, request, response):
        printer_selector = PrinterSelector.parse(request.params)

        printers = []
        for system in self.print_systems.supported_systems:
            printers.extend(system.get_printers(printer_selector))
        response.media = {
            "printers": [
                {
                    "name": printer.name,
                    "model": printer.model,
                    "printerState": printer.printer_state.name.lower(),
                    "stateReasons": printer.state_reasons,
                    "printSystem": printer.print_system,
                    "supportedOptions": printer.supported_options,
                    "defaultOptions": printer.default_options,
                }
                for printer in printers
            ]
        }
