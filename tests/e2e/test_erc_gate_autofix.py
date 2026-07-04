"""E2E: the ERC gate clears power_pin_not_driven via PWR_FLAG (Stage 14, Part A).

Requires KiCad 10's kicad-cli (skips cleanly if absent). Generates the template
divider — whose GND power symbol has no driver, so KiCad ERC flags
``power_pin_not_driven`` — and asserts the gate auto-fixes it.
"""

import os
from pathlib import Path

import pytest

from circuit_synth import Component, Net, circuit
from circuit_synth.kicad.sch_gen.erc_gate import ErcUnavailable, erc_gate, run_erc

pytestmark = pytest.mark.e2e

R_FP = "Resistor_SMD:R_0603_1608Metric"


@circuit(name="ErcGateDiv")
def _divider():
    r1 = Component(symbol="Device:R", ref="R1", value="1k", footprint=R_FP)
    r2 = Component(symbol="Device:R", ref="R2", value="2k", footprint=R_FP)
    vin, vout, gnd = Net("VIN_5V"), Net("VOUT_3V3"), Net("GND")
    r1[1] += vin
    r1[2] += vout
    r2[1] += vout
    r2[2] += gnd


def _generate(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        _divider().generate_kicad_project(project_name="ercgatediv", generate_pcb=False)
    finally:
        os.chdir(cwd)
    return next(tmp_path.rglob("ErcGateDiv.kicad_sch"))


def _require_erc(sch: Path):
    try:
        return run_erc(str(sch))
    except ErcUnavailable:
        pytest.skip("kicad-cli (KiCad 10) not available")


def test_erc_gate_clears_power_pin_not_driven(tmp_path):
    sch = _generate(tmp_path / "proj")

    before = _require_erc(sch)
    assert any(
        v.type == "power_pin_not_driven" for v in before.violations
    ), "expected the bare divider's GND symbol to trip power_pin_not_driven"

    report = erc_gate(str(sch))
    assert report.autofixes_applied >= 1
    assert not any(v.type == "power_pin_not_driven" for v in report.violations)
    assert (
        report.error_count == 0
    )  # only the benign isolated_pin_label warning may remain


def test_generate_kicad_project_erc_gate_flag(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = _divider().generate_kicad_project(
            project_name="ercflag", generate_pcb=False, erc_gate=True
        )
    finally:
        os.chdir(cwd)

    report = result.get("erc_report")
    if report is None:
        pytest.skip("kicad-cli (KiCad 10) not available — gate skipped")
    assert report.error_count == 0
    assert report.autofixes_applied >= 1


@circuit(name="TwoRail")
def _two_rail():
    # Two undriven power rails (VCC + GND) -> two power_pin_not_driven violations,
    # mirroring the canary's multi-rail op-amp. The gate must flag both with
    # *unique* #FLG references and run to completion (Stage 17.2, G3).
    r1 = Component(symbol="Device:R", ref="R1", value="10k", footprint=R_FP)
    r2 = Component(symbol="Device:R", ref="R2", value="10k", footprint=R_FP)
    vcc, gnd, sig = Net("VCC"), Net("GND"), Net("SIG")
    r1[1] += vcc
    r1[2] += sig
    r2[1] += sig
    r2[2] += gnd


def test_erc_gate_two_rails_unique_flags_runs_to_completion(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        _two_rail().generate_kicad_project(project_name="tworail", generate_pcb=False)
    finally:
        os.chdir(cwd)
    sch = next(tmp_path.rglob("TwoRail.kicad_sch"))

    before = _require_erc(sch)
    assert (
        sum(1 for v in before.violations if v.type == "power_pin_not_driven") >= 2
    ), "expected two undriven rails"

    # Runs to completion without propagating an exception.
    report = erc_gate(str(sch))
    assert report.autofixes_applied >= 2

    # All #FLG references written are unique (the G3 collision would abort here).
    import re

    import kicad_sch_api as ksa

    reloaded = ksa.load_schematic(str(sch))
    flg_refs = [
        str(c.reference)
        for c in reloaded.components
        if re.match(r"#FLG\d+$", str(c.reference))
    ]
    assert len(flg_refs) == len(set(flg_refs)), f"duplicate #FLG refs: {flg_refs}"
    assert len(flg_refs) >= 2
