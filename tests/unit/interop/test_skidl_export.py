"""Unit tests for the circuit_synth -> SKiDL render adapter (Stage 13, Phase B/C).

These are pure-Python: they exercise script *emission* and netlist *comparison*
without importing skidl or running kicad-cli. The actual render is covered by the
``requires_skidl`` e2e in ``tests/e2e/test_skidl_render.py``.
"""

import pytest

from circuit_synth import Component, Net, circuit
from circuit_synth.interop import export_skidl_script
from circuit_synth.interop.netlist_compare import (
    ParsedNetlist,
    compare_netlists,
    parse_netlist,
)

pytestmark = pytest.mark.unit

R_FP = "Resistor_SMD:R_0603_1608Metric"


@circuit(name="Divider")
def _divider():
    r1 = Component(symbol="Device:R", ref="R", value="1k", footprint=R_FP)
    r2 = Component(symbol="Device:R", ref="R", value="2k", footprint=R_FP)
    vin, vout, gnd = Net("VIN_5V"), Net("VOUT_3V3"), Net("GND")
    r1[1] += vin
    r1[2] += vout
    r2[1] += vout
    r2[2] += gnd


@circuit(name="child")
def _child(v5, gnd):
    r = Component(symbol="Device:R", ref="R", value="10k", footprint=R_FP)
    r[1] += v5
    r[2] += gnd


@circuit(name="Hier")
def _hier():
    v5, gnd = Net("V5"), Net("GND")
    src = Component(symbol="Device:R", ref="R", value="100", footprint=R_FP)
    src[1] += Net("VIN")
    src[2] += v5
    _child(v5, gnd)


@circuit(name="child_local")
def _child_local(v5, gnd):
    # Two series resistors create an internal MID net touched ONLY by this child
    # (single non-top group, non-power) -> it should be localized (Blocker A).
    r1 = Component(symbol="Device:R", ref="R", value="10k", footprint=R_FP)
    r2 = Component(symbol="Device:R", ref="R", value="20k", footprint=R_FP)
    mid = Net("MID")
    r1[1] += v5
    r1[2] += mid
    r2[1] += mid
    r2[2] += gnd


@circuit(name="HierLocal")
def _hier_local():
    v5, gnd = Net("V5"), Net("GND")
    src = Component(symbol="Device:R", ref="R", value="100", footprint=R_FP)
    src[1] += Net("VIN")
    src[2] += v5
    _child_local(v5, gnd)


# --------------------------------------------------------------------------- #
# Script emission
# --------------------------------------------------------------------------- #


@circuit(name="Cluster")
def _cluster_demo():
    u = Component(symbol="Amplifier_Operational:TL072", ref="U", value="TL072")
    cap = Component(
        symbol="Device:C", ref="C", value="100nF", footprint=R_FP, cluster="U1.8"
    )
    plain = Component(symbol="Device:C", ref="C", value="1uF", footprint=R_FP)
    vcc, gnd = Net("VCC"), Net("GND")
    u[8] += vcc
    cap[1] += vcc
    cap[2] += gnd
    plain[1] += vcc
    plain[2] += gnd


def test_cluster_field_survives_validation_and_is_visible():
    """Component(cluster=...) stores the hint in _extra_fields (no ValidationError)."""
    c = Component(symbol="Device:C", ref="C2", value="100nF", cluster="U3.8")
    assert c._extra_fields.get("cluster") == "U3.8"


def test_export_emits_cluster_kwarg(tmp_path):
    """The declared adjacency rides through to the emitted Part(...) kwargs."""
    text = export_skidl_script(_cluster_demo(), tmp_path / "clus_skidl.py").read_text(
        encoding="utf-8"
    )
    # The annotated cap carries cluster=; the un-annotated one does not.
    assert "cluster='U1.8'" in text
    assert text.count("cluster=") == 1


def test_export_divider_script_structure(tmp_path):
    path = export_skidl_script(_divider(), tmp_path / "d_skidl.py")
    text = path.read_text(encoding="utf-8")

    # Header + imports for a self-contained skidl script.
    assert "import os" in text
    assert 'os.environ.setdefault("KICAD9_SYMBOL_DIR"' in text
    assert (
        "from skidl import POWER, Net, Part, generate_schematic, reset, subcircuit"
        in text
    )

    # Both resistors emitted as Parts with their ref/value/footprint.
    assert "Part('Device', 'R', ref='R1', value='1k'" in text
    assert "Part('Device', 'R', ref='R2', value='2k'" in text
    assert R_FP in text

    # Pin connections use pin numbers.
    assert "R1[1] +=" in text
    assert "R2[2] +=" in text

    # GND is auto-detected as a power net -> POWER drive so auto_stub emits a symbol.
    assert "GND.drive = POWER" in text

    # Footer generates the schematic and prints the success sentinel.
    assert "generate_schematic(" in text
    assert 'auto_stub_fallback="labels"' in text
    assert "SKIDL_RENDER_OK" in text


