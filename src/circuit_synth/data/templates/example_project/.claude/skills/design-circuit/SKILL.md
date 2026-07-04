---
name: design-circuit
description: Design a circuit from a natural-language spec using an iterative loop — plan, write circuit-synth Python, generate the KiCad schematic, simulate with ngspice, examine results, and refine until the spec's measurable criteria pass. Use whenever the user asks to design, create, or modify a circuit or schematic in this project.
---

# design-circuit — iterative circuit design loop

You are designing a circuit in a circuit-synth project. Work in numbered
iterations (max **5**). Keep an append-only `design_log.md` in the project root;
after every iteration append: iteration number, what changed, generation result,
simulation measurements, PASS/FAIL per criterion, and the next action.

## Two modes: NEW design vs EDIT existing

- **NEW design** (no matching `.py` yet, or the user asks for a fresh circuit):
  run the full loop below starting at Phase 0.
- **EDIT an existing design** (the user asks to change/tweak/fix a circuit that
  already has a `circuit-synth/*.py` in this project — "make R3 4.7k", "add a
  bypass cap on VOUT", "raise the cutoff to 10 kHz"): jump to the
  **"Editing an existing design"** section near the end. The short version:
  change the **Python source**, regenerate in update mode (placement is
  preserved), and re-simulate — do **not** hand-edit the generated `.kicad_sch`
  for value/topology changes; the next regeneration overwrites such edits.

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
  For a multi-sheet design (see Phase 3), group the component list by sheet
  (`### Sheet: psu`, `### Sheet: amp`, ...) so the hierarchy is visible in the log.
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
- **Sourcing / availability (OPTIONAL — only when the user asks for real parts,
  a BOM, or sourcing, or supplies MPNs).** Check real stock/price:
  `PYTHONUTF8=1 uv run python tools/check_availability.py "<query>"`. **JLCPCB
  works without any credentials** (via the keyless tscircuit JLCSearch mirror,
  rows tagged `jlcpcb:jlcsearch`); DigiKey needs `DIGIKEY_CLIENT_ID`/`_SECRET`
  and is skipped otherwise. It never returns fake data and prints a
  `skipped: <source> -- <reason>` line for any source it could not query.
  - Record a `### Sourcing` table in the iteration-plan block with columns
    `| ref | MPN | source | stock | price | note |`.
  - **Honesty rule:** if a source was skipped (no credentials / network error),
    write "not checked — no credentials" in the note; **never invent stock or
    prices.** No creds at all → say sourcing was not verified and move on; this
    must not block the design.
  - Attach the chosen part's identity to its component as plain KiCad
    properties via kwargs — `Component(..., **{"MPN": "2N7000",
    "Manufacturer": "onsemi", "Distributor": "DigiKey"})`. They round-trip into
    the schematic (same mechanism as `Sim.*`); no schema change needed.

## Phase 3 — WRITE
<!-- language-coupled: this WRITE step teaches the circuit_synth DSL (@circuit / Net / generate_kicad_project). A future DSL swap repoints this section only; see workingdocs/loop-boundary-contract.md rule R3. -->
- Create/modify `circuit-synth/<snake_case_name>.py` following `main.py`'s
  pattern: `@circuit(name=...)`, `Net(...)` for connections (GND/VCC-style
  names auto-become power symbols), `component[pin] += net`.
- In `__main__`: `generate_kicad_project(project_name=..., generate_pcb=False)`.
  Do NOT call gerber functions (unavailable in this build).
- **Multi-sheet / hierarchical designs.** Split into sheets when the design has
  distinct functional blocks (power, MCU, analog front-end, ...), the user asks
  for it, or it exceeds ~15 components. Pattern: write one `@circuit` function
  per block, and a top `@circuit` that creates the *shared* nets and calls each
  block, **passing the same `Net` objects** into the blocks that must connect:

  ```python
  @circuit(name="psu")
  def psu(vin, v5, gnd): ...        # components here land on the psu sheet

  @circuit(name="amp")
  def amp(v5, gnd, sig_in, sig_out): ...

  @circuit(name="main")
  def main():
      vin, v5, gnd = Net("VIN_9V"), Net("V5"), Net("GND")
      sig_in, sig_out = Net("SIG_IN"), Net("SIG_OUT")
      psu(vin, v5, gnd)             # auto-registered as a child sheet
      amp(v5, gnd, sig_in, sig_out) # V5/GND shared by object identity
  ```

  Generation emits one `.kicad_sch` per block plus the root; a net shared
  between two or more blocks becomes a **sheet pin** on each (e.g. `V5`), while
  power nets (`GND`/`VCC*`) use global power symbols, not pins. A net used
  inside only one block stays local to that sheet — so to expose a block's I/O,
  share that net with the top or another block. See
  `tools/hierarchical_example.py` for a runnable two-sheet reference.
- Simulation flattens the hierarchy automatically; measure nodes by net name as
  usual (`result.get_voltage("V5")`) — no special handling needed.
<!-- /language-coupled -->

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

## Phase 4.5 — ERC (optional connectivity gate)
- Run KiCad's headless Electrical Rules Check to catch real wiring mistakes
  (undriven power, dangling pins) before trusting the schematic:
  `PYTHONUTF8=1 "C:\Program Files\KiCad\10.0\bin\kicad-cli.exe" sch erc
  --format json --severity-all --output erc.json <project>/<name>.kicad_sch`
  (netlist/ERC on the **root** sheet covers subsheets too).
- To auto-repair the common `power_pin_not_driven` case (a power symbol with no
  driver → add a `PWR_FLAG`), enable the generator's ERC gate: pass
  `erc_gate=True` to the generation call in your Phase-3 file. It iterates ERC +
  PWR_FLAG fixes and returns the residual report on the result
  (`result["erc_report"].summary()`).
- Paste the ERC summary into `design_log.md`. Treat remaining **errors** as
  FAIL → route back to Phase 3 (fix the connection). **Warnings** like
  `isolated_pin_label` on an I/O net terminated by a single label are normal —
  note them, don't chase them. If kicad-cli is absent, skip this phase.

## Phase 5 — SIMULATE
<!-- language-coupled: the .simulate() API and Sim.* model controls are circuit_synth-specific; the run/measure/plot/fallback *procedure* is portable. See workingdocs/loop-boundary-contract.md rule R3. -->
- **DC / operating point:** follow the working pattern in
  `tools/simulate_example.py` to run an operating-point analysis of your circuit
  and capture node voltages for every net named in the acceptance criteria. The
  pattern is: build the `@circuit` function, then `sim = circuit.simulate()`,
  `result = sim.operating_point()`, and read values with
  `result.get_voltage("NET_NAME")` (ngspice node lookup is case-insensitive, so
  `"VOUT_3V3"` and `"vout_3v3"` both work).
- **AC / frequency response:** follow `tools/simulate_filter.py`. Drive the input
  with a `Simulation_SPICE:VSIN` source (it carries an AC magnitude of 1 V, so the
  output node *is* the transfer function), then
  `result = sim.ac_analysis(start_hz, stop_hz, points)` and measure with
  `result.cutoff_frequency("NET")` (−3 dB corner), `result.passband_gain_db("NET")`,
  and `result.bode("NET")` → `(freq, magnitude_db, phase_deg)`. Roll-off is best
  measured on the asymptote (e.g. 10·fc → 100·fc), not fc → 10·fc (fc sits on the
  −3 dB knee).
- **Declaring sources:** use KiCad's real `Simulation_SPICE` symbols — `VDC` for a
  DC supply, `VSIN` for AC/transient stimulus. Pin 1 is `+`, pin 2 is `-`. Do NOT
  use `Device:V`/`Device:I` (not real KiCad symbols). An explicit source overrides
  the net-name rail heuristic on the nets it drives.
- **Transient stimulus:** pass waveform parameters as component kwargs (they are
  stored as extra fields). `VSIN` reads `amplitude`/`frequency`/`offset`; `VPULSE`
  reads `v1`/`v2`/`td`/`tr`/`tf`/`pw`/`per`; `VPWL` reads `points` (a string or a
  list of `(t, v)` pairs). Keep SI suffixes (`1k`/`1m`/`1u`/`1n`) — ngspice parses
  them. Run with `sim.transient_analysis(step_s, end_s)`; an optional `options={...}`
  (e.g. `reltol`/`abstol`/`gmin`) tunes ngspice convergence on any analysis.
- **Active-device models (diodes/BJTs/MOSFETs):** naming a real part in `value`
  (e.g. `value="1N4148"`, `value="2N3904"`) pulls **datasheet-fit** parameters from
  the built-in model library when known; otherwise a device falls back to a
  textbook-generic model. The converter records which tier each device got in
  `sim.model_provenance[ref].tier` (`datasheet_fit` / `generic` / `vendor_lib`) and
  logs it, so a generic is never silently passed off as the real part. Naming a
  part the library can't resolve is a hard error (declare the model, don't guess).
