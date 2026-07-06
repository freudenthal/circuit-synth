"""Coupled inductors / transformer (K elements) + flyback macromodel (Stage 21).

21.1: a KiCad ``Device:Transformer_1P_1S`` symbol (or ``Sim.Device=TRANSFORMER``)
simulates as two coupled inductors + a ``K`` card. The transformer is a real
user part; only its SPICE model is emitted. Pin names AA/AB (primary) and SA/SB
(secondary); node order puts the SPICE dots at AA/SA, matching the symbol's
printed dots, so flyback polarity comes from user wiring.

21.2: ``Sim.Device=FLYBACK`` emits the Stage-20.3-style open-loop computed-duty
macromodel with a low-side switch and a drain avalanche clamp (leakage into an
ideal switch otherwise rings to kV -- spiked).

Netlist-shape + validation tests here (no ngspice); the live flyback run is in
``tests/e2e/test_flyback_macromodel.py``.
"""

import numpy as np
import pytest

from circuit_synth import Component, Net, circuit
from circuit_synth.simulation.converter import (
    SimulationValidationError,
    SpiceConverter,
)


# --- Pure classification -----------------------------------------------------


def test_classify_transformer_symbol():
    assert SpiceConverter._classify("Device:Transformer_1P_1S") == "transformer"


def test_sim_device_transformer_kinds():
    assert SpiceConverter._SIM_DEVICE_KINDS.get("TRANSFORMER") == "transformer"
    assert SpiceConverter._SIM_DEVICE_KINDS.get("XFMR") == "transformer"


# --- Fixtures ----------------------------------------------------------------


def _symbols_available() -> bool:
    try:
        from circuit_synth.simulation.converter import PYSPICE_AVAILABLE

        if not PYSPICE_AVAILABLE:
            return False
        Component(symbol="Device:Transformer_1P_1S", ref="T1")
        return True
    except Exception:
        return False


needs_symbols = pytest.mark.skipif(
    not _symbols_available(),
    reason="PySpice or the KiCad Transformer_1P_1S symbol not available",
)


def _connect_by_name(comp, pin_name, net):
    for num, pin in getattr(comp, "_pins", {}).items():
        if (getattr(pin, "name", "") or "").strip().upper() == pin_name.upper():
            comp[num] += net
            return
    raise AssertionError(f"{comp.ref} has no pin named {pin_name}")


def _xfmr_circuit(sim_params="lp=100u n=0.5", wire_all=True):
    """A transformer driven by a source on the primary, loaded on the secondary."""

    @circuit(name="xfmr_unit")
    def _c():
        t1 = Component(
            symbol="Device:Transformer_1P_1S",
            ref="T1",
            **({"Sim.Params": sim_params} if sim_params else {}),
        )
        v1 = Component(symbol="Simulation_SPICE:VSIN", ref="V1", amplitude="1")
        rl = Component(symbol="Device:R", ref="RL", value="50")
        vin, gnd, sec = Net("VIN"), Net("GND"), Net("SEC")
        v1[1] += vin
        v1[2] += gnd
        _connect_by_name(t1, "AA", vin)
        _connect_by_name(t1, "AB", gnd)
        _connect_by_name(t1, "SA", gnd)  # shared sim ground (isolation caveat)
        if wire_all:
            _connect_by_name(t1, "SB", sec)
            rl[1] += sec
        else:
            rl[1] += gnd
        rl[2] += gnd

    return _c()


def _netlist(c) -> list:
    return str(SpiceConverter(c).convert()).splitlines()


# --- 21.1 netlist shape -------------------------------------------------------


@needs_symbols
def test_transformer_emits_coupled_inductors():
    lines = _netlist(_xfmr_circuit())
    low = [ln.lower() for ln in lines]
    # Primary: dot (first node) at AA's net (vin); LP as given.
    lp = next((ln for ln in low if ln.startswith("lt1_p ")), None)
    assert lp is not None and lp.split()[1] == "vin", lines
    assert float(lp.split()[3]) == pytest.approx(100e-6)
    # Secondary: dot at SA's net (gnd -> spice node 0); LS = LP*N^2 = 25u.
    ls = next((ln for ln in low if ln.startswith("lt1_s ")), None)
    assert ls is not None and ls.split()[1] == "0", lines
    assert float(ls.split()[3]) == pytest.approx(25e-6)
    # K card couples the two windings at the default 0.999.
    k = next((ln for ln in low if ln.startswith("kt1 ")), None)
    assert k is not None and k.split()[1:3] == ["lt1_p", "lt1_s"], lines
    assert float(k.split()[3]) == pytest.approx(0.999)


@needs_symbols
def test_transformer_explicit_ls_wins_and_k_override():
    lines = _netlist(_xfmr_circuit(sim_params="lp=100u n=0.5 ls=30u k=1"))
    low = [ln.lower() for ln in lines]
    ls = next(ln for ln in low if ln.startswith("lt1_s "))
    assert float(ls.split()[3]) == pytest.approx(30e-6)  # explicit ls beats lp*n^2
    k = next(ln for ln in low if ln.startswith("kt1 "))
    assert float(k.split()[3]) == pytest.approx(1.0)


@needs_symbols
def test_transformer_provenance():
    conv = SpiceConverter(_xfmr_circuit())
    conv.convert()
    prov = conv.model_provenance.get("T1")
    assert prov is not None and prov.kind == "transformer", prov
    assert "xfmr" in prov.name.lower(), prov


# --- 21.1 validation ----------------------------------------------------------


@needs_symbols
def test_transformer_missing_params_is_validation_error():
    with pytest.raises(SimulationValidationError) as exc:
        _netlist(_xfmr_circuit(sim_params=None))
    msg = str(exc.value)
    assert "T1" in msg and "lp" in msg.lower()


@needs_symbols
def test_transformer_missing_ratio_is_validation_error():
    with pytest.raises(SimulationValidationError) as exc:
        _netlist(_xfmr_circuit(sim_params="lp=100u"))  # no n, no ls
    assert "T1" in str(exc.value)


@needs_symbols
def test_transformer_bad_k_is_validation_error():
    with pytest.raises(SimulationValidationError) as exc:
        _netlist(_xfmr_circuit(sim_params="lp=100u n=0.5 k=1.5"))
    msg = str(exc.value)
    assert "T1" in msg and "k" in msg.lower()


@needs_symbols
def test_transformer_unconnected_pin_is_validation_error():
    with pytest.raises(SimulationValidationError) as exc:
        _netlist(_xfmr_circuit(wire_all=False))
    msg = str(exc.value)
    assert "T1" in msg and ("pin" in msg.lower() or "connected" in msg.lower())
