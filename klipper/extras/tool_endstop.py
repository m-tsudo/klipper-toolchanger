# Per-tool X/Y endstop routing and crash detection support.
# Copyright (C) 2026
# This file may be distributed under the terms of the GNU GPLv3 license.

import pins


class ToolEndstop:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name()
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.poll_interval = config.getfloat('poll_interval', 0.02, above=0.)
        self.crash_mintime = config.getfloat('crash_mintime', 0.5, above=0.)
        self.crash_gcode = self.gcode_macro.load_template(
            config, 'crash_gcode', '')

        self.tool_endstops = {}
        self.active_tool = None
        self.last_query = {}
        self.crash_detection_active = False
        self.crash_lasttime = 0.
        self.toolhead = None
        self.crash_timer = self.reactor.register_timer(
            self._poll_crash, self.reactor.NEVER)

        self.router = EndstopRouter(self.printer, self)
        self.virtual_pin_claimed = False
        self.printer.lookup_object('pins').register_chip('tool_endstop', self)
        self.printer.register_event_handler('klippy:ready',
                                            self._handle_ready)
        self.printer.register_event_handler('gcode:command_error',
                                            self._handle_command_error)
        self.printer.register_event_handler('klippy:shutdown',
                                            self._handle_shutdown)

        self.gcode.register_command(
            'SET_ACTIVE_TOOL_ENDSTOP', self.cmd_SET_ACTIVE_TOOL_ENDSTOP,
            desc='Set the tool endstop that will act as the X/Y endstop')
        self.gcode.register_command(
            'DETECT_ACTIVE_TOOL_ENDSTOP', self.cmd_DETECT_ACTIVE_TOOL_ENDSTOP,
            desc='Detect the active tool by its endstop state')
        self.gcode.register_command(
            'START_TOOL_ENDSTOP_CRASH_DETECTION',
            self.cmd_START_TOOL_ENDSTOP_CRASH_DETECTION,
            desc='Start detecting tool crashes')
        self.gcode.register_command(
            'STOP_TOOL_ENDSTOP_CRASH_DETECTION',
            self.cmd_STOP_TOOL_ENDSTOP_CRASH_DETECTION,
            desc='Stop detecting tool crashes')

    def add_tool(self, config, tool, mcu_endstop):
        if tool in self.tool_endstops:
            raise config.error('Duplicate endstop for tool %s' % (tool.name,))
        self.tool_endstops[tool] = mcu_endstop
        self.router.add_endstop(mcu_endstop)

    def set_active_tool(self, tool):
        self.active_tool = tool
        self.router.set_active_endstop(
            self.tool_endstops.get(tool) if tool is not None else None)

    def _selected_tool(self):
        # Resolve the toolchanger at use time; do not cache it during
        # config loading. An explicit SET_ACTIVE_TOOL_ENDSTOP selection
        # takes precedence until the toolchanger selects another tool.
        toolchanger = self.printer.lookup_object('toolchanger', None)
        selected = (toolchanger.get_selected_tool()
                    if toolchanger is not None else None)
        if self.active_tool in self.tool_endstops:
            return self.active_tool
        if selected in self.tool_endstops:
            return selected
        return None

    def _selected_endstop(self):
        tool = self._selected_tool()
        if tool is None:
            raise self.printer.command_error(
                'Cannot use tool endstop - no active tool with endstop_pin')
        return tool, self.tool_endstops[tool]

    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        # Preserve explicit toolchanger selection if one already exists;
        # otherwise attempt the same unique-open-endstop detection as the G-code.
        toolchanger = self.printer.lookup_object('toolchanger', None)
        selected = toolchanger.get_selected_tool() if toolchanger else None
        if selected is not None:
            self.set_active_tool(selected)
        else:
            self._detect_active_tool()

    def _handle_command_error(self):
        self.set_active_tool(None)
        self.stop_crash_detection()

    def _handle_shutdown(self):
        self.set_active_tool(None)
        self.stop_crash_detection()

    def _query_open_tools(self):
        toolhead = self.printer.lookup_object('toolhead')
        print_time = toolhead.get_last_move_time()
        self.last_query.clear()
        candidates = []
        for tool, mcu_endstop in self.tool_endstops.items():
            triggered = bool(mcu_endstop.query_endstop(print_time))
            key = tool.tool_number if tool.tool_number >= 0 else tool.name
            self.last_query[key] = triggered
            if not triggered:
                candidates.append(tool)
        return candidates

    def _describe_detection(self, candidates):
        if len(candidates) == 1:
            return 'OK'
        if not candidates:
            return 'All tool endstops are triggered'
        return 'Multiple tool endstops are open: %s' % (
            [tool.name for tool in candidates],)

    def _detect_active_tool(self):
        candidates = self._query_open_tools()
        if len(candidates) == 1:
            self.set_active_tool(candidates[0])
            return candidates[0]
        self.set_active_tool(None)
        return None

    def cmd_SET_ACTIVE_TOOL_ENDSTOP(self, gcmd):
        tool_number = gcmd.get_int('T')
        toolchanger = self.printer.lookup_object('toolchanger')
        tool = toolchanger.lookup_tool(tool_number)
        if tool is None:
            raise gcmd.error('SET_ACTIVE_TOOL_ENDSTOP: tool T%d not found'
                             % (tool_number,))
        if tool not in self.tool_endstops:
            raise gcmd.error('SET_ACTIVE_TOOL_ENDSTOP: %s has no endstop_pin'
                             % (tool.name,))
        self.set_active_tool(tool)

    def cmd_DETECT_ACTIVE_TOOL_ENDSTOP(self, gcmd):
        tool = self._detect_active_tool()
        if tool is not None:
            gcmd.respond_info('Found active tool endstop: %s' % (tool.name,))
            return
        candidates = [
            t for t in self.tool_endstops
            if not self.last_query.get(
                t.tool_number if t.tool_number >= 0 else t.name, True)]
        gcmd.respond_info(self._describe_detection(candidates))

    def cmd_START_TOOL_ENDSTOP_CRASH_DETECTION(self, gcmd):
        tool = self._detect_active_tool()
        if tool is None:
            raise gcmd.error(
                'Cannot start endstop crash detection: %s' %
                (self._describe_detection([
                    t for t, endstop in self.tool_endstops.items()
                    if not self.last_query.get(
                        t.tool_number if t.tool_number >= 0 else t.name,
                        True)]),))
        gcmd.respond_info('Found active tool endstop: %s' % (tool.name,))
        expected_tool_number = gcmd.get_int('T', tool.tool_number)
        if expected_tool_number != tool.tool_number:
            raise gcmd.error(
                'Cannot start endstop crash detection - expected tool not active')
        self.crash_lasttime = 0.
        self.crash_detection_active = True
        self.reactor.update_timer(
            self.crash_timer, self.reactor.monotonic() + self.poll_interval)

    def cmd_STOP_TOOL_ENDSTOP_CRASH_DETECTION(self, gcmd):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.register_lookahead_callback(
            lambda _: self.stop_crash_detection())

    def stop_crash_detection(self):
        self.crash_lasttime = 0.
        self.crash_detection_active = False
        self.reactor.update_timer(self.crash_timer, self.reactor.NEVER)

    def _poll_crash(self, eventtime):
        if not self.crash_detection_active:
            return self.reactor.NEVER
        try:
            tool, mcu_endstop = self._selected_endstop()
            print_time = mcu_endstop.get_mcu().estimated_print_time(
                eventtime)
            triggered = bool(mcu_endstop.query_endstop(print_time))
        except self.printer.command_error:
            self.stop_crash_detection()
            return self.reactor.NEVER
        if triggered:
            if not self.crash_lasttime:
                self.crash_lasttime = eventtime
            elif eventtime - self.crash_lasttime >= self.crash_mintime:
                self.stop_crash_detection()
                self.crash_gcode.run_gcode_from_command()
                return self.reactor.NEVER
        else:
            self.crash_lasttime = 0.
        return eventtime + self.poll_interval

    def get_status(self, eventtime):
        tool = self._selected_tool()
        return {
            'active_tool_number': tool.tool_number if tool else -1,
            'active_tool_endstop': tool.name if tool else None,
            'last_tools_query': dict(self.last_query),
            'crash_detection_active': self.crash_detection_active,
        }

    # PrinterPins chip interface for tool_endstop:virtual
    def setup_pin(self, pin_type, pin_params):
        if pin_type != 'endstop' or pin_params['pin'] != 'virtual':
            raise pins.error(
                'tool_endstop virtual pin is only valid as an endstop pin')
        if pin_params['invert'] or pin_params['pullup']:
            raise pins.error(
                'Can not pullup/invert tool_endstop:virtual')
        if self.virtual_pin_claimed:
            raise pins.error(
                'tool_endstop:virtual can be used by only one axis')
        self.virtual_pin_claimed = True
        return self.router


