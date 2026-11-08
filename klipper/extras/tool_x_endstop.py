# Per-tool X endstop and crash detection support for klipper-toolchanger.
# Copyright (C) 2026
# This file may be distributed under the terms of the GNU GPLv3 license.

class ToolXEndstop:
    """Selects one tool-mounted MCU endstop for X homing."""
    def __init__(self, config, toolchanger):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.toolhead = None
        self.toolchanger = toolchanger
        self.endstops = []
        self.tool_number_to_endstop = {}
        self.last_query = {}
        self.active_tool_endstop = None
        self.active_tool = None
        self.active_tool_number = -1
        self.crash_detection_active = False
        self.crash_lasttime = 0.
        self.crash_mintime = config.getfloat('crash_mintime', 0.5, above=0.)
        self.gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.crash_gcode = self.gcode_macro.load_template(
            config, 'crash_gcode', '')
        self.router = XEndstopRouter(self.printer, toolchanger)
        self.printer.lookup_object('pins').register_chip(
            'tool_x_endstop', self.router)
        toolchanger.tool_x_endstop = self
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command(
            'SET_ACTIVE_TOOL_X_ENDSTOP', self.cmd_SET_ACTIVE_TOOL_X_ENDSTOP,
            desc='Set the tool endstop that will act as the X endstop')
        self.gcode.register_command(
            'DETECT_ACTIVE_TOOL_X_ENDSTOP', self.cmd_DETECT_ACTIVE_TOOL_X_ENDSTOP,
            desc='Detect the active tool from its untriggered X endstop')
        self.gcode.register_command(
            'START_TOOL_X_ENDSTOP_CRASH_DETECTION',
            self.cmd_START_TOOL_X_ENDSTOP_CRASH_DETECTION,
            desc='Start detecting tool crashes using the active X endstop')
        self.gcode.register_command(
            'STOP_TOOL_X_ENDSTOP_CRASH_DETECTION',
            self.cmd_STOP_TOOL_X_ENDSTOP_CRASH_DETECTION,
            desc='Stop detecting tool crashes')
        self.printer.register_event_handler('klippy:connect', self._handle_connect)

    def _handle_connect(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        self._detect_active_tool()

    def add_endstop(self, tool, mcu_endstop):
        old_entry = next((entry for entry in self.endstops
                          if entry[0] is tool), None)
        if old_entry is not None:
            self.endstops.remove(old_entry)
            self.tool_number_to_endstop.pop(old_entry[0].tool_number, None)
        existing = self.tool_number_to_endstop.get(tool.tool_number)
        if existing is not None and existing[0] is not tool:
            raise self.printer.config_error(
                'Duplicate X endstop tool number %d' % (tool.tool_number,))
        self.endstops.append((tool, mcu_endstop))
        self.tool_number_to_endstop[tool.tool_number] = (tool, mcu_endstop)
        self.router.add_mcu(mcu_endstop)

    def set_active_tool(self, tool):
        if self.active_tool is tool:
            return
        if self.router.homing_mcu is not None and tool is not None:
            raise self.printer.command_error(
                'Cannot change active X endstop while X homing is in progress')
        self.active_tool = tool
        if tool is None:
            self.stop_crash_detection()
            self.active_tool_endstop = None
            self.active_tool_number = -1
            self.router.set_active_mcu(None)
            return
        match = next((entry for entry in self.endstops if entry[0] is tool), None)
        if match is None:
            self.active_tool_endstop = None
            self.active_tool_number = -1
            self.router.set_active_mcu(None)
            return
        self.active_tool_endstop = match[1]
        self.active_tool_number = tool.tool_number
        self.router.set_active_mcu(match[1])

    def _query_open_tools(self):
        if self.toolhead is None:
            raise self.printer.command_error(
                'Cannot detect active X endstop before Klipper connects')
        print_time = self.toolhead.get_last_move_time()
        self.last_query.clear()
        candidates = []
        for tool, endstop in self.endstops:
            triggered = endstop.query_endstop(print_time)
            self.last_query[tool.tool_number] = triggered
            if not triggered:
                candidates.append(tool)
        return candidates

    def _describe_detection_issue(self, candidates):
        if not candidates:
            return 'All tool X endstops are triggered'
        return 'Multiple tool X endstops are not triggered: %s' % (
            [tool.name for tool in candidates],)

    def _detect_active_tool(self):
        candidates = self._query_open_tools()
        if len(candidates) == 1:
            self.set_active_tool(candidates[0])
        else:
            self.set_active_tool(None)

    def _require_active_tool(self, gcmd):
        if self.active_tool is None or self.active_tool_endstop is None:
            raise gcmd.error('No active tool X endstop')
        if self.toolchanger.status != 'ready':
            raise gcmd.error(
                'Cannot use X endstop: toolchanger status is %s' %
                (self.toolchanger.status,))

    def cmd_SET_ACTIVE_TOOL_X_ENDSTOP(self, gcmd):
        tool_nr = gcmd.get_int('T')
        entry = self.tool_number_to_endstop.get(tool_nr)
        if entry is None:
            raise gcmd.error('No X endstop configured for tool %d' % (tool_nr,))
        self.set_active_tool(entry[0])

    def cmd_DETECT_ACTIVE_TOOL_X_ENDSTOP(self, gcmd):
        candidates = self._query_open_tools()
        if len(candidates) == 1:
            self.set_active_tool(candidates[0])
            gcmd.respond_info('Found active tool X endstop: %s' %
                              (candidates[0].name,))
        else:
            self.set_active_tool(None)
            gcmd.respond_info(self._describe_detection_issue(candidates))

    def cmd_START_TOOL_X_ENDSTOP_CRASH_DETECTION(self, gcmd):
        # Querying endstops flushes the queued print moves before monitoring.
        self.cmd_DETECT_ACTIVE_TOOL_X_ENDSTOP(gcmd)
        self._require_active_tool(gcmd)
        expected = gcmd.get_int('T', self.active_tool_number)
        if expected != self.active_tool_number:
            raise gcmd.error(
                'Expected tool %d is not the detected active tool %d' %
                (expected, self.active_tool_number))
        self.crash_lasttime = 0.
        self.crash_detection_active = True

    def cmd_STOP_TOOL_X_ENDSTOP_CRASH_DETECTION(self, gcmd):
        self.toolhead.register_lookahead_callback(
            lambda _: self.stop_crash_detection())

    def stop_crash_detection(self):
        self.crash_lasttime = 0.
        self.crash_detection_active = False

    def note_endstop_triggered(self, tool, eventtime, is_triggered):
        if not self.crash_detection_active or tool is not self.active_tool:
            return
        if is_triggered:
            self.crash_lasttime = eventtime
            self.reactor.register_callback(
                lambda _: self._endstop_triggered_delayed(eventtime),
                eventtime + self.crash_mintime)
        else:
            self.crash_lasttime = 0.

    def _endstop_triggered_delayed(self, expected_eventtime):
        if self.crash_lasttime != expected_eventtime:
            return
        if self.crash_detection_active:
            self.crash_detection_active = False
            self.crash_gcode.run_gcode_from_command()

    def get_status(self, eventtime):
        return {
            'active_tool_x_endstop': self.active_tool.name if self.active_tool else None,
            'active_tool_number': self.active_tool_number,
            'last_tools_query': dict(self.last_query),
            'crash_detection_active': self.crash_detection_active,
        }


class XEndstopRouter:
    """Klipper endstop interface forwarding to the selected tool MCU."""
    def __init__(self, printer, toolchanger):
        self.printer = printer
        self.toolchanger = toolchanger
        self.active_mcu = None
        self.homing_mcu = None
        self.mcus = []
        self.steppers = []

    def setup_pin(self, pin_type, pin_params):
        pins = self.printer.lookup_object('pins')
        if pin_type != 'endstop' or pin_params['pin'] != 'virtual':
            raise pins.error("tool_x_endstop only supports endstop_pin: tool_x_endstop:virtual")
        if pin_params['invert'] or pin_params['pullup']:
            raise pins.error("tool_x_endstop:virtual does not accept invert or pullup")
        return self

    def add_mcu(self, mcu_endstop):
        if mcu_endstop in self.mcus:
            return
        self.mcus.append(mcu_endstop)
        for stepper in self.steppers:
            mcu_endstop.add_stepper(stepper)

    def set_active_mcu(self, mcu_endstop):
        self.active_mcu = mcu_endstop

    def _get_active(self):
        if self.active_mcu is None:
            raise self.printer.command_error(
                'Cannot use X endstop: no active tool endstop')
        return self.active_mcu

    def get_mcu(self):
        return self._get_active().get_mcu()

    def add_stepper(self, stepper):
        if stepper not in self.steppers:
            self.steppers.append(stepper)
            for mcu_endstop in self.mcus:
                mcu_endstop.add_stepper(stepper)

    def get_steppers(self):
        return list(self.steppers)

    def home_start(self, *args, **kwargs):
        if self.toolchanger is None:
            raise self.printer.command_error(
                "Toolchanger is not initialized")
        if self.toolchanger.status != 'ready':
            raise self.printer.command_error(
                'Cannot home X: toolchanger status is %s' %
                (self.toolchanger.status,))
        if self.homing_mcu is not None:
            raise self.printer.command_error(
                'X endstop homing is already in progress')
        self.homing_mcu = self._get_active()
        try:
            return self.homing_mcu.home_start(*args, **kwargs)
        except Exception:
            self.homing_mcu = None
            raise

    def home_wait(self, home_end_time):
        mcu_endstop = self.homing_mcu
        if mcu_endstop is None:
            raise self.printer.command_error(
                'X endstop home_wait without home_start')
        try:
            return mcu_endstop.home_wait(home_end_time)
        finally:
            self.homing_mcu = None

    def query_endstop(self, print_time):
        return self._get_active().query_endstop(print_time)

def load_config(config):
    printer = config.get_printer()
    toolchanger = printer.lookup_object('toolchanger')
    return ToolXEndstop(config, toolchanger)
