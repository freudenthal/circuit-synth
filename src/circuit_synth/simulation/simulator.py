"""
Main CircuitSimulator class for circuit-synth SPICE integration.

This module provides the primary interface for running SPICE simulations
on circuit-synth designs.
"""

import logging
import os
import platform
from typing import Dict, List, Optional, Tuple, Union

# Configure logging
logger = logging.getLogger(__name__)

try:
    import PySpice
    from PySpice.Spice.Netlist import Circuit as SpiceCircuit
    from PySpice.Spice.NgSpice.Shared import NgSpiceShared
    from PySpice.Unit import *

    PYSPICE_AVAILABLE = True

    # Auto-configure ngspice library path for macOS
    if platform.system() == "Darwin":  # macOS
        possible_paths = [
            "/opt/homebrew/lib/libngspice.dylib",  # Apple Silicon
            "/usr/local/lib/libngspice.dylib",  # Intel Mac
        ]
        for path in possible_paths:
            if os.path.exists(path):
                NgSpiceShared.LIBRARY_PATH = path
                logger.debug(f"Set ngspice library path: {path}")
                break

    # Auto-configure ngspice library path on Windows using KiCad's bundled DLL.
    # KiCad ships ngspice.dll (and its codemodels) under
    # <ProgramFiles>\KiCad\<version>\bin\ngspice.dll, so no separate ngspice
    # install is needed. Verified against KiCad 10.0 (ngspice 46).
    elif platform.system() == "Windows":
        import re as _re
        from pathlib import Path as _Path

        _roots = [
            _Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "KiCad",
            _Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
            / "KiCad",
        ]
        _versioned = []
        for _root in _roots:
            if _root.is_dir():
                for _child in _root.iterdir():
                    if _child.is_dir() and _re.fullmatch(r"\d+(?:\.\d+)*", _child.name):
                        _versioned.append(
                            (tuple(int(p) for p in _child.name.split(".")), _child)
                        )
        for _, _ver_dir in sorted(_versioned, reverse=True):
            _dll = _ver_dir / "bin" / "ngspice.dll"
            if _dll.exists():
                # Let ngspice.dll's own dependencies resolve from KiCad's bin dir.
                os.add_dll_directory(str(_dll.parent))
                NgSpiceShared.LIBRARY_PATH = str(_dll)
                # Point ngspice at its codemodels (analog.cm, etc.) if present so
                # AC/transient/behavioral sources work later; harmless if absent.
                _cm_dir = _ver_dir / "lib" / "ngspice"
                if _cm_dir.is_dir():
                    os.environ.setdefault("SPICE_LIB_DIR", str(_cm_dir))
                logger.debug(f"Set ngspice library path: {_dll}")
                break

except ImportError as e:
    PYSPICE_AVAILABLE = False
    logger.warning(f"PySpice not available: {e}")