class EndstopRouter:
    def __init__(self, printer, owner):
        self.printer = printer
        self.owner = owner
        self.endstops = []
        self.steppers = []
        self.active_endstop = None

    def add_endstop(self, mcu_endstop):
        self.endstops.append(mcu_endstop)
        for stepper in self.steppers:
            mcu_endstop.add_stepper(stepper)

    def set_active_endstop(self, mcu_endstop):
        self.active_endstop = mcu_endstop

    def _require_active(self):
        if self.active_endstop is None:
            raise self.printer.command_error(
                'Cannot home - no active tool endstop')
        return self.active_endstop

    def get_mcu(self):
        return self._require_active().get_mcu()

    def add_stepper(self, stepper):
        if stepper in self.steppers:
            return
        self.steppers.append(stepper)
        for mcu_endstop in self.endstops:
            mcu_endstop.add_stepper(stepper)

    def get_steppers(self):
        return list(self.steppers)

    def home_start(self, print_time, sample_time, sample_count, rest_time,
                   triggered=True):
        return self._require_active().home_start(
            print_time, sample_time, sample_count, rest_time,
            triggered=triggered)

    def home_wait(self, home_end_time):
        return self._require_active().home_wait(home_end_time)

    def query_endstop(self, print_time):
        return self._require_active().query_endstop(print_time)


def load_config(config):
    return ToolEndstop(config)
