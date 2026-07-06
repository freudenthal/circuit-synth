"""Acceptance: LDO behavioral macromodel end-to-end on ngspice (Stage 20.1).

A datasheet-parameterized linear regulator simulates with physically sensible
behavior across its operating range, using no vendor model file:

* In regulation (VIN comfortably above VOUT+VDROP) the output holds at VOUT minus
  the small RSER * Iload drop.
* In dropout (VIN below VOUT+VDROP) the output tracks (VIN - VDROP).

A DC sweep of the input from 0..6 V into a 3.3 V macromodel (VDROP=0.3, RSER=0.1)
with a 33 ohm load reproduces the feasibility-spike numbers: ~3.29 V at VIN=5 V
and ~1.70 V at VIN=2 V. U1 resolves at tier ``sim_params``.

Skips cleanly when PySpice or a loadable ngspice is unavailable.
"""

import numpy as np
import pytest

from circuit_synth import Component, Net, circuit


def _ngspice_available() -> bool:
    try:
        from circuit_synth.simulation.simulator import PYSPICE_AVAILABLE

        if not PYSPICE_AVAILABLE:
            return False
        Component(symbol="Regulator_Linear:AMS1117-3.3", ref="U1")
        Component(symbol="Simulation_SPICE:VDC", ref="V1", value="5")
        from PySpice.Spice.NgSpice.Shared import NgSpiceShared

        NgSpiceShared.new_instance()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _ngspice_available(),
    reason="PySpice, the KiCad regulator symbol, or a loadable ngspice is not available",
)


@circuit(name="ldo_regulation")
def _ldo_regulation():
    """3.3 V LDO fed by a sweepable VDC, driving a 33 ohm load."""
    u1 = Component(
        symbol="Regulator_Linear:AMS1117-3.3",
        ref="U1",
        **{"Sim.Device": "LDO", "Sim.Params": "vout=3.3 vdrop=0.3 rser=0.1 iq=2m"},
    )
    v1 = Component(symbol="Simulation_SPICE:VDC", ref="V1", value="5")
    rl = Component(symbol="Device:R", ref="RL", value="33")
    vin = Net("VIN")
    vout = Net("VOUT")
    gnd = Net("GND")
    v1[1] += vin
    v1[2] += gnd
    u1[3] += vin  # VI
    u1[1] += gnd  # GND
    u1[2] += vout  # VO
    rl[1] += vout
    rl[2] += gnd


def _sweep():
    c = _ldo_regulation()
    sim = c.simulate()
    # PySpice prepends the element letter to the ref, so V1 is swept as "VV1".
    result = sim.dc_analysis(source="VV1", start=0, stop=6, step=0.05)
    vin = np.real(np.asarray(result.sweep_array())).astype(float)
    vout = np.real(np.asarray(result.analysis["VOUT"])).astype(float)
    return sim, vin, vout


def _v_at(vin, vout, target):
    return float(vout[int(np.argmin(np.abs(vin - target)))])


def test_ldo_regulates_in_range():
    """VIN=5 V -> output holds near 3.3 V (minus the RSER*Iload drop)."""
    _sim, vin, vout = _sweep()
    assert 3.25 < _v_at(vin, vout, 5.0) < 3.31


def test_ldo_tracks_in_dropout():
    """VIN=2 V (below 3.3+0.3) -> output tracks VIN-VDROP."""
    _sim, vin, vout = _sweep()
    assert 1.5 < _v_at(vin, vout, 2.0) < 1.72


def test_ldo_provenance_is_sim_params_tier():
    sim, _vin, _vout = _sweep()
    prov = sim.model_provenance.get("U1")
    assert prov is not None and prov.kind == "ldo" and prov.tier == "sim_params", prov
