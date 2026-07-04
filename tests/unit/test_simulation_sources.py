"""Explicit SPICE source support.

A circuit can declare its own voltage/current source with a real KiCad symbol
(``Simulation_SPICE:VDC`` / ``IDC``) and an explicit value, instead of relying on
the net-name heuristic in ``converter._add_power_sources`` (which only injects a
supply for nets whose names match rail patterns like ``VCC*``/``VIN*``).

Two layers of coverage:

* **Netlist-level** (no ngspice needed): build the circuit, run ``SpiceConverter``,
  and assert on the emitted SPICE netlist string -- the source is present with the
  right value, its pins map to the right SPICE nodes in pin-number order (KiCad
  ``VDC`` declares ``Sim.Pins "1=+ 2=-"``), and the net-name heuristic did *not*
  add a second supply on a net the explicit source already drives.
* **End-to-end** (skipped without a loadable ngspice): a DC operating point proves
  the declared source actually drives the circuit with the correct sign.

Both layers skip cleanly (never fail) when PySpice or the KiCad Simulation_SPICE
symbol library is unavailable, matching ``tests/test_simulation_smoke.py``.
"""

import pytest

from circuit_synth import Component, Net, circuit


def _sim_symbols_available() -> bool:
    """True only if PySpice is importable and ``Simulation_SPICE:VDC`` constructs.

    Constructing the component proves the KiCad Simulation_SPICE symbol library is
    discoverable on this machine (the converter never needs ngspice to build a
    netlist, only PySpice).
    """
    try:
        from circuit_synth.simulation.converter import PYSPICE_AVAILABLE

        if not PYSPICE_AVAILABLE:
            return False
        Component(symbol="Simulation_SPICE:VDC", ref="V1", value="1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _sim_symbols_available(),
    reason="PySpice or the KiCad Simulation_SPICE symbol library is not available",
)


@circuit(name="ExplicitSourceDivider")
def _explicit_divider():
    """Explicit 9 V source -> 1k/2k divider. VOUT = 9 * 2/3 = 6.0 V.

    VDC declares ``Sim.Pins "1=+ 2=-"``, so pin 1 -> VIN (+), pin 2 -> GND (-).
    """
    v1 = Component(symbol="Simulation_SPICE:VDC", ref="V1", value="9V")
    r1 = Component(symbol="Device:R", ref="R1", value="1k")
    r2 = Component(symbol="Device:R", ref="R2", value="2k")
    vin = Net("VIN")
    vout = Net("VOUT")
    gnd = Net("GND")
    v1[1] += vin
    v1[2] += gnd
    r1[1] += vin
    r1[2] += vout
    r2[1] += vout
    r2[2] += gnd


def _netlist(c) -> str:
    from circuit_synth.simulation.converter import SpiceConverter

    return str(SpiceConverter(c).convert())


def _v1_line(netlist: str) -> str:
    """The emitted SPICE line for source V1 (PySpice names it 'VV1')."""
    for line in netlist.splitlines():
        if line.startswith("VV1 "):
            return line
    raise AssertionError(f"no V source for V1 in netlist:\n{netlist}")


def test_explicit_voltage_source_emitted_with_value():
    """The declared VDC becomes a SPICE V source carrying its 9 V value."""
    parts = _v1_line(_netlist(_explicit_divider())).split()
    # 'VV1 <+node> <-node> <value>'
    assert float(parts[3]) == pytest.approx(9.0), parts


def test_explicit_source_polarity_follows_pin_numbers():
    """Pin 1 (+) -> VIN, pin 2 (-) -> GND(0): node order is 'VIN 0', not '0 VIN'."""
    parts = _v1_line(_netlist(_explicit_divider())).split()
    assert parts[1] == "VIN" and parts[2] == "0", parts


def test_explicit_source_suppresses_net_name_heuristic():
    """No auto 'V_supply' is added on a net an explicit source already drives."""
    netlist = _netlist(_explicit_divider())
    assert "V_supply" not in netlist, f"heuristic double-drove the net:\n{netlist}"


def test_polarity_swaps_with_pin_assignment():
    """Swapping which pin connects to VIN flips the emitted node order.

    This is the guard against the old behaviour, where nodes were sorted
    alphabetically and a source's polarity was independent of its pin wiring.
    """

    @circuit(name="SwappedSource")
    def swapped():
        v1 = Component(symbol="Simulation_SPICE:VDC", ref="V1", value="9V")
        r1 = Component(symbol="Device:R", ref="R1", value="1k")
        vin = Net("VIN")
        gnd = Net("GND")
        v1[1] += gnd  # + -> GND
        v1[2] += vin  # - -> VIN
        r1[1] += vin
        r1[2] += gnd

    parts = _v1_line(_netlist(swapped())).split()
    assert parts[1] == "0" and parts[2] == "VIN", parts


def _vsin_available() -> bool:
    """True only if the KiCad ``Simulation_SPICE:VSIN`` symbol constructs.

    ``VSIN`` (not ``VAC`` -- which does not exist in KiCad 10) is the AC/transient
    stimulus symbol; the converter gives it an ``AC`` magnitude so its driven node
    is the transfer function during an AC sweep.
    """
    try:
        from circuit_synth.simulation.converter import PYSPICE_AVAILABLE

        if not PYSPICE_AVAILABLE:
            return False
        Component(symbol="Simulation_SPICE:VSIN", ref="V1", value="1")
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _vsin_available(), reason="KiCad Simulation_SPICE:VSIN symbol not available"
)
def test_ac_source_emits_ac_magnitude():
    """A VSIN source emits an ``AC <mag>`` term (default 1) in its netlist line."""

    @circuit(name="ACSource")
    def ac_src():
        v1 = Component(symbol="Simulation_SPICE:VSIN", ref="V1")
        r1 = Component(symbol="Device:R", ref="R1", value="1k")
        vin = Net("VIN")
        gnd = Net("GND")
        v1[1] += vin
        v1[2] += gnd
        r1[1] += vin
        r1[2] += gnd

    line = _v1_line(_netlist(ac_src()))
    assert "AC 1" in line.upper(), line


@pytest.mark.skipif(
    not _vsin_available(), reason="KiCad Simulation_SPICE:VSIN symbol not available"
)
def test_ac_source_honors_explicit_magnitude():
    """An explicit ``value`` on a VSIN source becomes its AC magnitude."""

    @circuit(name="ACSourceMag")
    def ac_src():
        v1 = Component(symbol="Simulation_SPICE:VSIN", ref="V1", value="2V")
        r1 = Component(symbol="Device:R", ref="R1", value="1k")
        vin = Net("VIN")
        gnd = Net("GND")
        v1[1] += vin
        v1[2] += gnd
        r1[1] += vin
        r1[2] += gnd

    line = _v1_line(_netlist(ac_src())).upper()
    assert "AC 2" in line, line


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
def test_explicit_source_drives_operating_point():
    """End-to-end: the explicit 9 V source yields VIN=+9 V, VOUT=+6 V via ngspice."""
    result = _explicit_divider().simulate().operating_point()
    assert result.get_voltage("VIN") == pytest.approx(9.0, abs=0.01)
    assert result.get_voltage("VOUT") == pytest.approx(6.0, abs=0.01)
