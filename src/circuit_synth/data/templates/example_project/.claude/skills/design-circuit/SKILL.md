---
name: design-circuit
description: Design a circuit from a natural-language spec using an iterative loop — plan, write circuit-synth Python, generate the KiCad schematic, simulate with ngspice, examine results, and refine until the spec's measurable criteria pass. Use whenever the user asks to design, create, or modify a circuit or schematic in this project.
---

# design-circuit — iterative circuit design loop

You are designing a circuit in a circuit-synth project. Work in numbered
iterations (max **5**). Keep an append-only `design_log.md` in the project root;
after every iteration append: iteration number, what changed, generation result,
simulation measurements, PASS/FAIL per criterion, and the next action.

## Phase 0 — SETUP (once)
- Read `circuit-synth/main.py` — it is a known-good example of the API pattern.
- Windows note: run every command with UTF-8 mode, e.g. in bash
  `PYTHONUTF8=1 uv run ...` (emoji prints crash captured output otherwise).

## Phase 1 — THINK
- Restate the user's request as a spec: topology, inputs, outputs, constraints.
- Derive **measurable acceptance criteria** — concrete node voltages/currents
  with tolerances (default ±5 % unless the user specified). Example:
  "VOUT_3V3 = 3.30 V ± 5 % with VIN_5V = 5.0 V". If the request has nothing
  measurable, define at minimum: schematic generates, ERC-relevant connectivity
  is sane, expected component count.
- List every component with its intended KiCad `symbol=` and `footprint=` id.
- Write all of this into `design_log.md` under `## Iteration N — plan`.

## Phase 2 — DISCOVER (symbol/footprint resolution)
- NEVER guess lib ids. Verify each one:
  `PYTHONUTF8=1 uv run python tools/find_symbol.py "<query>"` (add
  `--footprints` for footprints). Common: `Device:R`, `Device:C`, `Device:LED`,
  `Regulator_Linear:AMS1117-3.3`.
- If the kicad-sch-api MCP server is connected (see `.mcp.json`), you can confirm
  pin numbering for unfamiliar parts with its tools — e.g. `get_component_pins`,
  `find_pins_by_name`, `find_pins_by_type`. This is optional: if the server is
  not connected, rely on `find_symbol.py` and `main.py`'s pattern instead — a
  missing MCP server must not stop the loop.

## Phase 3 — WRITE
- Create/modify `circuit-synth/<snake_case_name>.py` following `main.py`'s
  pattern: `@circuit(name=...)`, `Net(...)` for connections (GND/VCC-style
  names auto-become power symbols), `component[pin] += net`.
- In `__main__`: `generate_kicad_project(project_name=..., generate_pcb=False)`.
  Do NOT call gerber functions (unavailable in this build).

## Phase 4 — GENERATE
- Run: `PYTHONUTF8=1 uv run python circuit-synth/<name>.py`
- Then verify the output is real, not an empty shell:
  the emitted `.kicad_sch` must contain a `(symbol` block per component and a
  `(property "Reference" "<ref>"` for each expected reference. A schematic of
  ~1 KB with only a text box means every component silently failed.

**Error routing table:**
| Symptom | Route |
|---|---|
| `LibraryNotFound` / `Unknown library ID` | Phase 2 — fix that lib id |
| Schematic missing components / tiny file | Read the run log for `Failed to add component`; Phase 2 or 3 |
| Python exception in your file | Phase 3 — fix the code |
| `UnicodeEncodeError` | You forgot `PYTHONUTF8=1`; rerun |
| Gerber/PCB errors | Ignore — unavailable feature; ensure `generate_pcb=False` |

## Phase 5 — SIMULATE
- Follow the working pattern in `tools/simulate_example.py` to run an operating-
  point analysis of your circuit and capture node voltages for every net named
  in the acceptance criteria. The pattern is: build the `@circuit` function,
  then `sim = circuit.simulate()`, `result = sim.operating_point()`, and read
  values with `result.get_voltage("NET_NAME")` (ngspice node lookup is
  case-insensitive, so `"VOUT_3V3"` and `"vout_3v3"` both work).
- On Windows the ngspice DLL bundled with KiCad is auto-configured — no separate
  ngspice install is needed.
- If simulation errors out or the backend is unavailable (the helper prints
  `SIMULATION_UNAVAILABLE` and exits 2): fall back to STATIC verification —
  recompute expected values by hand (Ohm's law, divider ratios), confirm net
  connectivity in the `.kicad_sch`, and mark the iteration
  "**not simulation-verified**" in `design_log.md`. Never fabricate measurements.

## Phase 6 — EXAMINE & DECIDE
- Compare each measurement to its criterion → PASS/FAIL table in `design_log.md`.
- All PASS → **COMPLETE**: summarize (files written, final values, how verified,
  path to the `.kicad_pro` to open in KiCad). Stop.
- Any FAIL → diagnose before looping:
  - Values wrong but topology right (e.g. Vout off by a ratio) → Phase 3,
    adjust component values; show the algebra in the log.
  - Topology wrong (missing path, shorted net, wrong pin) → Phase 1, re-plan.
  - Same failure twice in a row → change strategy, don't repeat the edit.
- Iteration 5 still failing → stop; report best attempt, remaining gaps, and
  what a human should look at. An honest partial beats a false success.