class SimulationResult:
    """Container for SPICE simulation results with analysis capabilities."""

    def __init__(self, analysis_result, analysis_type: str):
        self.analysis = analysis_result
        self.analysis_type = analysis_type
        self._voltages = {}
        self._currents = {}

        # Extract voltages and currents from analysis
        if hasattr(analysis_result, "nodes"):
            for node in analysis_result.nodes:
                if hasattr(analysis_result, node):
                    self._voltages[node] = analysis_result[node]

    def get_voltage(self, node: str) -> Union[float, List[float]]:
        """Get voltage at a specific node."""
        if node in self._voltages:
            voltage = self._voltages[node]
            # Handle scalar or array results
            if hasattr(voltage, "__len__") and len(voltage) == 1:
                return float(voltage[0])
            elif hasattr(voltage, "__len__"):
                return [float(v) for v in voltage]
            else:
                return float(voltage)
        else:
            # Try direct access
            try:
                voltage = self.analysis[node]
                if hasattr(voltage, "__len__") and len(voltage) == 1:
                    return float(voltage[0])
                elif hasattr(voltage, "__len__"):
                    return [float(v) for v in voltage]
                else:
                    return float(voltage)
            except:
                raise KeyError(f"Node '{node}' not found in simulation results")

    def get_current(self, component: str) -> Union[float, List[float]]:
        """Get current through a specific component."""
        # PySpice current notation: I(Vcomponent) for voltage sources
        current_name = f"I({component})"
        try:
            current = self.analysis[current_name]
            if hasattr(current, "__len__") and len(current) == 1:
                return float(current[0])
            elif hasattr(current, "__len__"):
                return [float(i) for i in current]
            else:
                return float(current)
        except:
            raise KeyError(f"Current for component '{component}' not found")

    def _frequency_array(self):
        """The AC sweep frequency axis as a real float ndarray.

        Raises if this result is not from an AC analysis (no frequency axis).
        """
        import numpy as np

        freq = getattr(self.analysis, "frequency", None)
        if freq is None:
            raise ValueError(
                "no frequency axis available (bode/cutoff need an AC analysis result)"
            )
        return np.real(np.asarray(freq)).astype(float)

    def _complex_node(self, node: str):
        """The complex node response (H(f)) as a complex ndarray."""
        import numpy as np

        try:
            data = self.analysis[node]
        except Exception:
            raise KeyError(f"Node '{node}' not found in AC analysis results")
        return np.asarray(data, dtype=complex)

    def bode(self, node: str, input_magnitude: float = 1.0):
        """Bode data for a node: ``(frequencies, magnitude_db, phase_deg)``.

        ``magnitude_db = 20*log10(|H|)`` where ``H = V(node) / input_magnitude``.
        With the default AC source magnitude of 1 V the node voltage *is* the
        transfer function, so ``input_magnitude`` can be left at 1.
        """
        import numpy as np

        freq = self._frequency_array()
        H = self._complex_node(node) / input_magnitude
        magnitude_db = 20 * np.log10(np.abs(H))
        phase_deg = np.angle(H, deg=True)
        return freq, magnitude_db, phase_deg

    def passband_gain_db(self, node: str, input_magnitude: float = 1.0) -> float:
        """Peak magnitude (dB) of the response -- the passband gain."""
        import numpy as np

        _, magnitude_db, _ = self.bode(node, input_magnitude)
        return float(np.max(magnitude_db))

    def cutoff_frequency(
        self, node: str, ref_db: float = -3.0, input_magnitude: float = 1.0
    ) -> Optional[float]:
        """Frequency where the response is ``ref_db`` below its passband peak.

        For a low-pass response this is the -3 dB corner. Returns the first
        frequency (scanning low->high) where the magnitude crosses
        ``passband_db + ref_db``, linearly interpolated in log-frequency between
        the two straddling samples. Returns ``None`` if the curve never crosses.
        """
        import numpy as np

        freq, magnitude_db, _ = self.bode(node, input_magnitude)
        target = float(np.max(magnitude_db)) + ref_db
        for i in range(1, len(freq)):
            a, b = magnitude_db[i - 1], magnitude_db[i]
            if a == b:
                continue
            # Straddle: target lies between consecutive samples.
            if (a - target) * (b - target) <= 0:
                fa, fb = np.log10(freq[i - 1]), np.log10(freq[i])
                t = (target - a) / (b - a)
                return float(10 ** (fa + t * (fb - fa)))
        return None

    def list_nodes(self) -> List[str]:
        """List all available voltage nodes."""
        nodes = []
        if hasattr(self.analysis, "nodes"):
            nodes.extend(self.analysis.nodes)
        # Also check for direct access
        for attr in dir(self.analysis):
            if not attr.startswith("_") and attr not in ["nodes", "branches"]:
                try:
                    val = getattr(self.analysis, attr)
                    if hasattr(val, "__len__") or isinstance(val, (int, float)):
                        nodes.append(attr)
                except:
                    pass
        return list(set(nodes))

    def plot(self, *nodes, title: Optional[str] = None):
        """Plot voltage results (requires matplotlib)."""
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            logger.error("matplotlib required for plotting")
            return

        plt.figure(figsize=(10, 6))

        for node in nodes:
            try:
                voltage = self.get_voltage(node)
                if isinstance(voltage, list):
                    plt.plot(voltage, label=f"V({node})")
                else:
                    plt.axhline(y=voltage, label=f"V({node}) = {voltage:.3f}V")
            except KeyError as e:
                logger.warning(f"Could not plot {node}: {e}")

        plt.xlabel("Time/Frequency/Sweep")
        plt.ylabel("Voltage (V)")
        plt.title(title or f"{self.analysis_type.upper()} Analysis Results")
        plt.legend()
        plt.grid(True)
        plt.show()


