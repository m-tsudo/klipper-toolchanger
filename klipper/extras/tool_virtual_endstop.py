# Virtual X/Y endstop routing for klipper-toolchanger.
# Copyright (C) 2026 OpenAI
# This file may be distributed under the terms of the GNU GPLv3 license.

import pins


class ToolVirtualEndstop:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name()
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.crash_mintime = config.getfloat('crash_mintime', 0.5, above=0.)
        self.crash_gcode = self.gcode_macro.load_template(
            config, 'crash_gcode', '')

        self.endstops = {}
        self.last_query = {}
        self.crash_detection_active = False
        self.crash_lasttime = 0.
        self.toolhead = None
        self.router = EndstopRouter(self.printer, self)
        self.printer.lookup_object('pins').register_chip('tool_endstop', self)
        self.printer.register_event_handler('klippy:connect',
                                            self._handle_connect)

        self.gcode.register_command(
            'SET_ACTIVE_TOOL_ENDSTOP', self.cmd_SET_ACTIVE_TOOL_ENDSTOP,
            desc='Set the tool endstop used for X/Y homing')
        self.gcode.register_command(
            'DETECT_ACTIVE_TOOL_ENDSTOP', self.cmd_DETECT_ACTIVE_TOOL_ENDSTOP,
            desc='Detect the active tool by querying its endstop')
        self.gcode.register_command(
            'START_TOOL_ENDSTOP_CRASH_DETECTION',
            self.cmd_START_TOOL_ENDSTOP_CRASH_DETECTION,
            desc='Start tool endstop crash detection')
        self.gcode.register_command(
            'STOP_TOOL_ENDSTOP_CRASH_DETECTION',
            self.cmd_STOP_TOOL_ENDSTOP_CRASH_DETECTION,
            desc='Stop tool endstop crash detection')

    def add_endstop(self, config, tool_endstop):
        number = tool_endstop.tool_number
        if number in self.endstops:
            raise config.error('Duplicate tool endstop for tool %d'
                               % (number,))
        self.endstops[number] = tool_endstop
        self.router.add_endstop(tool_endstop.mcu_endstop)

    def _get_selected_tool_number(self):
        toolchanger = self.printer.lookup_object('toolchanger', None)
        if toolchanger is None:
            return None
        tool = toolchanger.get_selected_tool()
        if tool is None or tool.tool_number < 0:
            return None
        return tool.tool_number

    def _selected_endstop(self):
        number = self._get_selected_tool_number()
        if number is None or number not in self.endstops:
            raise self.printer.command_error(
                'No tool endstop for the selected tool')
        return self.endstops[number]

    def _handle_connect(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        number = self._get_selected_tool_number()
        if number in self.endstops:
            self.router.set_active_endstop(self.endstops[number].mcu_endstop)
        elif self.endstops:
            self._detect_active_tool_endstop()

    def _query_open_tools(self):
        print_time = self.toolhead.get_last_move_time()
        self.last_query.clear()
        candidates = []
        for number, tool_endstop in self.endstops.items():
            triggered = bool(tool_endstop.mcu_endstop.query_endstop(print_time))
            self.last_query[number] = triggered
            if not triggered:
                candidates.append((number, tool_endstop))
        return candidates

    def _describe_detection(self, candidates):
        if len(candidates) == 0:
            return 'All tool endstops are triggered'
        if len(candidates) == 1:
            return 'OK'
        return 'Multiple tool endstops are open: %s' % ', '.join(
            ['T%d' % (number,) for number, endstop in candidates])

    def _detect_active_tool_endstop(self):
        candidates = self._query_open_tools()
        if len(candidates) == 1:
            number, tool_endstop = candidates[0]
            self.router.set_active_endstop(tool_endstop.mcu_endstop)
            return number
        self.router.set_active_endstop(None)
        return None

    def cmd_SET_ACTIVE_TOOL_ENDSTOP(self, gcmd):
        number = gcmd.get_int('T', minval=0)
        if number not in self.endstops:
            raise gcmd.error('No tool endstop configured for T%d'
                             % (number,))
        selected = self._get_selected_tool_number()
        if selected is not None and selected != number:
            raise gcmd.error('Selected tool is T%d; cannot activate T%d endstop'
                             % (selected, number))
        self.router.set_active_endstop(self.endstops[number].mcu_endstop)
        gcmd.respond_info('Active tool endstop: T%d' % (number,))

    def cmd_DETECT_ACTIVE_TOOL_ENDSTOP(self, gcmd):
        number = self._detect_active_tool_endstop()
        if number is None:
            candidates = self._query_open_tools()
            gcmd.respond_info(self._describe_detection(candidates))
        else:
            gcmd.respond_info('Detected active tool endstop: T%d' % (number,))

    def cmd_START_TOOL_ENDSTOP_CRASH_DETECTION(self, gcmd):
        number = self._detect_active_tool_endstop()
        expected = gcmd.get_int('T', number, minval=0)
        if number is None:
            raise gcmd.error('Cannot start crash detection: active tool not detected')
        if expected != number:
            raise gcmd.error('Cannot start crash detection: expected T%d, detected T%d'
                             % (expected, number))
        self.crash_lasttime = 0.
        self.crash_detection_active = True

    def cmd_STOP_TOOL_ENDSTOP_CRASH_DETECTION(self, gcmd):
        self.toolhead.register_lookahead_callback(
            lambda print_time: self.stop_crash_detection())

    def stop_crash_detection(self):
        self.crash_lasttime = 0.
        self.crash_detection_active = False

    def note_endstop_triggered(self, tool_endstop, eventtime, is_triggered):
        if not self.crash_detection_active:
            return
        number = self._get_selected_tool_number()
        if number != tool_endstop.tool_number:
            return
        if is_triggered:
            self.crash_lasttime = eventtime
            self.reactor.register_callback(
                lambda et: self._endstop_triggered_delayed(eventtime),
                eventtime + self.crash_mintime)
        else:
            self.crash_lasttime = 0.

    def _endstop_triggered_delayed(self, expected_eventtime):
        if self.crash_lasttime != expected_eventtime:
            return
        if self.crash_detection_active:
            self.crash_detection_active = False
            self.crash_gcode.run_gcode_from_command()

    def setup_pin(self, pin_type, pin_params):
        if (pin_type != 'endstop' or pin_params['pin'] != 'virtual'
                or pin_params['invert'] or pin_params['pullup']):
            raise pins.error('tool_endstop:virtual is only valid as an '
                             'uninverted, non-pullup endstop pin')
        return self.router

    def get_status(self, eventtime):
        number = self._get_selected_tool_number()
        return {'active_tool_number': number,
                'last_query': dict(self.last_query),
                'crash_detection_active': self.crash_detection_active}


class EndstopRouter:
    def __init__(self, printer, manager):
        self.printer = printer
        self.manager = manager
        self.endstops = []
        self.steppers = []
        self.active_endstop = None
        self.homing_endstop = None

    def add_endstop(self, mcu_endstop):
        self.endstops.append(mcu_endstop)
        for stepper in self.steppers:
            mcu_endstop.add_stepper(stepper)

    def set_active_endstop(self, mcu_endstop):
        self.active_endstop = mcu_endstop

    def _get_active(self):
        # Resolve from the toolchanger at use time so a tool change does not
        # require modifying tool.py or adding a tool-selection callback there.
        number = self.manager._get_selected_tool_number()
        if number in self.manager.endstops:
            return self.manager.endstops[number].mcu_endstop
        if self.active_endstop is not None:
            return self.active_endstop
        raise self.printer.command_error(
            'Cannot home: no active tool virtual endstop')

    def get_mcu(self):
        return self._get_active().get_mcu()

    def add_stepper(self, stepper):
        if stepper not in self.steppers:
            self.steppers.append(stepper)
            for endstop in self.endstops:
                endstop.add_stepper(stepper)

    def get_steppers(self):
        return list(self.steppers)

    def home_start(self, print_time, sample_time, sample_count, rest_time,
                   triggered=True):
        self.homing_endstop = self._get_active()
        return self.homing_endstop.home_start(
            print_time, sample_time, sample_count, rest_time,
            triggered=triggered)

    def home_wait(self, home_end_time):
        if self.homing_endstop is None:
            raise self.printer.command_error(
                'Internal error: tool endstop home_wait without home_start')
        endstop = self.homing_endstop
        self.homing_endstop = None
        return endstop.home_wait(home_end_time)

    def query_endstop(self, print_time):
        return self._get_active().query_endstop(print_time)


def load_config(config):
    return ToolVirtualEndstop(config)
