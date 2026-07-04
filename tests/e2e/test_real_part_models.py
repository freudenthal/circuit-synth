"""Acceptance: real-part simulation with datasheet-fit models (Stage 9.5).

End-to-end proof that naming a real MPN pulls datasheet-fit parameters through the
resolution ladder and produces physically sensible results on ngspice -- and that
a ``Sim.Enable=0`` decorative part changes nothing.

* 1N4148 half-wave rectifier -> forward drop lands in the datasheet 0.6-0.8 V band
  (a textbook-generic diode would give a visibly different drop), reverse blocks.
* 2N3904 common-emitter amp -> a sane operating point (IC and an active-region
  collector) and a small-signal gain matching the emitter-degenerated design.
* Both assert the device resolved to tier ``datasheet_fit``.

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
        Component(symbol="Simulation_SPICE:VSIN", ref="V1", value="1")
        Component(symbol="Transistor_BJT:BC547", ref="Q1")
        from PySpice.Spice.NgSpice.Shared import NgSpiceShared

        NgSpiceShared.new_instance()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _ngspice_available(),
    reason="PySpice, KiCad sim symbols, or a loadable ngspice is not available",
)


# --------------------------------------------------------------------------- #
# 1N4148 half-wave rectifier                                                  #
# --------------------------------------------------------------------------- #


def _wire_rectifier(with_decorative):
    v1 = Component(
        symbol="Simulation_SPICE:VSIN", ref="V1", value="10V", frequency="1k"
    )
    d1 = Component(symbol="Device:D", ref="D1", value="1N4148")
    rl = Component(symbol="Device:R", ref="RL", value="1k")
    vin = Net("VIN")
    out = Net("OUT")
    gnd = Net("GND")
    v1[1] += vin
    v1[2] += gnd
    d1[1] += vin
    d1[2] += out
    rl[1] += out
    rl[2] += gnd
    if with_decorative:
        # A non-simulatable decorative part opted out of simulation. It must not
        # affect (or break) the run.
        j1 = Component(
            symbol="Device:Varistor", ref="RV1", value="", **{"Sim.Enable": "0"}
        )
        j1[1] += vin
        j1[2] += gnd


@circuit(name="Rectifier1N4148")
def _rectifier_plain():
    _wire_rectifier(with_decorative=False)


@circuit(name="Rectifier1N4148Deco")
def _rectifier_deco():
    _wire_rectifier(with_decorative=True)


def _rectifier_peak(builder):
    sim = builder().simulate()
    res = sim.transient_analysis(step_time=2e-6, end_time=3e-3)
    return sim, np.array(res.analysis["OUT"])


def test_rectifier_forward_drop_is_datasheet_band():
    """Peak output = Vpk - Vf with the 1N4148 Vf in the datasheet 0.6-0.8 V band."""
    sim, out = _rectifier_peak(_rectifier_plain)
    vf = 10.0 - out.max()
    assert 0.6 <= vf <= 0.8, f"Vf={vf:.3f} outside datasheet band"
    assert out.min() > -0.5  # reverse half blocked
    assert sim.model_provenance["D1"].tier == "datasheet_fit"
    assert sim.model_provenance["D1"].name == "1N4148"


def test_sim_enable_decorative_part_is_transparent():
    """A Sim.Enable=0 decorative part validates, is excluded, and changes nothing.

    Building two circuits in one test collides in the global reference hierarchy,
    so this builds only the decorated variant and checks it lands in the same
    datasheet Vf band as the plain rectifier (test above), with the decorative
    RV1 absent from the netlist.
    """
    from circuit_synth.simulation.converter import SpiceConverter

    ckt = _rectifier_deco()
    netlist = str(SpiceConverter(ckt).convert())  # validates (strict) + excludes
    assert "RV1" not in netlist

    sim = ckt.simulate()
    out = np.array(sim.transient_analysis(step_time=2e-6, end_time=3e-3).analysis["OUT"])
    vf = 10.0 - out.max()
    assert 0.6 <= vf <= 0.8, f"Vf={vf:.3f} with decorative part present"


# --------------------------------------------------------------------------- #
# 2N3904 common-emitter amplifier                                             #
# --------------------------------------------------------------------------- #


@circuit(name="CommonEmitter2N3904")
def _ce_amp():
    """Emitter-degenerated CE amp: 47k/10k bias, RC=4.7k, RE=1k (unbypassed)."""
    vcc = Component(symbol="Simulation_SPICE:VDC", ref="V1", value="12V")
    vac = Component(
        symbol="Simulation_SPICE:VSIN", ref="V2", value="0.01V", frequency="10k"
    )
    cin = Component(symbol="Device:C", ref="Cin", value="10uF")
    r1 = Component(symbol="Device:R", ref="R1", value="47k")
    r2 = Component(symbol="Device:R", ref="R2", value="10k")
    rc = Component(symbol="Device:R", ref="RC", value="4.7k")
    re = Component(symbol="Device:R", ref="RE", value="1k")
    q1 = Component(symbol="Transistor_BJT:BC547", ref="Q1", value="2N3904")

    vccn = Net("VCC")
    inn = Net("IN")
    b = Net("B")
    c = Net("C")
    e = Net("E")
    gnd = Net("GND")

    vcc[1] += vccn
    vcc[2] += gnd
    vac[1] += inn
    vac[2] += gnd
    cin[1] += inn
    cin[2] += b
    r1[1] += vccn
    r1[2] += b
    r2[1] += b
    r2[2] += gnd
    rc[1] += vccn
    rc[2] += c
    re[1] += e
    re[2] += gnd
    # BC547 pinout: 1 = C, 2 = B, 3 = E.
    q1[1] += c
    q1[2] += b
    q1[3] += e


def test_ce_amp_operating_point():
    """Bias puts IC near the ~1.4 mA design (+/-30 %) with the collector active."""
    sim = _ce_amp().simulate()
    res = sim.operating_point()
    vc = float(np.array(res.analysis["C"])[0])
    ic = (12.0 - vc) / 4700.0
    assert 1.0e-3 <= ic <= 1.8e-3, f"IC={ic*1e3:.3f} mA outside design band"
    assert 1.0 < vc < 11.0, f"collector {vc:.2f} V not in the active region"
    assert sim.model_provenance["Q1"].tier == "datasheet_fit"
    assert sim.model_provenance["Q1"].name == "2N3904"


def test_ce_amp_small_signal_gain():
    """Midband |gain| matches the degenerated design RC/(RE+re) ~= 4.6."""
    res = _ce_amp().simulate().ac_analysis(1e3, 1e5, points=20)
    freq = np.array(res.analysis.frequency)
    idx = int(np.argmin(np.abs(freq - 1e4)))
    gain = abs(np.array(res.analysis["C"])[idx]) / abs(np.array(res.analysis["IN"])[idx])
    assert 3.5 <= gain <= 5.5, f"midband gain {gain:.2f} outside expected range"
