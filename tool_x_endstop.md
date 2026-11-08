# Per-tool X endstop

This patch adds a per-tool X endstop router and the corresponding active-tool
and crash-detection commands. It deliberately does not implement any probe
interface or Z-probing functionality.

The `[toolchanger]` section must be defined before `[tool_x_endstop]`. Configure
the virtual endstop before `[stepper_x]` is parsed:

```ini
[toolchanger]

[tool_x_endstop]
crash_mintime: 0.5
crash_gcode:
    RESPOND TYPE=error MSG='Toolhead X endstop crash detected'
    PAUSE

[stepper_x]
endstop_pin: tool_x_endstop:virtual

[tool T0]
detection_pin: ^EBBCan:PA15
x_endstop_pin: ^EBBCan:PA15

[tool T1]
detection_pin: ^EBB1:PA15
x_endstop_pin: ^EBB1:PA15
```

`[tool_x_endstop]` loads and registers the virtual pin chip. It must appear
before `[stepper_x]` so Klipper knows the `tool_x_endstop` pin namespace when
parsing `endstop_pin`.

Use actual MCU names. The `detection_pin` and `x_endstop_pin` for each tool
may refer to the same physical input. Pull-up and inversion should match the
wiring and the current toolchanger detection semantics.

Commands:

- `SET_ACTIVE_TOOL_X_ENDSTOP T=n`
- `DETECT_ACTIVE_TOOL_X_ENDSTOP`
- `START_TOOL_X_ENDSTOP_CRASH_DETECTION [T=n]`
- `STOP_TOOL_X_ENDSTOP_CRASH_DETECTION`

The detection command identifies the sole untriggered tool endstop. Zero or
multiple untriggered endstops clears the active route. Crash detection monitors
button transitions only for the selected tool, and runs `crash_gcode` if the
input remains triggered for `crash_mintime`.

X homing is rejected unless the toolchanger is `ready` and an active tool
endstop is selected. The endstop used by `home_start` is snapshotted until
`home_wait` completes. On toolchanger command error or Klipper shutdown, crash
monitoring is stopped and the active route is cleared. Each physical MCU
endstop receives the X stepper association to preserve Klipper's multi-MCU
homing behavior.

No `PROBE`, `QUERY_PROBE`, Z offsets, probe sessions, or Cartographer
configuration are changed.
