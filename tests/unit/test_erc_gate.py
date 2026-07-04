"""Unit tests for the ERC gate parser + classifier (Stage 14, Part A).

Pure Python: they exercise KiCad-10 ERC JSON parsing, reference extraction, and
classification without running kicad-cli. The autofix + full loop are covered by
the kicad-cli e2e in ``tests/e2e/test_erc_gate_autofix.py``.
"""

import pytest

from circuit_synth.kicad.sch_gen.erc_gate import (
    AUTOFIX_TYPES,
    ErcItem,
    ErcReport,
    ErcViolation,
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