def test_export_hierarchical_emits_multiple_subcircuits(tmp_path):
    path = export_skidl_script(_hier(), tmp_path / "h_skidl.py")
    text = path.read_text(encoding="utf-8")
    # One @subcircuit per circuit_synth circuit node that owns components
    # (the top 'Hier' with its series R, and the 'child' group).
    assert text.count("@subcircuit") == 2
    # The shared V5 rail is passed as an argument into the child group.
    assert "def build():" in text


def test_export_localizes_single_group_internal_net(tmp_path):
    # Blocker A: a net touched by exactly one non-top subcircuit (and not power)
    # is declared as a LOCAL inside that subcircuit's body so skidl wires it,
    # instead of being homed at the top and passed through as a label.
    text = export_skidl_script(_hier_local(), tmp_path / "hl_skidl.py").read_text(
        encoding="utf-8"
    )
    defs, sep, build = text.partition("def build():")
    assert sep, "expected a build() function"

    # MID is internal to the child -> declared in a subcircuit body, NOT in build().
    assert "MID = Net('MID')" in defs
    assert "Net('MID')" not in build
    # ...and it is NOT a parameter of the child subcircuit.
    child_sig = next(
        ln for ln in defs.splitlines() if ln.startswith("def _HierLocal_child_local(")
    )
    assert "MID" not in child_sig
    assert "GND" in child_sig  # power net still passed in as a param

    # The shared V5 rail spans top + child -> created at top level and passed in.
    assert "V5 = Net('V5')" in build
    # GND is power and touched by one group -> stays top-level with POWER drive.
    assert "GND = Net('GND')" in build
    assert "GND.drive = POWER" in build


def test_export_top_owned_and_power_nets_stay_top_level(tmp_path):
    # A net the TOP group owns (VIN here) is NOT localized even though only one
    # group touches it; and a power net is never localized. Both are created in
    # build() rather than inside a subcircuit body.
    text = export_skidl_script(_hier_local(), tmp_path / "hl2_skidl.py").read_text(
        encoding="utf-8"
    )
    defs, _sep, build = text.partition("def build():")
    assert "VIN = Net('VIN')" in build  # top-owned -> top level
    assert "Net('VIN')" not in defs  # not declared inside any subcircuit body


def test_export_flatness_and_auto_stub_are_configurable(tmp_path):
    path = export_skidl_script(
        _divider(), tmp_path / "cfg_skidl.py", flatness=1.0, auto_stub=False
    )
    text = path.read_text(encoding="utf-8")
    assert "flatness=1.0" in text
    assert "auto_stub=False" in text


def test_export_seed_placement_emitted_only_when_true(tmp_path):
    # Default False -> the kwarg is absent (stock-skidl compatible).
    off = export_skidl_script(_divider(), tmp_path / "off_skidl.py").read_text(
        encoding="utf-8"
    )
    assert "seed_placement" not in off

    # Enabled -> the emitted generate_schematic call carries seed_placement=True.
    on = export_skidl_script(
        _divider(), tmp_path / "on_skidl.py", seed_placement=True
    ).read_text(encoding="utf-8")
    assert "seed_placement=True" in on


def test_export_seed_always_emitted_for_determinism(tmp_path):
    # The RNG seed is ALWAYS emitted (unlike seed_placement) so renders are
    # deterministic: without it skidl does random.seed(None) == OS entropy.
    default = export_skidl_script(_divider(), tmp_path / "s_def_skidl.py").read_text(
        encoding="utf-8"
    )
    assert "seed=1" in default

    # An explicit override propagates verbatim.
    override = export_skidl_script(
        _divider(), tmp_path / "s_ov_skidl.py", seed=7
    ).read_text(encoding="utf-8")
    assert "seed=7" in override