- **Op-amps** default to an ideal VCVS (infinite gain-bandwidth), so feedback/source
  capacitance has no effect on simulated bandwidth or stability. For a bandwidth- or
  stability-sensitive design (e.g. a transimpedance amp with a large source cap) add
  `Sim.Gbw="1.4G"` to opt into a single-pole GBW-limited macromodel — then the Rf·Cf
  pole, source capacitance, and finite loop bandwidth interact, so cap-limited rolloff
  and gain peaking become visible. Without `Sim.Gbw` the ideal model is unchanged.
- **Simulation-only model controls (KiCad `Sim.*`, passed as component kwargs):**
  `Sim.Enable="0"` excludes a part from simulation (its symbol/footprint stay put —
  use it for connectors/test points so `validate()` doesn't flag them);
  `Sim.Params="bf=250 vaf=80"` overrides model params; `Sim.Library="path.lib"` +
  `Sim.Name="MODEL"` (+ optional `Sim.Pins="1=out 2=inp 3=inn"`) attaches an external
  vendor `.lib`/`.subckt` model verbatim. Pass dotted names as
  `Component(..., **{"Sim.Enable": "0"})`.
- On Windows the ngspice DLL bundled with KiCad is auto-configured — no separate
  ngspice install is needed.
- **Save a plot of the result** so the log is visual, not just numbers. Every
  `SimulationResult` has headless PNG savers (no display needed):
  `result.save_bode_plot("sim_plots/iterN_<name>_bode.png", "NET")` for AC,
  `result.save_transient_plot("sim_plots/iterN_<name>_tran.png", ["NET", ...])`
  for transient, `result.save_dc_transfer_plot("sim_plots/iterN_<name>_dc.png",
  "NET", sweep_label="Vin")` for a DC sweep. Write them under a `sim_plots/`
  directory in the project root (created automatically); use the iteration
  number and circuit name in the filename so they don't collide. They return
  the written path, or `None` if plotting is unavailable — if `None`, skip the
  embed step, don't fail the loop.
- If simulation errors out or the backend is unavailable (the helper prints
  `SIMULATION_UNAVAILABLE` and exits 2): fall back to STATIC verification —
  recompute expected values by hand (Ohm's law, divider ratios), confirm net
  connectivity in the `.kicad_sch`, and mark the iteration
  "**not simulation-verified**" in `design_log.md`. Never fabricate measurements.
<!-- /language-coupled -->

## Phase 6 — EXAMINE & DECIDE
- Compare each measurement to its criterion → PASS/FAIL table in `design_log.md`.
- **Embed the plot(s)** you saved in Phase 5: after the PASS/FAIL table, add one
  markdown image per plot — `![iter N bode](sim_plots/iterN_<name>_bode.png)` —
  followed by a one-line reading of what it shows (e.g. "−3 dB at ~9.7 kHz,
  −20 dB/dec rolloff — matches the 1st-order target"). If a plot save returned
  `None` (plotting unavailable), skip the embed and note "plot unavailable"; if
  simulation didn't run at all, there is no plot — say so, never invent one.
- All PASS → **COMPLETE**: summarize (files written, final values, how verified,
  path to the `.kicad_pro` to open in KiCad). Stop.
- Any FAIL → diagnose before looping:
  - Values wrong but topology right (e.g. Vout off by a ratio) → Phase 3,
    adjust component values; show the algebra in the log.
  - Topology wrong (missing path, shorted net, wrong pin) → Phase 1, re-plan.
  - Same failure twice in a row → change strategy, don't repeat the edit.
- Iteration 5 still failing → stop; report best attempt, remaining gaps, and
  what a human should look at. An honest partial beats a false success.

## Editing an existing design

Use this when the user asks to modify a circuit that already has a
`circuit-synth/*.py` source in this project. The design's **source of truth is
the Python file**, not the `.kicad_sch` — every downstream step (simulation,
`Sim.*` model controls, `validate()`, sourcing/BOM) reads the Python circuit, so
edits must go there. In-place edits to the generated `.kicad_sch` are invisible
to those steps and are **overwritten the next time the project is regenerated**.

<!-- language-coupled: the edit steps operate on the circuit_synth Python source (generate_kicad_project / force_regenerate update mode). See workingdocs/loop-boundary-contract.md rule R3. -->
1. **Locate the source.** Find the `circuit-synth/*.py` whose
   `generate_kicad_project(project_name=...)` matches the project the user means
   (usually `main.py` or the one named after the target). If several could
   match, ask which; never guess and hand-edit the `.kicad_sch`.
2. **Change the Python.** Make the requested edit in code — component values,
   added/removed components, net connections, `Sim.*` kwargs, MPN/Manufacturer
   kwargs. Follow the same API patterns as the rest of the file.
3. **Regenerate in update mode (placement-preserving).** Re-run the file:
   `PYTHONUTF8=1 uv run python circuit-synth/<name>.py`. Keep the default
   `generate_kicad_project(..., generate_pcb=False)` — its default
   `force_regenerate=False` performs an **update**: components are matched by
   UUID/reference/topology, so existing parts keep their manual KiCad placement
   while your change is applied and any new part is dropped in a free column to
   the right (reposition later in KiCad). **Do not pass `force_regenerate=True`**
   unless the user explicitly accepts losing all manual placement — it rewrites
   the schematic from scratch. Verify the edit landed the same way Phase 4 does
   (the changed value / new `(symbol` block is present in the `.kicad_sch`).
4. **Re-simulate and re-examine (Phases 5–6)** for the criteria the edit
   affects. Append a new block to `design_log.md` headed
   `## Iteration N — edit`: state what the user asked, the code change, the
   regeneration result (updated/preserved/added), the new measurements vs. the
   criteria (PASS/FAIL), and an embedded plot if you produced one.
5. **Iterate** as in Phase 6 if the edit didn't meet its criterion.
<!-- /language-coupled -->

**MCP boundary.** The kicad-sch-api MCP server (if connected) stays a
**read-only helper** in this loop — pin lookups (`get_component_pins`,
`find_pins_by_name`) during Phase 2. Its editing tools (`add_component`,
`add_wire`, `bulk_update_components`, …) directly mutate a `.kicad_sch`; on a
project that has a circuit-synth `.py` source, those edits diverge from the
source and are lost on the next regeneration, so **route every value/topology
change back to the Python file**. The MCP editing tools are for schematics that
have **no** circuit-synth source (foreign/hand-drawn `.kicad_sch`), which this
skill does not manage.
