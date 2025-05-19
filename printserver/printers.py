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
                    "mediaSizes": [
                        {
                            "name": size.name,
                            "width": size.width,
                            "height": size.height,
                            "units": size.units.value,
                        }
                        for size in printer.media_sizes
                    ],
                    "supportedOptions": {
                        key: {
                            "displayName": spec.display_name,
                            "defaultChoice": spec.default_choice,
                            "choices": list(spec.choices),
                        }
                        for key, spec in sorted(printer.supported_options.items())
                    },
                }
                for printer in printers
            ]
        }
