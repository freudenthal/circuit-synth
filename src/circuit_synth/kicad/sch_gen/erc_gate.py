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
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Violation types this gate will auto-repair. Everything else is reported only.
AUTOFIX_TYPES = {"power_pin_not_driven"}

# Regex to pull a component reference (e.g. "#PWR001", "U1") out of an ERC item
# description like: 'Symbol #PWR001 Pin 1 [Power input, Line]'.
_REF_RE = re.compile(r"Symbol\s+(\S+)\s+Pin")


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


@dataclass
class ErcReport:
    """Parsed ERC result for one schematic (root sheet + its subsheets)."""

    violations: List[ErcViolation]
    schematic_path: str
    iterations: int = 1
    autofixes_applied: int = 0

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

    flagged_nets = set()
    added = 0
    flag_index = 1
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
        flag_ref = f"#FLG{flag_index:02d}"
        flag_index += 1
        sch.components.add(
            "power:PWR_FLAG",
            reference=flag_ref,
            value="PWR_FLAG",
            position=(pin_pos.x, pin_pos.y + 5.08),
        )
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

    while iteration < max_iterations:
        if not any(classify(v) == "autofix" for v in report.violations):
            break
        applied = _apply_power_flag_autofixes(str(schematic_path), report)
        if applied == 0:
            break  # nothing actionable left; don't spin
        total_fixes += applied
        iteration += 1
        report = run_erc(schematic_path, kicad_cli_path)

    report.iterations = iteration
    report.autofixes_applied = total_fixes
    return report
