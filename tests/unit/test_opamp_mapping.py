"""Op-amp pin mapping (Stage 7.2): map by pin function/name, not pin number.

An op-amp is modelled as an ideal VCVS (SPICE E source):
``Vout = Aol * (V(in+) - V(in-))``. The three signal terminals must be identified
by their KiCad pin function/name (output / input "+" / input "-"), NOT by pin
order -- an LM358 unit is out=1, in-=2, in+=3, so positional mapping would swap
the two inputs. PySpice renders the element as
``E<ref> <out+> <out-> <in+> <in-> <gain>``.

Mostly netlist-level (no ngspice); one closed-loop op-point test is skipped when
ngspice is unavailable.
"""

import pytest

from circuit_synth import Component, Net, circuit


def _symbols_available() -> bool:
    try:
        from circuit_synth.simulation.converter import PYSPICE_AVAILABLE

        if not PYSPICE_AVAILABLE:
            return False
        Component(symbol="Amplifier_Operational:LM358", ref="U1", value="LM358")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _symbols_available(),
    reason="PySpice or the KiCad op-amp symbol library is not available",
)


def _netlist(c) -> str:
    from circuit_synth.simulation.converter import SpiceConverter

    return str(SpiceConverter(c).convert())


def _e_line(netlist: str, ref: str) -> list:
    for line in netlist.splitlines():
        if line.startswith(f"E{ref} "):
            return line.split()
    raise AssertionError(f"no VCVS E{ref} in netlist:\n{netlist}")


@circuit(name="noninv_amp")
def _noninv_amp():
    """Non-inverting amp so in+ (VIN) and in- (FB) are distinct nets.

    in- is the Rf/Rg divider tap from VOUT, so a positional mapping (which would
    put pin 2 = in- into the '+' slot) is distinguishable from the correct one.
    """
    v1 = Component(symbol="Simulation_SPICE:VDC", ref="V1", value="1")
    u1 = Component(symbol="Amplifier_Operational:LM358", ref="U1", value="LM358")
    rf = Component(
        symbol="Device:R",
        ref="RF",
        value="10k",
        footprint="Resistor_SMD:R_0603_1608Metric",
    )
    rg = Component(
        symbol="Device:R",
        ref="RG",
        value="10k",
        footprint="Resistor_SMD:R_0603_1608Metric",
    )
    vin = Net("VIN")
    vout = Net("VOUT")
    fb = Net("FB")
    gnd = Net("GND")
    v1[1] += vin
    v1[2] += gnd
    u1[3] += vin  # in+  (non-inverting)
    u1[2] += fb  # in-  (inverting)
    u1[1] += vout  # out
    rf[1] += vout
    rf[2] += fb
    rg[1] += fb
    rg[2] += gnd


def test_opamp_terminals_mapped_by_function():
    """out/in+/in- are resolved by pin function+name, not pin number."""
    parts = _e_line(_netlist(_noninv_amp()), "U1")
    # E<ref> <out+> <out-> <in+> <in-> <gain>
    assert parts[1] == "VOUT", f"output node wrong: {parts}"
    assert parts[2] == "0", f"output- should be ground: {parts}"
    assert parts[3] == "VIN", f"non-inverting (+) input wrong: {parts}"
    assert parts[4] == "FB", f"inverting (-) input wrong: {parts}"


def test_opamp_modeled_as_high_gain_vcvs():
    """The ideal model uses a high open-loop gain."""
    parts = _e_line(_netlist(_noninv_amp()), "U1")
    assert float(parts[5]) >= 1e5, f"op-amp gain should be high (ideal): {parts}"


def test_unused_unit_of_dual_opamp_ignored():
    """Only the connected unit of a dual op-amp is modelled (one VCVS)."""
    netlist = _netlist(_noninv_amp())
    e_lines = [ln for ln in netlist.splitlines() if ln.startswith("EU1 ")]
    assert len(e_lines) == 1, f"expected exactly one VCVS for U1:\n{netlist}"


def _ngspice_loads() -> bool:
    try:
        from circuit_synth.simulation.simulator import PYSPICE_AVAILABLE

        if not PYSPICE_AVAILABLE:
            return False
        from PySpice.Spice.NgSpice.Shared import NgSpiceShared

        NgSpiceShared.new_instance()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _ngspice_loads(), reason="no loadable ngspice library")
def test_noninverting_closed_loop_gain():
    """End-to-end: the ideal op-amp gives the analytic closed-loop gain.

    Non-inverting amp with Rf=Rg=10k has gain 1 + Rf/Rg = 2, so VIN=1 V -> VOUT=2 V.
    Wrong input mapping (in+/in- swapped) makes the feedback positive and the
    op-point diverges, so this also guards the pin mapping through simulation.
    """
    result = _noninv_amp().simulate().operating_point()
    assert result.get_voltage("VIN") == pytest.approx(1.0, abs=0.01)
    assert result.get_voltage("VOUT") == pytest.approx(2.0, abs=0.02)
