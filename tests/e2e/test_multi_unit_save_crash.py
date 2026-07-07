"""E2E: multi-unit symbols must not crash KiCad's writer on save (bug #B).

Before the Stage-23.1 fix, a design placing a multi-unit symbol (LM358,
ADA4807-2ACP) generated a schematic whose extra units carried a dangling
``(path "/")``; ``kicad-cli sch upgrade --force`` segfaulted (rc=139) and
truncated the file to 0 bytes -- a headless proxy for the KiCad GUI save crash.

Requires KiCad 10's kicad-cli (skips cleanly if absent).
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from circuit_synth import Component, Net, circuit
from circuit_synth.kicad.sch_gen.erc_gate import _find_kicad_cli

pytestmark = pytest.mark.e2e


def assert_kicad_save_ok(sch_path, kicad_cli=None):
    """Assert KiCad can re-save ``sch_path`` without crashing.

    Copies the file, runs ``kicad-cli sch upgrade --force`` on the copy (KiCad's
    own writer -- a headless proxy for a GUI save), and asserts the write
    round-trips: ``rc == 0`` AND the file is non-empty AND ``kicad-cli sch erc``
    reloads it. This 3-part gate guards the traps that hid bug #B:

      1. Pipe rc trap -- ``... | tail; echo $?`` reports *tail's* rc, so a 139
         segfault reads as 0. We use ``subprocess.run`` with a list argv (no
         shell) and read ``.returncode`` directly.
      2. 0-byte truncation -- a mid-write crash can leave a 0-byte file, so
         "rc != 139" is not enough; assert ``size > 0``.
      3. MSYS path trap (bash-only, N/A from Python) -- handing the Windows
         ``kicad-cli.exe`` an MSYS ``/tmp/...`` path makes it silently write a
         0-byte file with rc=0. Always pass native paths; ``tmp_path`` is native.

    Skips (does not fail) if kicad-cli is unavailable.
    """
    sch_path = Path(sch_path)
    try:
        cli = kicad_cli or _find_kicad_cli()
    except Exception:
        pytest.skip("kicad-cli (KiCad 10) not available")

    copy = sch_path.with_name(sch_path.stem + "_savecopy.kicad_sch")
    shutil.copyfile(sch_path, copy)

    up = subprocess.run(
        [cli, "sch", "upgrade", "--force", str(copy)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert up.returncode == 0, (
        f"kicad-cli sch upgrade returned {up.returncode} on {copy.name} "
        f"(139 = segfault -> KiCad save crash). stderr: {up.stderr.strip()}"
    )
    size = copy.stat().st_size
    assert size > 0, (
        f"{copy.name} is 0 bytes after upgrade (crash truncated it, or an MSYS "
        f"path trap); rc was {up.returncode}"
    )
    # -o keeps the .rpt beside the copy (in tmp) instead of polluting cwd.
    erc = subprocess.run(
        [cli, "sch", "erc", "-o", str(copy.with_suffix(".rpt")), str(copy)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    # ERC returns 0 (clean) or 5 (violations exist) on a *loadable* file; a
    # crash/parse failure is a different, nonzero code. Either 0/5 proves reload.
    assert erc.returncode in (0, 5), (
        f"upgraded {copy.name} failed to reload in kicad-cli sch erc "
        f"(rc={erc.returncode}). stderr: {erc.stderr.strip()}"
    )


R_FP = "Resistor_SMD:R_0603_1608Metric"


@circuit(name="MuLM358")
def _lm358_design():
    u1 = Component(symbol="Amplifier_Operational:LM358", ref="U1")
    r1 = Component(symbol="Device:R", ref="R1", value="1k", footprint=R_FP)
    n1, gnd, vcc = Net("N1"), Net("GND"), Net("VCC")
    u1[1] += n1
    u1[2] += n1
    u1[3] += gnd
    u1[4] += gnd
    u1[8] += vcc
    r1[1] += n1
    r1[2] += gnd


@circuit(name="MuADA4807")
def _ada4807_dual_design():
    # ADA4807-2ACP: dual op-amp, 10-pin. Wire unit A as a follower, unit B idle.
    u1 = Component(symbol="Amplifier_Operational:ADA4807-2ACP", ref="U1")
    r1 = Component(symbol="Device:R", ref="R1", value="1k", footprint=R_FP)
    n1, gnd, vpos = Net("N1"), Net("GND"), Net("VPOS")
    # OUTA=1 -INA=2 +INA=3 V-=4 ... V+ pin varies; wire the amp + power rails.
    u1[1] += n1
    u1[2] += n1
    u1[3] += gnd
    r1[1] += n1
    r1[2] += gnd


def _generate(circ, name: str, tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        circ.generate_kicad_project(
            project_name=name,
            generate_pcb=False,
            erc_gate=False,
            selective_wires=False,
        )
    finally:
        os.chdir(cwd)
    return next(tmp_path.rglob(f"{name}.kicad_sch"))


def test_lm358_multi_unit_saves_without_crash(tmp_path):
    sch = _generate(_lm358_design(), "MuLM358", tmp_path / "lm358")
    assert_kicad_save_ok(sch)


def test_ada4807_dual_saves_without_crash(tmp_path):
    sch = _generate(_ada4807_dual_design(), "MuADA4807", tmp_path / "ada4807")
    assert_kicad_save_ok(sch)
