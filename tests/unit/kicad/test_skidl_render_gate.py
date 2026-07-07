# -*- coding: utf-8 -*-

"""Unit tests for the gated SKiDL-render install (stage 19 Phase E).

These mock kicad-cli / render / netlist-compare so they need no skidl and no
KiCad libraries — they exercise the gate's decision logic and the file-swap /
restore mechanics directly.
"""

from pathlib import Path

import pytest

from circuit_synth.interop import skidl_export as _skidl_export
from circuit_synth.interop import netlist_compare as _netlist_compare
from circuit_synth.kicad import skidl_render_gate as gate


def _write(p, text="(kicad_sch)"):
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def project(tmp_path):
    """A project dir with a native root + child schematic already 'generated'."""
    d = tmp_path / "proj"
    d.mkdir()
    _write(d / "proj.kicad_sch", "(kicad_sch (native root))")
    _write(d / "proj__child.kicad_sch", "(kicad_sch (native child))")
    return d


def _install_mocks(monkeypatch, *, equivalent=True, save_ok=True, cli="cli",
                   render_raises=False, wires_text="(kicad_sch (wire ) (wire ))"):
    monkeypatch.setattr(gate, "_find_cli", lambda explicit=None: cli)
    monkeypatch.setattr(gate, "_export_netlist", lambda c, sch, out: (Path(out).write_text("net", encoding="utf-8"), True)[1])
    monkeypatch.setattr(gate, "_save_gate_ok", lambda c, sch: save_ok)

    class _Cmp:
        def __init__(self, ok):
            self.equivalent = ok
            self.messages = [] if ok else ["net group only in A: ['R1-1', 'R2-1']"]

    monkeypatch.setattr(_netlist_compare, "compare_netlists",
                        lambda a, b, **kw: _Cmp(equivalent))

    def _fake_render(circuit, out_dir, *, top_name, seed_placement=False, timeout=600, **kw):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        if render_raises:
            raise _skidl_export.SkidlRenderError("no interpreter")
        top = out / f"{top_name}.kicad_sch"
        _write(top, wires_text)
        _write(out / f"{top_name}__child.kicad_sch", wires_text)
        return top

    monkeypatch.setattr(_skidl_export, "render_with_skidl", _fake_render)


def test_install_success_swaps_and_keeps_native(monkeypatch, project):
    _install_mocks(monkeypatch, equivalent=True, save_ok=True)
    res = gate.render_skidl_and_install(object(), project, "proj")
    assert res["installed"] is True
    # Rendered schematics are now in the project dir (they contain wires).
    assert "(wire" in (project / "proj.kicad_sch").read_text(encoding="utf-8")
    # Native set preserved under native_ref/.
    assert (project / "native_ref" / "proj.kicad_sch").exists()
    assert (project / "native_ref" / "proj__child.kicad_sch").exists()
    assert res["native_ref"] == project / "native_ref"
    assert res["wires"] and res["wires"] > 0
    # Staging cleaned up.
    assert not (project / "skidl_render_staging").exists()


def test_equivalence_fail_keeps_native_no_swap(monkeypatch, project):
    _install_mocks(monkeypatch, equivalent=False)
    res = gate.render_skidl_and_install(object(), project, "proj")
    assert res["installed"] is False
    assert "equivalence" in res["reason"].lower()
    # Native untouched, nothing moved aside.
    assert "(native root)" in (project / "proj.kicad_sch").read_text(encoding="utf-8")
    assert not (project / "native_ref").exists()
    assert not (project / "skidl_render_staging").exists()


def test_save_gate_fail_keeps_native(monkeypatch, project):
    _install_mocks(monkeypatch, equivalent=True, save_ok=False)
    res = gate.render_skidl_and_install(object(), project, "proj")
    assert res["installed"] is False
    assert "save gate" in res["reason"].lower()
    assert "(native root)" in (project / "proj.kicad_sch").read_text(encoding="utf-8")
    assert not (project / "native_ref").exists()


def test_render_error_falls_back(monkeypatch, project):
    _install_mocks(monkeypatch, render_raises=True)
    res = gate.render_skidl_and_install(object(), project, "proj")
    assert res["installed"] is False
    assert "render failed" in res["reason"].lower()
    assert "(native root)" in (project / "proj.kicad_sch").read_text(encoding="utf-8")


def test_no_kicad_cli_falls_back(monkeypatch, project):
    _install_mocks(monkeypatch, cli=None)
    res = gate.render_skidl_and_install(object(), project, "proj")
    assert res["installed"] is False
    assert "kicad-cli" in res["reason"].lower()


def test_missing_native_root_falls_back(monkeypatch, tmp_path):
    _install_mocks(monkeypatch)
    empty = tmp_path / "empty"
    empty.mkdir()
    res = gate.render_skidl_and_install(object(), empty, "proj")
    assert res["installed"] is False
    assert "native root" in res["reason"].lower()


def test_restore_native_undoes_install(monkeypatch, project):
    _install_mocks(monkeypatch, equivalent=True, save_ok=True)
    gate.render_skidl_and_install(object(), project, "proj")
    assert "(wire" in (project / "proj.kicad_sch").read_text(encoding="utf-8")

    restored = gate.restore_native(project, "proj")
    assert restored is True
    txt = (project / "proj.kicad_sch").read_text(encoding="utf-8")
    assert "(native root)" in txt
    assert (project / "proj__child.kicad_sch").exists()
    assert not (project / "native_ref").exists()


def test_restore_native_noop_without_native_ref(project):
    assert gate.restore_native(project, "proj") is False


def test_generate_kicad_project_rejects_bad_renderer():
    # Validation happens before any generation, so no KiCad libs are needed.
    from circuit_synth import Net, circuit

    @circuit(name="tiny")
    def _tiny():
        Net("X")

    with pytest.raises(ValueError, match="renderer must be"):
        _tiny().generate_kicad_project("bogus_proj", renderer="bogus")
