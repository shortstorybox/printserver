from .print_systems.base import PrinterSelector
from falcon import HTTPNotFound
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
            "printers": [GetPrinterApi.printer_to_json(printer) for printer in printers]
        }


class GetPrinterApi:
    @staticmethod
    def printer_to_json(printer):
        return {
            "name": printer.name,
            "id": f"{printer.print_system}:{printer.identifier}",
            "model": printer.model,
            "printerState": printer.printer_state.name.lower(),
            "stateReasons": printer.state_reasons,
            "printSystem": printer.print_system,
            "defaultMediaSize": printer.default_media_size,
            "mediaSizes": [
                {
                    "key": size.name,
                    "width": size.width,
                    "height": size.height,
                    "units": size.units.value,
                    "display_name": size.display_name,
                }
                for size in printer.media_sizes
            ],
            "supportedOptions": {
                spec.keyword: {
                    "key": spec.keyword,
                    "defaultChoice": spec.default_choice,
                    "choices": list(spec.choices),
                    "displayName": spec.display_name,
                    "displayPosition": i,
                }
                for i, spec in enumerate(printer.supported_options)
            },
        }

    def on_get(self, printer_id, request, response):
        printer = None
        system_name, _, identifier = printer_id.partition(":")
        for system in self.print_systems.supported_systems:
            if system_name == system.system_name:
                system = system
                break
        else:
            raise HTTPNotFound(
                title=f"No print system named {system_name} is enabled",
            )
        printer = system.get_printer(identifier)
        if printer is None:
            raise HTTPNotFound(
                title=f"No active printer with id: {printer_id}",
            )
        response.media = self.printer_to_json(printer)
