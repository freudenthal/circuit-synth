"""Headless ERC correction gate for generated schematics (Stage 14, Part A).

Runs KiCad 10's ``kicad-cli sch erc`` on a generated ``.kicad_sch``, parses the
(KiCad-10-nested) JSON report, applies the one reliable autofix -- adding a
``power:PWR_FLAG`` to power nets flagged ``power_pin_not_driven`` -- and iterates a
few times. Everything else is reported, not touched. Modelled on the *idea* of
SKiDL's ``auto_stub`` ERC loop, but circuit_synth-native (no SKiDL dependency).

The gate is **opt-in** (``generate_kicad_project(erc_gate=True)``) and degrades
gracefully: if ``kicad-cli`` is not found it raises :class:`ErcUnavailable`, which
the generator catches and turns into a warning rather than a failure.

Note: this module intentionally re-implements JSON parsing rather than using
``circuit_synth.quality_assurance.erc.run_erc`` -- that helper predates KiCad 10 and
parses a flat ``violations`` list, whereas KiCad 10 nests violations under
``sheets[].violations`` with an ``items`` list. See the Stage-14 findings.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Violation types this gate will auto-repair. Everything else is reported only.
AUTOFIX_TYPES = {"power_pin_not_driven"}

# Regex to pull a component reference (e.g. "#PWR001", "U1") out of an ERC item
# description like: 'Symbol #PWR001 Pin 1 [Power input, Line]'.
_REF_RE = re.compile(r"Symbol\s+(\S+)\s+Pin")

# Same shape but also captures the pin number (group 2), e.g. from
# 'Symbol U1 Pin 8 [+V_{S}, Power input, Line]' -> ("U1", "8").
_REF_PIN_RE = re.compile(r"Symbol\s+(\S+)\s+Pin\s+(\S+)")


class ErcUnavailable(RuntimeError):
    """kicad-cli could not be located, so ERC could not run."""


@dataclass
class ErcItem:
    description: str
    x: Optional[float] = None
    y: Optional[float] = None
    uuid: Optional[str] = None

    @property
    def reference(self) -> Optional[str]:
        m = _REF_RE.search(self.description or "")
        return m.group(1) if m else None

    @property
    def pin(self) -> Optional[str]:
        """The flagged pin number, e.g. "8" from '... Pin 8 [+V_{S}, ...]'."""
        m = _REF_PIN_RE.search(self.description or "")
        return m.group(2) if m else None


@dataclass
class ErcViolation:
    type: str
    severity: str
    description: str
    sheet: str = "/"
    items: List[ErcItem] = field(default_factory=list)

    @property
    def references(self) -> List[str]:
        return [it.reference for it in self.items if it.reference]

    @property
    def ref_pins(self) -> List[Tuple[str, str]]:
        """[(ref, pin)] for every item that names both a symbol and a pin."""
        out: List[Tuple[str, str]] = []
        for it in self.items:
            if it.reference and it.pin:
                out.append((it.reference, it.pin))
        return out


@dataclass
class ErcReport:
    """Parsed ERC result for one schematic (root sheet + its subsheets)."""

    violations: List[ErcViolation]
    schematic_path: str
    iterations: int = 1
    autofixes_applied: int = 0
    note: Optional[str] = None

    @property
    def error_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "warning")

    def summary(self) -> str:
        """One-paragraph human summary suitable for pasting into design_log.md."""
        head = (
            f"ERC: {self.error_count} error(s), {self.warning_count} warning(s) "
            f"after {self.iterations} iteration(s)"
        )
        if self.autofixes_applied:
            head += f"; {self.autofixes_applied} PWR_FLAG autofix(es) applied"
        if self.note:
            head += f" [{self.note}]"
        if not self.violations:
            return head + ". Clean."
        lines = [head + ":"]
        for v in self.violations:
            refs = ", ".join(v.references) if v.references else ""
            lines.append(f"  - [{v.severity}] {v.type} {refs}".rstrip())
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Running kicad-cli
# --------------------------------------------------------------------------- #


def _find_kicad_cli(explicit: Optional[str] = None) -> str:
    if explicit and Path(explicit).exists():
        return explicit
    # Reuse the robust KiCad-10-aware discovery used by the PCB/netlist paths.
    try:
        from ...pcb.kicad_cli import get_kicad_cli

        cli = get_kicad_cli()
        path = getattr(cli, "kicad_cli_path", None)
        if path and Path(str(path)).exists():
            return str(path)
    except Exception as e:  # pragma: no cover - discovery best-effort
        logger.debug("kicad-cli discovery via pcb.kicad_cli failed: %s", e)
    import shutil

    which = shutil.which("kicad-cli")
    if which:
        return which
    raise ErcUnavailable(
        "kicad-cli not found; install KiCad 10 or pass kicad_cli_path. "
        "ERC gate skipped."
    )


def _parse_erc_json(data: dict, schematic_path: str) -> ErcReport:
    """Parse KiCad-10 ERC JSON (violations nested under sheets[].violations)."""
    violations: List[ErcViolation] = []
    for sheet in data.get("sheets", []):
        sheet_path = sheet.get("path", "/")
        for v in sheet.get("violations", []):
            items = []
            for it in v.get("items", []):
                pos = it.get("pos") or {}
                items.append(
                    ErcItem(
                        description=it.get("description", ""),
                        x=pos.get("x"),
                        y=pos.get("y"),
                        uuid=it.get("uuid"),
                    )
                )
            violations.append(
                ErcViolation(
                    type=v.get("type", "unknown"),
                    severity=v.get("severity", "warning"),
                    description=v.get("description", ""),
                    sheet=sheet_path,
                    items=items,
                )
            )
    return ErcReport(violations=violations, schematic_path=str(schematic_path))


def run_erc(
    schematic_path, kicad_cli_path: Optional[str] = None, timeout: int = 60
) -> ErcReport:
    """Run ``kicad-cli sch erc --format json --severity-all`` and parse the result.

    Raises :class:`ErcUnavailable` if kicad-cli is missing, ``FileNotFoundError`` if
    the schematic is missing, ``RuntimeError`` if kicad-cli errors for another reason.
    """
    sch = Path(schematic_path)
    if not sch.exists():
        raise FileNotFoundError(f"Schematic not found: {schematic_path}")
    cli = _find_kicad_cli(kicad_cli_path)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        out_json = tf.name
    try:
        proc = subprocess.run(
            [
                cli,
                "sch",
                "erc",
                "--format",
                "json",
                "--severity-all",
                "--output",
                out_json,
                str(sch),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        # kicad-cli returns 5 when violations exist (with --exit-code-violations);
        # without that flag it returns 0. Treat 0 and 5 as success.
        if proc.returncode not in (0, 5):
            raise RuntimeError(
                f"kicad-cli sch erc failed (exit {proc.returncode}): {proc.stderr.strip()}"
            )
        with open(out_json, "r", encoding="utf-8") as f:
            data = json.load(f)
    finally:
        Path(out_json).unlink(missing_ok=True)

    return _parse_erc_json(data, str(sch))


# --------------------------------------------------------------------------- #
# Classification + autofix
# --------------------------------------------------------------------------- #


def classify(violation: ErcViolation) -> str:
    """``"autofix"`` if the gate can repair this violation, else ``"report"``."""
    return "autofix" if violation.type in AUTOFIX_TYPES else "report"


_FLG_RE = re.compile(r"#FLG0*(\d+)$")


def _next_flag_index(references) -> int:
    """Return the first free ``#FLG`` numeric suffix given existing references.

    Seeds past any ``#FLGnn`` already present so a second autofix pass (across
    ``erc_gate()`` iterations) does not re-emit ``#FLG01`` and collide with a flag
    written by the previous pass. ``#FLG07`` present -> 8; none present -> 1.
    """
    nums = [
        int(m.group(1)) for ref in references if (m := _FLG_RE.match(str(ref)))
    ]
    return (max(nums) + 1) if nums else 1


def _apply_power_flag_autofixes(schematic_path: str, report: ErcReport) -> int:
    """Add a ``power:PWR_FLAG`` to each distinct power net flagged undriven.

    Power symbols carry ``value`` == net name, so we dedupe by value: one flag per
    net clears every ``power_pin_not_driven`` on that net. Returns the number of
    flags added. Returns 0 (and logs) if kicad-sch-api is unavailable.
    """
    undriven_refs = {
        ref
        for v in report.violations
        if classify(v) == "autofix"
        for ref in v.references
    }
    if not undriven_refs:
        return 0

    try:
        import kicad_sch_api as ksa
    except Exception as e:  # pragma: no cover
        logger.warning("kicad-sch-api unavailable; cannot apply ERC autofix: %s", e)
        return 0

    sch = ksa.load_schematic(schematic_path)
    by_ref = {str(c.reference): c for c in sch.components}

    # Seed the flag index past any #FLG refs already in the schematic. This
    # function may be called more than once across erc_gate() iterations (fixing a
    # subset each time), so restarting at 1 would re-emit #FLG01 and collide with
    # the flags written by the previous iteration (KiCad-sch-api raises
    # ValidationError on a duplicate reference).
    flag_index = _next_flag_index(by_ref)

    # Positions already occupied by a PWR_FLAG (from a prior iteration). A ref that
    # the autofix cannot actually clear -- e.g. an op-amp whose power *rails* are
    # undriven, where by_ref[value] is the part number (not a net) and pin "1" is a
    # signal pin -- would otherwise get a fresh flag stacked on the same point every
    # iteration, which ERC then reports as a pin_to_pin short between two flags.
    # Never place two flags on one point (stage 17.2 follow-up).
    def _pt_key(x, y):
        return (round(float(x), 2), round(float(y), 2))

    occupied = {
        _pt_key(c.position.x, c.position.y)
        for c in sch.components
        if str(c.reference).startswith("#FLG") and getattr(c, "position", None)
    }

    flagged_nets = set()
    added = 0
    for ref in sorted(undriven_refs):
        comp = by_ref.get(ref)
        if comp is None:
            continue
        net = getattr(comp, "value", None) or ref
        if net in flagged_nets:
            continue
        pin_pos = sch.get_component_pin_position(ref, "1")
        if pin_pos is None:
            logger.debug("ERC autofix: no pin position for %s, skipping", ref)
            continue
        flag_pos = (pin_pos.x, pin_pos.y + 5.08)
        if _pt_key(*flag_pos) in occupied:
            logger.debug(
                "ERC autofix: a PWR_FLAG already sits at %s (via %s); skipping to "
                "avoid stacking flags",
                flag_pos,
                ref,
            )
            continue
        flag_ref = f"#FLG{flag_index:02d}"
        flag_index += 1
        sch.components.add(
            "power:PWR_FLAG",
            reference=flag_ref,
            value="PWR_FLAG",
            position=flag_pos,
        )
        occupied.add(_pt_key(*flag_pos))
        wire = sch.add_wire_between_pins(ref, "1", flag_ref, "1")
        if wire is None:
            logger.debug("ERC autofix: could not wire PWR_FLAG to %s", ref)
            continue
        flagged_nets.add(net)
        added += 1
        logger.info("ERC autofix: added PWR_FLAG on net '%s' (via %s)", net, ref)

    if added:
        sch.save()
    return added


# --------------------------------------------------------------------------- #
# The gate loop
# --------------------------------------------------------------------------- #


def erc_gate(
    schematic_path,
    max_iterations: int = 3,
    kicad_cli_path: Optional[str] = None,
) -> ErcReport:
    """Run ERC, apply PWR_FLAG autofixes, and iterate until clean or capped.

    Returns the final :class:`ErcReport` (with ``iterations`` and
    ``autofixes_applied`` populated). Raises :class:`ErcUnavailable` if kicad-cli is
    missing -- callers that want graceful degradation should catch it.
    """
    report = run_erc(schematic_path, kicad_cli_path)
    total_fixes = 0
    iteration = 1
    abort_note: Optional[str] = None

    while iteration < max_iterations:
        if not any(classify(v) == "autofix" for v in report.violations):
            break
        try:
            applied = _apply_power_flag_autofixes(str(schematic_path), report)
        except Exception as e:
            # The gate's contract is "never break generation, always return an
            # honest report". Contain a per-iteration autofix failure: end the loop
            # and return the current report with a note, rather than propagating.
            abort_note = f"autofix aborted on iteration {iteration}: {type(e).__name__}: {e}"
            logger.warning("ERC gate: %s", abort_note)
            break
        if applied == 0:
            break  # nothing actionable left; don't spin
        total_fixes += applied
        iteration += 1
        report = run_erc(schematic_path, kicad_cli_path)

    report.iterations = iteration
    report.autofixes_applied = total_fixes
    report.note = abort_note
    return report
