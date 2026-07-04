"""Semiconductor ``.model`` cards (Stage 7.3).

A diode/BJT/MOSFET references a SPICE model by name; before this stage the
converter emitted the device line but never a matching ``.model`` card, so any
such circuit errored in ngspice on an undefined model. The converter now ships a
small library of generic models (``DefaultDiode``/``DefaultNPN``/``DefaultPNP``/
``DefaultNMOS``/``DefaultPMOS``) and emits a ``.model`` card for each model a
circuit actually uses; ``validate()`` (strict, the default) rejects a device that
references an unresolvable model.

Coverage:

* **Netlist-level** (no ngspice): the emitted netlist carries the right
  ``.model`` card, only for models actually used, and an unresolved custom model
  raises ``SimulationValidationError``.
* **End-to-end** (skipped without a loadable ngspice): a diode + resistor op
  point runs -- proof the model card lets ngspice resolve the device.

Skips cleanly when PySpice or the KiCad ``Device:D`` symbol is unavailable.
"""

import pytest

from circuit_synth import Component, Net, circuit
from circuit_synth.simulation.converter import (
    SimulationValidationError,
    SpiceConverter,
)


def _sim_symbols_available() -> bool:
    """True only if PySpice is importable and the semiconductor symbols construct."""
    try:
        from circuit_synth.simulation.converter import PYSPICE_AVAILABLE

        if not PYSPICE_AVAILABLE:
            return False
        Component(symbol="Device:D", ref="D1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _sim_symbols_available(),
    reason="PySpice or the KiCad semiconductor symbols are not available",
)


def _netlist(c) -> str:
    return str(SpiceConverter(c).convert())


@circuit(name="DiodeRectifier")
def _diode_circuit():
    """VDC 5 V -> R1 -> D1 -> GND. Diode uses the DefaultDiode generic."""
    v1 = Component(symbol="Simulation_SPICE:VDC", ref="V1", value="5V")
    r1 = Component(symbol="Device:R", ref="R1", value="1k")
    d1 = Component(symbol="Device:D", ref="D1")
    vin = Net("VIN")
    vn = Net("VN")
    gnd = Net("GND")
    v1[1] += vin
    v1[2] += gnd
    r1[1] += vin
    r1[2] += vn
    d1[1] += vn
    d1[2] += gnd


def test_diode_emits_model_card():
    """A diode's netlist carries a ``.model DefaultDiode D (...)`` card."""
    netlist = _netlist(_diode_circuit())
    assert ".model DefaultDiode D" in netlist, netlist


def test_only_used_models_emitted():
    """A diode-only circuit emits no BJT/MOSFET model cards."""
    netlist = _netlist(_diode_circuit())
    assert "NPN" not in netlist and "NMOS" not in netlist, netlist


def test_npn_bjt_emits_npn_model():
    """An NPN BJT (BC547) emits a ``.model DefaultNPN NPN`` card."""

    @circuit(name="NpnStage")
    def npn():
        v1 = Component(symbol="Simulation_SPICE:VDC", ref="V1", value="5V")
        r1 = Component(symbol="Device:R", ref="R1", value="1k")
        r2 = Component(symbol="Device:R", ref="R2", value="100k")
        q1 = Component(symbol="Transistor_BJT:BC547", ref="Q1")
        vcc = Net("VCC")
        vc = Net("VC")
        vb = Net("VB")
        gnd = Net("GND")
        v1[1] += vcc
        v1[2] += gnd
        r1[1] += vcc
        r1[2] += vc
        r2[1] += vcc
        r2[2] += vb
        q1[1] += vc
        q1[2] += vb
        q1[3] += gnd

    netlist = _netlist(npn())
    assert ".model DefaultNPN NPN" in netlist, netlist


