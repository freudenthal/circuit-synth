"""Unit tests for the ERC gate parser + classifier (Stage 14, Part A).

Pure Python: they exercise KiCad-10 ERC JSON parsing, reference extraction, and
classification without running kicad-cli. The autofix + full loop are covered by
the kicad-cli e2e in ``tests/e2e/test_erc_gate_autofix.py``.
"""

import os
from tempfile import TemporaryDirectory

import pytest

from circuit_synth import Component, Net, circuit
from circuit_synth.kicad.sch_gen.erc_gate import (
    AUTOFIX_TYPES,
    ErcItem,
    ErcReport,
    ErcViolation,
    _apply_power_flag_autofixes,
    _invert_named_nets,
    _next_flag_index,
    _parse_erc_json,
    classify,
)

pytestmark = pytest.mark.unit

# A representative KiCad-10 ERC JSON (violations nested under sheets[].violations,
# each with an items list carrying the "Symbol #REF Pin N ..." description).
_KICAD10_ERC = {
    "coordinate_units": "mm",
    "kicad_version": "10.0.0",
    "sheets": [
        {
            "path": "/",
            "uuid_path": "/",
            "violations": [
                {
                    "type": "power_pin_not_driven",
                    "severity": "error",
                    "description": "Input Power pin not driven by any Output Power pins",
                    "items": [
                        {
                            "description": "Symbol #PWR001 Pin 1 [Power input, Line]",
                            "pos": {"x": 55.88, "y": 39.37},
                            "uuid": "abc",
                        }
                    ],
                },
                {
                    "type": "isolated_pin_label",
                    "severity": "warning",
                    "description": "Label connected to only one pin",
                    "items": [
                        {"description": "Label 'VIN_5V'", "pos": {"x": 1.0, "y": 2.0}}
                    ],
                },
            ],
        }
    ],
}


def test_parse_kicad10_nested_json():
    report = _parse_erc_json(_KICAD10_ERC, "x.kicad_sch")
    assert len(report.violations) == 2
    assert report.error_count == 1
    assert report.warning_count == 1

    power = report.violations[0]
    assert power.type == "power_pin_not_driven"
    assert power.severity == "error"
    assert power.sheet == "/"
    assert power.items[0].x == 55.88 and power.items[0].y == 39.37


def test_item_reference_extraction():
    item = ErcItem(description="Symbol #PWR001 Pin 1 [Power input, Line]")
    assert item.reference == "#PWR001"
    # A non-symbol item (e.g. a label) yields no reference.
    assert ErcItem(description="Label 'VIN_5V'").reference is None


def test_violation_references():
    report = _parse_erc_json(_KICAD10_ERC, "x.kicad_sch")
    assert report.violations[0].references == ["#PWR001"]
    assert report.violations[1].references == []


# --------------------------------------------------------------------------- #
# Stage 18.1: ERC items expose the flagged pin number; violations expose ref_pins.
# --------------------------------------------------------------------------- #


def test_item_pin_extraction():
    # A real op-amp rail item carries the pin number after "Pin".
    assert (
        ErcItem(description="Symbol U1 Pin 8 [+V_{S}, Power input, Line]").pin == "8"
    )
    assert (
        ErcItem(description="Symbol U1 Pin 8 [+V_{S}, Power input, Line]").reference
        == "U1"
    )
    assert ErcItem(description="Symbol #PWR001 Pin 1 [Power input, Line]").pin == "1"
    # A non-symbol item (e.g. a label) has no pin.
    assert ErcItem(description="Label 'VIN_5V'").pin is None


def test_violation_ref_pins():
    v = ErcViolation(
        type="power_pin_not_driven",
        severity="error",
        description="Input Power pin not driven by any Output Power pins",
        items=[
            ErcItem(description="Symbol U1 Pin 8 [+V_{S}, Power input, Line]"),
            ErcItem(description="Symbol U1 Pin 5 [-V_{S}, Power input, Line]"),
            ErcItem(description="Label 'VIN_5V'"),  # no ref/pin -> excluded
        ],
    )
    assert v.ref_pins == [("U1", "8"), ("U1", "5")]


# --------------------------------------------------------------------------- #
# Stage 18.2: invert a netlist's named_nets to a (ref, pin) -> net map.
# --------------------------------------------------------------------------- #


def test_invert_named_nets():
    named = {
        "GND": {("R1", "2"), ("#PWR01", "1")},
        "V_POS_5V": {("U1", "1"), ("U1", "8")},
    }
    mapping = _invert_named_nets(named)
    assert mapping[("R1", "2")] == "GND"
    assert mapping[("#PWR01", "1")] == "GND"
    assert mapping[("U1", "1")] == "V_POS_5V"
    assert mapping[("U1", "8")] == "V_POS_5V"
    assert len(mapping) == 4


def test_invert_named_nets_empty():
    assert _invert_named_nets({}) == {}


def test_classify_only_power_pin_not_driven_is_autofix():
    report = _parse_erc_json(_KICAD10_ERC, "x.kicad_sch")
    assert classify(report.violations[0]) == "autofix"
    assert classify(report.violations[1]) == "report"
    assert AUTOFIX_TYPES == {"power_pin_not_driven"}