class CircuitSimulator:
    """Main interface for SPICE simulation of circuit-synth designs."""

    def __init__(self, circuit_synth_circuit):
        if not PYSPICE_AVAILABLE:
            raise ImportError(
                "PySpice not available. Install with: pip install PySpice\n"
                "Also ensure ngspice is installed on your system."
            )

        self.circuit_synth_circuit = circuit_synth_circuit
        self.spice_circuit = None
        # {ref: ResolvedModel} recording which model tier each active device got
        # (datasheet_fit / generic / vendor_lib). Populated during conversion.
        self.model_provenance = {}
        self._convert_to_spice()

    def _convert_to_spice(self):
        """Convert circuit-synth circuit to PySpice format."""
        from .converter import SpiceConverter

        converter = SpiceConverter(self.circuit_synth_circuit)
        self.spice_circuit = converter.convert()
        self.model_provenance = converter.model_provenance

    def _make_simulator(self, temperature: float, options: Optional[Dict] = None):
        """Build a PySpice simulator with temperature and optional ngspice options.

        ``options`` maps ngspice ``.options`` names to values (e.g.
        ``{"reltol": 1e-3, "abstol": 1e-9, "gmin": 1e-12}``) for convergence /
        accuracy tuning; omit for ngspice defaults.
        """
        if not self.spice_circuit:
            raise RuntimeError("SPICE circuit not initialized")
        simulator = self.spice_circuit.simulator(
            temperature=temperature, nominal_temperature=temperature
        )
        if options:
            simulator.options(**options)
        return simulator

    def operating_point(
        self, temperature: float = 25, options: Optional[Dict] = None
    ) -> SimulationResult:
        """Run DC operating point analysis."""
        simulator = self._make_simulator(temperature, options)
        analysis = simulator.operating_point()

        return SimulationResult(analysis, "dc_op")

    def dc_analysis(
        self,
        source: str,
        start: float,
        stop: float,
        step: float,
        temperature: float = 25,
        options: Optional[Dict] = None,
    ) -> SimulationResult:
        """Run DC sweep analysis."""
        simulator = self._make_simulator(temperature, options)
        analysis = simulator.dc(**{source: slice(start, stop, step)})

        return SimulationResult(analysis, "dc_sweep")

    def ac_analysis(
        self,
        start_freq: float,
        stop_freq: float,
        points: int = 100,
        temperature: float = 25,
        options: Optional[Dict] = None,
    ) -> SimulationResult:
        """Run AC analysis."""
        simulator = self._make_simulator(temperature, options)
        analysis = simulator.ac(
            start_frequency=start_freq @ u_Hz,
            stop_frequency=stop_freq @ u_Hz,
            number_of_points=points,
            variation="dec",
        )

        return SimulationResult(analysis, "ac")

    def transient_analysis(
        self,
        step_time: float,
        end_time: float,
        temperature: float = 25,
        options: Optional[Dict] = None,
    ) -> SimulationResult:
        """Run transient analysis."""
        simulator = self._make_simulator(temperature, options)
        analysis = simulator.transient(
            step_time=step_time @ u_s, end_time=end_time @ u_s
        )

        return SimulationResult(analysis, "transient")

    def list_components(self) -> List[str]:
        """List all components in the SPICE circuit."""
        if not self.spice_circuit:
            return []

        components = []
        for element in self.spice_circuit.elements:
            components.append(str(element.name))
        return components

    def list_nodes(self) -> List[str]:
        """List all nodes in the SPICE circuit."""
        if not self.spice_circuit:
            return []

        nodes = []
        for node in self.spice_circuit.node_names:
            nodes.append(str(node))
        return nodes

    def get_netlist(self) -> str:
        """Get the SPICE netlist as string."""
        if not self.spice_circuit:
            return ""

        return str(self.spice_circuit)