def test_pnp_selected_by_value_keyword():
    """value='pnp' selects the DefaultPNP generic regardless of symbol."""

    @circuit(name="PnpStage")
    def pnp():
        v1 = Component(symbol="Simulation_SPICE:VDC", ref="V1", value="5V")
        r1 = Component(symbol="Device:R", ref="R1", value="1k")
        r2 = Component(symbol="Device:R", ref="R2", value="100k")
        q1 = Component(symbol="Transistor_BJT:BC547", ref="Q1", value="pnp")
        vcc = Net("VCC")
        vc = Net("VC")
        vb = Net("VB")
        gnd = Net("GND")
        v1[1] += vcc
        v1[2] += gnd
        r1[1] += vcc
        r1[2] += vc
        r2[1] += vcc
        r2[2] += vb
        q1[1] += vc
        q1[2] += vb
        q1[3] += gnd

    netlist = _netlist(pnp())
    assert ".model DefaultPNP PNP" in netlist, netlist
    assert "DefaultNPN" not in netlist, netlist


def test_nmos_emits_nmos_model():
    """An NMOS (2N7000) emits a ``.model DefaultNMOS NMOS`` card."""

    @circuit(name="NmosStage")
    def nmos():
        v1 = Component(symbol="Simulation_SPICE:VDC", ref="V1", value="5V")
        r1 = Component(symbol="Device:R", ref="R1", value="1k")
        r2 = Component(symbol="Device:R", ref="R2", value="100k")
        m1 = Component(symbol="Transistor_FET:2N7000", ref="M1")
        vcc = Net("VCC")
        vd = Net("VD")
        vg = Net("VG")
        gnd = Net("GND")
        v1[1] += vcc
        v1[2] += gnd
        r1[1] += vcc
        r1[2] += vd
        r2[1] += vcc
        r2[2] += vg
        m1[1] += vd
        m1[2] += vg
        m1[3] += gnd

    netlist = _netlist(nmos())
    assert ".model DefaultNMOS NMOS" in netlist, netlist


def test_unresolved_model_raises():
    """A device naming a model the ladder can't resolve fails strict validation.

    (``1N4148`` now resolves to the datasheet-fit library card -- see Stage 9.2 --
    so this uses a name that is in neither the generics nor the model library.)
    """

    @circuit(name="BadModel")
    def bad():
        v1 = Component(symbol="Simulation_SPICE:VDC", ref="V1", value="5V")
        r1 = Component(symbol="Device:R", ref="R1", value="1k")
        d1 = Component(symbol="Device:D", ref="D1", value="NOSUCHPART9000")
        vin = Net("VIN")
        vn = Net("VN")
        gnd = Net("GND")
        v1[1] += vin
        v1[2] += gnd
        r1[1] += vin
        r1[2] += vn
        d1[1] += vn
        d1[2] += gnd

    with pytest.raises(SimulationValidationError) as exc:
        SpiceConverter(bad()).convert()
    assert "NOSUCHPART9000" in str(exc.value)


def test_lenient_convert_skips_model_validation():
    """convert(strict=False) still emits no card for an unresolved model...

    but does not raise -- the lenient path is for exploratory conversion.
    """

    @circuit(name="BadModelLenient")
    def bad():
        v1 = Component(symbol="Simulation_SPICE:VDC", ref="V1", value="5V")
        r1 = Component(symbol="Device:R", ref="R1", value="1k")
        d1 = Component(symbol="Device:D", ref="D1", value="NOSUCHPART9000")
        vin = Net("VIN")
        vn = Net("VN")
        gnd = Net("GND")
        v1[1] += vin
        v1[2] += gnd
        r1[1] += vin
        r1[2] += vn
        d1[1] += vn
        d1[2] += gnd

    netlist = str(SpiceConverter(bad()).convert(strict=False))
    assert "D1 " in netlist  # device line present
    assert ".model NOSUCHPART9000" not in netlist  # no card for the unknown model


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
def test_diode_operating_point_runs():
    """End-to-end: the diode's .model card lets ngspice solve the op point.

    5 V -> 1k -> forward diode -> GND. The generic diode drops ~0.6-0.8 V, so the
    R/D node sits well between 0 and 5 V. The point of the test is that ngspice
    does NOT error on an undefined model -- which it would without the card.
    """
    result = _diode_circuit().simulate().operating_point()
    vn = result.get_voltage("VN")
    assert 0.0 < vn < 5.0