def test_report_summary_mentions_counts_and_types():
    report = _parse_erc_json(_KICAD10_ERC, "x.kicad_sch")
    report.iterations = 2
    report.autofixes_applied = 1
    text = report.summary()
    assert "1 error(s)" in text
    assert "1 warning(s)" in text
    assert "PWR_FLAG autofix" in text
    assert "isolated_pin_label" in text


def test_clean_report_summary():
    report = ErcReport(violations=[], schematic_path="x.kicad_sch")
    assert "Clean" in report.summary()
    assert report.error_count == 0


def test_empty_sheets_parse_to_no_violations():
    report = _parse_erc_json({"sheets": []}, "x.kicad_sch")
    assert report.violations == []
    assert report.error_count == 0


# --------------------------------------------------------------------------- #
# Stage 17.2 (G3): #FLG reference seeding avoids collisions across passes.
# --------------------------------------------------------------------------- #


def test_next_flag_index_empty_starts_at_one():
    assert _next_flag_index(["R1", "C2", "#PWR001"]) == 1


def test_next_flag_index_seeds_past_existing():
    # A schematic already carrying #FLG07 must allocate #FLG08 next, not #FLG01.
    assert _next_flag_index(["R1", "#FLG07", "#PWR001"]) == 8


def test_next_flag_index_uses_max_not_count():
    assert _next_flag_index(["#FLG01", "#FLG02"]) == 3
    # A gap does not reset the counter: max wins.
    assert _next_flag_index(["#FLG01", "#FLG05"]) == 6


def test_next_flag_index_ignores_non_flag_refs():
    # #FLAG.. / #PWR.. are not #FLG references.
    assert _next_flag_index(["#FLAG3", "#PWR12"]) == 1


def test_report_note_appears_in_summary():
    report = ErcReport(violations=[], schematic_path="x.kicad_sch")
    report.note = "autofix aborted on iteration 1: ValidationError: dup"
    assert "autofix aborted" in report.summary()


# --------------------------------------------------------------------------- #
# Stage 17.2 (G3): applying the PWR_FLAG autofix twice must not collide on #FLG.
# Uses kicad-sch-api (a hard dependency) + generation; no kicad-cli needed.
# --------------------------------------------------------------------------- #

R_FP = "Resistor_SMD:R_0603_1608Metric"


@circuit(name="FlgDiv")
def _flg_divider():
    r1 = Component(symbol="Device:R", ref="R1", value="1k", footprint=R_FP)
    r2 = Component(symbol="Device:R", ref="R2", value="2k", footprint=R_FP)
    vin, vout, gnd = Net("VIN_5V"), Net("VOUT_3V3"), Net("GND")
    r1[1] += vin
    r1[2] += vout
    r2[1] += vout
    r2[2] += gnd


def _generate_divider(tmpdir):
    cwd = os.getcwd()
    os.chdir(tmpdir)
    try:
        _flg_divider().generate_kicad_project(
            project_name="flgdiv", generate_pcb=False
        )
    finally:
        os.chdir(cwd)
    from pathlib import Path

    return next(Path(tmpdir).rglob("FlgDiv.kicad_sch"))


def _flg_refs_and_positions(sch_path):
    import re

    import kicad_sch_api as ksa

    sch = ksa.load_schematic(str(sch_path))
    refs, positions = [], []
    for c in sch.components:
        if re.match(r"#FLG\d+$", str(c.reference)):
            refs.append(str(c.reference))
            positions.append((round(c.position.x, 2), round(c.position.y, 2)))
    return refs, positions


def _report_for_power_symbols(sch_path):
    import kicad_sch_api as ksa

    sch = ksa.load_schematic(str(sch_path))
    pwr_refs = [
        str(c.reference)
        for c in sch.components
        if str(c.reference).startswith("#PWR")
    ]
    violations = [
        ErcViolation(
            type="power_pin_not_driven",
            severity="error",
            description="Input Power pin not driven by any Output Power pins",
            items=[ErcItem(description=f"Symbol {ref} Pin 1 [Power input, Line]")],
        )
        for ref in pwr_refs
    ]
    return ErcReport(violations=violations, schematic_path=str(sch_path)), pwr_refs


def test_apply_power_flag_autofixes_twice_does_not_collide_or_stack():
    """The G3 bug: a second autofix pass re-emitted #FLG01 and raised.

    Applying the fix twice against the same file must (a) not raise -- seeding past
    existing flags -- and (b) not stack a second flag on the same point (the
    position guard), so all #FLG refs and positions stay unique.
    """
    with TemporaryDirectory() as tmpdir:
        sch = _generate_divider(tmpdir)
        report, pwr_refs = _report_for_power_symbols(sch)
        assert pwr_refs, "expected the divider's GND power symbol"

        first = _apply_power_flag_autofixes(str(sch), report)
        assert first >= 1
        refs_1, pos_1 = _flg_refs_and_positions(sch)
        assert refs_1, "first pass should have added a #FLG"

        # Second pass previously raised ValidationError (#FLG01 already exists);
        # now it must return cleanly (the GND flag's position is already occupied,
        # so it is skipped rather than stacked).
        second = _apply_power_flag_autofixes(str(sch), report)
        assert isinstance(second, int)  # did not raise

        refs_2, pos_2 = _flg_refs_and_positions(sch)
        assert len(refs_2) == len(set(refs_2)), f"duplicate #FLG refs: {refs_2}"
        assert len(pos_2) == len(set(pos_2)), f"two flags stacked on a point: {pos_2}"
