# Tool virtual endstop

`tool_virtual_endstop` provides one physical X/Y endstop per tool and routes a
single Klipper virtual endstop to the selected tool. The physical pin is also
registered with Klipper's buttons subsystem so crash detection is event-driven.

## Configuration

```ini
[tool_virtual_endstop]
crash_mintime: 0.5
crash_gcode:
    PAUSE

[tool_endstop T0]
tool: 0
pin: ^EBBCan:PA15

[tool_endstop T1]
tool: 1
pin: ^EBBCan2:PA15

[stepper_x]
endstop_pin: tool_endstop:virtual
```

Use the same virtual pin for `stepper_y` instead if the physical tool endstop
is used for Y homing. The virtual pin is intended for the axis that uses this
physical switch; it is not a pair of independent X and Y switches.

Each `[tool_endstop ...]` section uses the `tool` option, matching
the `tool_number` in the corresponding `[tool ...]` section. Each
physical pin is shared by the MCU endstop and buttons subsystem, following the
same pin-sharing pattern as `tool_probe`.

The manager maintains `active_tool_number`, following the same state
management model as `tool_probe_endstop`. It is updated by explicit selection
or by detection when exactly one configured endstop is open. Set `tool` to
the corresponding tool number when you want numbered selection and status.

## Status

The active tool number is available as `printer.tool_virtual_endstop.active_tool_number`.
It is `-1` when no numbered endstop is active.

## Commands

- `SET_ACTIVE_TOOL_ENDSTOP T=<number>`: select the configured endstop when no
  conflicting tool is selected.
- `DETECT_ACTIVE_TOOL_ENDSTOP`: detect the only open endstop.
- `START_TOOL_ENDSTOP_CRASH_DETECTION [T=<number>]`: detect the active endstop
  and start event-driven crash detection.
- `STOP_TOOL_ENDSTOP_CRASH_DETECTION`: stop crash detection after queued moves.

Crash detection runs the configured `crash_gcode` if the selected tool's
endstop remains triggered for `crash_mintime`.
