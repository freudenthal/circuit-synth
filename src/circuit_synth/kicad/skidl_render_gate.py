# -*- coding: utf-8 -*-

"""Gated SKiDL-render installation for ``generate_kicad_project(renderer="skidl")``
(stage 19 Phase E).

The routed SKiDL render can BECOME the project's schematic, but only after it
passes two gates against the native render, which is kept as a fallback:

  1. **Netlist equivalence** — the rendered schematic must be pin-partition
     equivalent to the native one (``interop.netlist_compare``), so switching
     renderers never changes connectivity.
  2. **Hardened save gate** — every rendered ``.kicad_sch`` must survive
     ``kicad-cli sch upgrade`` (rc==0, non-empty) and reload, so it can't
     segfault KiCad on save.

Only if BOTH pass is the native set moved aside into ``native_ref/`` and the
rendered set installed in its place. ANY failure returns ``installed=False`` with
a reason; the caller keeps the native output (the load-bearing fallback). This
module never raises for an expected failure — it degrades gracefully.
"""

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class _GateResult(dict):
    """A plain dict result; attribute access is just sugar for readability."""


def _find_cli(explicit=None):
    from .sch_gen.erc_gate import ErcUnavailable, _find_kicad_cli

    try:
        return _find_kicad_cli(explicit)
    except ErcUnavailable:
        return None


def _export_netlist(cli, sch, out):
    r = subprocess.run(
        [cli, "sch", "export", "netlist", "--output", str(out), str(sch)],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def _save_gate_ok(cli, sch):
    """rc==0 on `sch upgrade` (on a COPY — it rewrites), size>0, and reloads."""
    tmp = sch.with_name("_savegate_" + sch.name)
    try:
        shutil.copy(sch, tmp)
        up = subprocess.run(
            [cli, "sch", "upgrade", str(tmp)], capture_output=True, text=True
        )
        if up.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            return False
        # Reload probe: netlist export must also succeed on the upgraded file.
        return _export_netlist(cli, tmp, tmp.with_suffix(".net"))
    finally:
        for p in (tmp, tmp.with_suffix(".net")):
            try:
                p.unlink()
            except OSError:
                pass


def render_skidl_and_install(
    circuit, project_dir, base_name, *,
    seed_placement=False, kicad_cli=None, timeout=600,
):
    """Render *circuit* via SKiDL and, if it passes both gates, install it.

    Returns a ``_GateResult`` dict with keys:
      installed (bool), reason (str|None), equivalence (NetlistComparison|None),
      native_ref (Path|None), seed_placement (bool), wires (int|None).
    """
    from ..interop.netlist_compare import compare_netlists
    from ..interop.skidl_export import SkidlRenderError, render_with_skidl

    project_dir = Path(project_dir)
    res = _GateResult(
        installed=False, reason=None, equivalence=None, native_ref=None,
        seed_placement=seed_placement, wires=None,
    )

    root_native = project_dir / f"{base_name}.kicad_sch"
    if not root_native.exists():
        res["reason"] = f"native root schematic missing ({root_native.name})"
        return res

    cli = kicad_cli or _find_cli()
    if not cli:
        res["reason"] = "kicad-cli unavailable (cannot run equivalence/save gates)"
        return res

    # Snapshot the native schematic set before rendering.
    native_set = sorted(project_dir.glob("*.kicad_sch"))
    staging = project_dir / "skidl_render_staging"
    shutil.rmtree(staging, ignore_errors=True)

    native_net = staging.parent / "_native_ref.net"
    staging.mkdir(parents=True, exist_ok=True)
    if not _export_netlist(cli, root_native, native_net):
        res["reason"] = "could not export native reference netlist"
        shutil.rmtree(staging, ignore_errors=True)
        return res

    # Step: render via SKiDL into staging.
    try:
        rendered_top = render_with_skidl(
            circuit, staging, top_name=base_name,
            seed_placement=seed_placement, timeout=timeout,
        )
    except SkidlRenderError as e:
        res["reason"] = f"SKiDL render failed: {e}"
        shutil.rmtree(staging, ignore_errors=True)
        native_net.unlink(missing_ok=True)
        return res
    rendered_top = Path(rendered_top)

    # Gate 1: netlist equivalence (native vs rendered).
    rendered_net = staging / "_rendered.net"
    if not _export_netlist(cli, rendered_top, rendered_net):
        res["reason"] = "could not export rendered netlist (render may be malformed)"
        shutil.rmtree(staging, ignore_errors=True)
        native_net.unlink(missing_ok=True)
        return res
    cmp = compare_netlists(native_net, rendered_net, check_footprint=False)
    res["equivalence"] = cmp
    if not cmp.equivalent:
        res["reason"] = (
            "netlist equivalence FAILED (rendered schematic connectivity differs "
            f"from native): {'; '.join(cmp.messages[:5])}"
        )
        shutil.rmtree(staging, ignore_errors=True)
        native_net.unlink(missing_ok=True)
        return res

    # Gate 2: hardened save gate on every rendered sheet.
    rendered_set = sorted(staging.glob("*.kicad_sch"))
    for sch in rendered_set:
        if not _save_gate_ok(cli, sch):
            res["reason"] = f"save gate FAILED on rendered sheet {sch.name}"
            shutil.rmtree(staging, ignore_errors=True)
            native_net.unlink(missing_ok=True)
            return res

    # Both gates passed -> install. Move native aside, rendered in.
    native_ref = project_dir / "native_ref"
    native_ref.mkdir(exist_ok=True)
    for f in native_set:
        shutil.move(str(f), str(native_ref / f.name))
    for f in rendered_set:
        shutil.move(str(f), str(project_dir / f.name))
    shutil.rmtree(staging, ignore_errors=True)
    native_net.unlink(missing_ok=True)

    res["installed"] = True
    res["native_ref"] = native_ref
    res["wires"] = sum(
        _count_wires(project_dir / f.name) for f in rendered_set
    )
    return res


def restore_native(project_dir, base_name):
    """Undo an install: move ``native_ref/`` schematics back, drop the rendered set.

    Returns True if a restore happened. Used by the caller if a post-install step
    (e.g. ERC autofix) leaves the rendered schematic unsavable.
    """
    project_dir = Path(project_dir)
    native_ref = project_dir / "native_ref"
    if not native_ref.is_dir():
        return False
    saved = sorted(native_ref.glob("*.kicad_sch"))
    if not saved:
        return False
    # Remove the currently-installed (rendered) schematics, then restore native.
    for f in project_dir.glob("*.kicad_sch"):
        f.unlink(missing_ok=True)
    for f in saved:
        shutil.move(str(f), str(project_dir / f.name))
    shutil.rmtree(native_ref, ignore_errors=True)
    return True


def _count_wires(sch):
    import re

    try:
        return len(re.findall(r"\(wire\b", sch.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return 0