def test_export_small_subcircuit_max_keeps_wires_by_default(tmp_path):
    # Default 0 -> small hierarchical sheets keep their wires (skidl's cosmetic
    # blanket-stub of <=6-net subcircuits is disabled).
    default = export_skidl_script(_divider(), tmp_path / "def_skidl.py").read_text(
        encoding="utf-8"
    )
    assert "auto_stub_small_subcircuit_max=0" in default

    # Explicit override is emitted verbatim.
    override = export_skidl_script(
        _divider(), tmp_path / "ov_skidl.py", small_subcircuit_max=6
    ).read_text(encoding="utf-8")
    assert "auto_stub_small_subcircuit_max=6" in override

    # Absent when auto_stub is off (the option is meaningless without it).
    no_stub = export_skidl_script(
        _divider(), tmp_path / "ns_skidl.py", auto_stub=False
    ).read_text(encoding="utf-8")
    assert "auto_stub_small_subcircuit_max" not in no_stub


def test_export_empty_circuit_raises(tmp_path):
    @circuit(name="Empty")
    def _empty():
        # no components
        Net("DANGLING")

    with pytest.raises(ValueError, match="no components"):
        export_skidl_script(_empty(), tmp_path / "e_skidl.py")


def test_interop_import_does_not_require_skidl():
    # circuit_synth.interop must import with no skidl installed (this env has none).
    import importlib

    import circuit_synth.interop as interop

    importlib.reload(interop)
    assert hasattr(interop, "export_skidl_script")
    assert hasattr(interop, "render_with_skidl")


# --------------------------------------------------------------------------- #
# Netlist comparison
# --------------------------------------------------------------------------- #

_NETLIST_A = """
(export (version "E")
  (components
    (comp (ref "R1") (value "1k") (footprint "Resistor_SMD:R_0603_1608Metric"))
    (comp (ref "R2") (value "2k") (footprint "Resistor_SMD:R_0603_1608Metric")))
  (nets
    (net (code "1") (name "VIN_5V")
      (node (ref "R1") (pin "1") (pintype "passive")))
    (net (code "2") (name "MID")
      (node (ref "R1") (pin "2") (pintype "passive"))
      (node (ref "R2") (pin "1") (pintype "passive")))
    (net (code "3") (name "GND")
      (node (ref "R2") (pin "2") (pintype "passive"))
      (node (ref "#PWR01") (pin "1") (pintype "power_in")))))
"""

# Same connectivity, different net names + a differently-named power pseudo-symbol.
_NETLIST_B = """
(export (version "E")
  (components
    (comp (ref "R1") (value "1k") (footprint "Resistor_SMD:R_0603_1608Metric"))
    (comp (ref "R2") (value "2k") (footprint "Resistor_SMD:R_0603_1608Metric")))
  (nets
    (net (code "1") (name "N$7")
      (node (ref "R1") (pin "1")))
    (net (code "2") (name "VOUT")
      (node (ref "R2") (pin "1"))
      (node (ref "R1") (pin "2")))
    (net (code "3") (name "GND")
      (node (ref "#PWR099") (pin "1"))
      (node (ref "R2") (pin "2")))))
"""


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_parse_netlist_extracts_components_and_nets(tmp_path):
    nl = parse_netlist(_write(tmp_path, "a.net", _NETLIST_A))
    assert set(nl.components) == {"R1", "R2"}
    assert nl.components["R1"]["value"] == "1k"
    # partition ignores the #PWR pseudo pin by default
    part = nl.partition()
    assert frozenset({("R1", "2"), ("R2", "1")}) in part
    assert frozenset({("R2", "2")}) in part  # GND minus the pseudo pin


def test_compare_equivalent_despite_names_and_power_symbols(tmp_path):
    a = _write(tmp_path, "a.net", _NETLIST_A)
    b = _write(tmp_path, "b.net", _NETLIST_B)
    result = compare_netlists(a, b)
    assert result.equivalent, result.messages
    assert bool(result) is True


def test_compare_detects_connectivity_difference(tmp_path):
    broken = _NETLIST_B.replace(
        '(node (ref "R1") (pin "2"))', '(node (ref "R1") (pin "1"))'
    )
    a = _write(tmp_path, "a.net", _NETLIST_A)
    b = _write(tmp_path, "broken.net", broken)
    result = compare_netlists(a, b)
    assert not result.equivalent
    assert any("net group" in m for m in result.messages)


def test_compare_detects_value_mismatch(tmp_path):
    changed = _NETLIST_B.replace('(value "2k")', '(value "9k9")')
    a = _write(tmp_path, "a.net", _NETLIST_A)
    b = _write(tmp_path, "changed.net", changed)
    result = compare_netlists(a, b)
    assert not result.equivalent
    assert any("value differs" in m for m in result.messages)
