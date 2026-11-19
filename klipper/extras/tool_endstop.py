# Per-tool X/Y endstop support for klipper-toolchanger.
# Copyright (C) 2026 OpenAI
# This file may be distributed under the terms of the GNU GPLv3 license.

class ToolEndstop:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.tool_number = config.getint('tool', None)
        pin = config.get('pin')

        # The same physical pin is observed by the MCU endstop and buttons
        # subsystem. This is the same sharing arrangement used by tool_probe.
        ppins = self.printer.lookup_object('pins')
        ppins.allow_multi_use_pin(pin.replace('^', '').replace('!', ''))
        self.mcu_endstop = ppins.setup_pin('endstop', pin)

        buttons = self.printer.load_object(config, 'buttons')
        buttons.register_buttons([pin], self._button_handler)

        self.endstop = self.printer.load_object(config, 'tool_virtual_endstop')
        self.endstop.add_endstop(config, self)

    def _button_handler(self, eventtime, is_triggered):
        self.endstop.note_endstop_triggered(self, eventtime, is_triggered)


def load_config_prefix(config):
    return ToolEndstop(config)
