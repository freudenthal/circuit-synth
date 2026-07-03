"""
SpiceConverter: Converts circuit-synth designs to PySpice format.

This module handles the translation from circuit-synth components and nets
to SPICE netlists that can be simulated with PySpice/ngspice.
"""

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    from PySpice.Spice.Netlist import Circuit as SpiceCircuit
    from PySpice.Unit import *

    PYSPICE_AVAILABLE = True
except ImportError:
    PYSPICE_AVAILABLE = False


class SimulationValidationError(ValueError):
    """Raised when a circuit cannot be safely simulated.

    Carries the full list of problems (``.problems``) so the caller sees every
    issue at once, rather than discovering them one ngspice crash at a time. This
    replaces the old behaviour where unknown components were silently skipped,
    yielding a wrong-but-"successful" simulation.
    """

    def __init__(self, problems):
        self.problems = list(problems)
        body = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(f"circuit is not valid for simulation:\n{body}")


class SpiceConverter:
    """Converts circuit-synth circuits to PySpice format."""

    def __init__(self, circuit_synth_circuit):
        self.circuit = circuit_synth_circuit
        self.spice_circuit = None
        self.voltage_sources = []
        self.node_map = {}
        # Nets driven by an explicit source component (Device:V, Simulation_SPICE:V*,
        # ...). _add_power_sources skips these so the net-name heuristic never adds a
        # second, conflicting supply on a net an explicit source already drives.
        self.driven_nets = set()

    def convert(self, strict: bool = True) -> "SpiceCircuit":
        """Convert circuit-synth circuit to PySpice circuit.

        Args:
            strict: When True (default), validate the circuit first and raise
                ``SimulationValidationError`` if anything would produce a
                wrong-but-"successful" simulation (unknown component, floating
                node, no source, under-connected op-amp). When False, fall back to
                the lenient path that warns and skips unknown components -- useful
                for exploratory conversion of partial circuits.
        """
        if not PYSPICE_AVAILABLE:
            raise ImportError("PySpice not available")

        if strict:
            self.validate()

        # Create PySpice circuit
        circuit_name = getattr(self.circuit, "name", "Circuit")
        self.spice_circuit = SpiceCircuit(circuit_name)

        # Map circuit-synth nets to SPICE nodes
        self._map_nodes()

        # Add components to SPICE circuit
        self._add_components()

        # Add power sources (voltage/current sources)
        self._add_power_sources()

        return self.spice_circuit

    def _map_nodes(self):
        """Create mapping from circuit-synth nets to SPICE node names."""
        self.node_map = {}

        # Handle both dict and list formats for nets
        if hasattr(self.circuit.nets, "values"):
            # Dict format: {name: net_object}
            nets_to_process = self.circuit.nets.values()
        elif hasattr(self.circuit.nets, "__iter__"):
            # List format: [net_object, ...]
            nets_to_process = self.circuit.nets
        else:
            logger.error("Unknown nets format")
            return

        # Map GND net to SPICE ground
        for net in nets_to_process:
            net_name = getattr(net, "name", str(net))
            if net_name.upper() in ["GND", "GROUND", "VSS"]:
                self.node_map[net_name] = self.spice_circuit.gnd
            else:
                self.node_map[net_name] = net_name

    def _add_components(self):
        """Add circuit-synth components to SPICE circuit."""
        # circuit.components is a dict {ref: component}; iterating it directly
        # yields refs (strings), so pull the component objects out explicitly.
        components = self.circuit.components
        if hasattr(components, "values"):
            components = components.values()
        for component in components:
            self._add_component(component)

    @staticmethod
    def _classify(symbol: str) -> Optional[str]:
        """Map a KiCad symbol to a SPICE primitive kind, or None if unrecognized.

        Single source of truth shared by ``_add_component`` (dispatch) and
        ``validate`` (so validation and conversion never disagree about what is
        simulatable). Source checks come before the op-amp heuristic so a source
        symbol is never misclassified. Explicit SPICE sources use KiCad's real
        Simulation_SPICE library (VDC/VSIN/IDC/...); "Device:V"/"Device:I" are
        exact aliases (not real KiCad symbols but referenced by docs/testbench) --
        matched exactly so they do not also swallow "Device:Varistor".
        """
        if not symbol:
            return None
        if "Device:R" in symbol:
            return "resistor"
        if "Device:C" in symbol:
            return "capacitor"
        if "Device:L" in symbol:
            return "inductor"
        if "Device:D" in symbol or "Diode:" in symbol:
            return "diode"
        if (
            symbol.startswith("Simulation_SPICE:V")
            or "Reference_Voltage:" in symbol
            or symbol == "Device:V"
        ):
            return "voltage_source"
        if (
            symbol.startswith("Simulation_SPICE:I")
            or "Reference_Current:" in symbol
            or symbol == "Device:I"
        ):
            return "current_source"
        if any(x in symbol.lower() for x in ["op", "amp", "lm", "tl"]):
            return "opamp"
        if "Transistor_BJT:" in symbol or "Device:Q" in symbol:
            return "bjt"
        if "Transistor_FET:" in symbol or "Device:M" in symbol:
            return "mosfet"
        return None

    def _add_component(self, component):
        """Add a single component to the SPICE circuit."""
        symbol = getattr(component, "symbol", "")
        ref = getattr(component, "ref", "X")
        value = getattr(component, "value", None)

        handlers = {
            "resistor": self._add_resistor,
            "capacitor": self._add_capacitor,
            "inductor": self._add_inductor,
            "diode": self._add_diode,
            "voltage_source": self._add_voltage_source,
            "current_source": self._add_current_source,
            "opamp": self._add_opamp,
            "bjt": self._add_bjt_transistor,
            "mosfet": self._add_mosfet,
        }
        handler = handlers.get(self._classify(symbol))
        if handler is None:
            logger.warning(f"Unknown component type: {symbol} - skipping")
            return
        handler(component, ref, value)

    def _add_resistor(self, component, ref: str, value: str):
        """Add resistor to SPICE circuit."""
        # Get connected nodes
        nodes = self._get_component_nodes(component)
        if len(nodes) < 2:
            logger.warning(f"Resistor {ref} needs 2 connections, got {len(nodes)}")
            return

        # Convert value to SPICE format
        spice_value = self._convert_value_to_spice(value, "R")

        # Add to SPICE circuit
        self.spice_circuit.R(ref, nodes[0], nodes[1], spice_value)
        logger.debug(f"Added resistor {ref}: {nodes[0]} -> {nodes[1]} = {spice_value}")

    def _add_capacitor(self, component, ref: str, value: str):
        """Add capacitor to SPICE circuit."""
        nodes = self._get_component_nodes(component)
        if len(nodes) < 2:
            logger.warning(f"Capacitor {ref} needs 2 connections, got {len(nodes)}")
            return

        spice_value = self._convert_value_to_spice(value, "C")
        self.spice_circuit.C(ref, nodes[0], nodes[1], spice_value)
        logger.debug(f"Added capacitor {ref}: {nodes[0]} -> {nodes[1]} = {spice_value}")

    def _add_inductor(self, component, ref: str, value: str):
        """Add inductor to SPICE circuit."""
        nodes = self._get_component_nodes(component)
        if len(nodes) < 2:
            logger.warning(f"Inductor {ref} needs 2 connections, got {len(nodes)}")
            return

        spice_value = self._convert_value_to_spice(value, "L")
        self.spice_circuit.L(ref, nodes[0], nodes[1], spice_value)
        logger.debug(f"Added inductor {ref}: {nodes[0]} -> {nodes[1]} = {spice_value}")

    def _add_diode(self, component, ref: str, value: str):
        """Add diode to SPICE circuit."""
        nodes = self._get_component_nodes(component)
        if len(nodes) < 2:
            logger.warning(f"Diode {ref} needs 2 connections, got {len(nodes)}")
            return

        # Use default diode model
        model_name = value or "DefaultDiode"
        self.spice_circuit.D(ref, nodes[0], nodes[1], model=model_name)
        logger.debug(f"Added diode {ref}: {nodes[0]} -> {nodes[1]} model={model_name}")

    def _add_opamp(self, component, ref: str, value: str):
        """Add op-amp to SPICE circuit (simplified model)."""
        nodes = self._get_component_nodes(component)
        if len(nodes) < 3:
            logger.warning(
                f"Op-amp {ref} needs at least 3 connections, got {len(nodes)}"
            )
            return

        # Simplified op-amp as voltage-controlled voltage source
        # Assumes nodes[0] = out, nodes[1] = in+, nodes[2] = in-
        gain = 100000  # High gain approximation
        self.spice_circuit.VCVS(
            ref, nodes[0], self.spice_circuit.gnd, nodes[1], nodes[2], gain
        )
        logger.debug(f"Added op-amp {ref} with gain {gain}")

    def _add_bjt_transistor(self, component, ref: str, value: str):
        """Add BJT transistor to SPICE circuit."""
        nodes = self._get_component_nodes(component)
        if len(nodes) < 3:
            logger.warning(f"BJT {ref} needs 3 connections (C,B,E), got {len(nodes)}")
            return

        # Determine if NPN or PNP from symbol or value
        model_name = value or "DefaultNPN"
        if "pnp" in str(component.symbol).lower() or "pnp" in str(value).lower():
            model_name = value or "DefaultPNP"

        # Add transistor (collector, base, emitter)
        self.spice_circuit.Q(ref, nodes[0], nodes[1], nodes[2], model=model_name)
        logger.debug(
            f"Added BJT {ref}: C={nodes[0]}, B={nodes[1]}, E={nodes[2]}, model={model_name}"
        )

    def _add_mosfet(self, component, ref: str, value: str):
        """Add MOSFET to SPICE circuit."""
        nodes = self._get_component_nodes(component)
        if len(nodes) < 3:
            logger.warning(
                f"MOSFET {ref} needs at least 3 connections (D,G,S), got {len(nodes)}"
            )
            return

        # Determine NMOS or PMOS from symbol or value
        model_name = value or "DefaultNMOS"
        if "pmos" in str(component.symbol).lower() or "pmos" in str(value).lower():
            model_name = value or "DefaultPMOS"

        # Add MOSFET (drain, gate, source, bulk - bulk defaults to source if not provided)
        if len(nodes) >= 4:
            self.spice_circuit.M(
                ref, nodes[0], nodes[1], nodes[2], nodes[3], model=model_name
            )
            logger.debug(
                f"Added MOSFET {ref}: D={nodes[0]}, G={nodes[1]}, S={nodes[2]}, B={nodes[3]}"
            )
        else:
            # Bulk connected to source
            self.spice_circuit.M(
                ref, nodes[0], nodes[1], nodes[2], nodes[2], model=model_name
            )
            logger.debug(
                f"Added MOSFET {ref}: D={nodes[0]}, G={nodes[1]}, S={nodes[2]} (bulk=source)"
            )

    def _add_voltage_source(self, component, ref: str, value: str):
        """Add voltage source to SPICE circuit."""
        nodes = self._get_component_nodes(component)
        if len(nodes) < 2:
            logger.warning(
                f"Voltage source {ref} needs 2 connections, got {len(nodes)}"
            )
            return

        # Parse voltage value
        voltage = self._convert_value_to_spice(value or "5V", "V")

        # Add to list of voltage sources for tracking
        self.voltage_sources.append(ref)
        # Mark this source's nets so the net-name heuristic won't double-drive them.
        self.driven_nets.update(self._component_net_names(component))

        # Add voltage source. nodes[] is in pin-number order, so nodes[0] is pin 1
        # (KiCad Sim.Pins "1=+") and nodes[1] is pin 2 ("2=-"): V(name, +, -, value).
        self.spice_circuit.V(ref, nodes[0], nodes[1], voltage)
        logger.debug(
            f"Added voltage source {ref}: {nodes[0]}(+) -> {nodes[1]}(-) = {voltage}V"
        )

    def _add_current_source(self, component, ref: str, value: str):
        """Add current source to SPICE circuit."""
        nodes = self._get_component_nodes(component)
        if len(nodes) < 2:
            logger.warning(
                f"Current source {ref} needs 2 connections, got {len(nodes)}"
            )
            return

        # Parse current value
        current = self._convert_value_to_spice(value or "1mA", "I")
        # Mark this source's nets so the net-name heuristic won't double-drive them.
        self.driven_nets.update(self._component_net_names(component))

        # Add current source. nodes[] is pin-number ordered (pin 1 = +, pin 2 = -);
        # ngspice current flows from + node, through the source, to the - node.
        self.spice_circuit.I(ref, nodes[0], nodes[1], current)
        logger.debug(
            f"Added current source {ref}: {nodes[0]} -> {nodes[1]} = {current}A"
        )

    @staticmethod
    def _pin_sort_key(num):
        """Sort key that orders pin numbers numerically ('2' < '10') when possible."""
        s = str(num)
        return (0, int(s)) if s.isdigit() else (1, s)

    def _component_net_names(self, component) -> set:
        """Original (unmapped) net names connected to a component's pins.

        Best effort: only available for live Component objects that carry a
        ``_pins`` map. Returns an empty set for dict/JSON-shaped components.
        """
        names = set()
        pin_map = getattr(component, "_pins", None)
        if isinstance(pin_map, dict):
            for pin in pin_map.values():
                net = getattr(pin, "net", None)
                name = getattr(net, "name", None)
                if name:
                    names.add(name)
        return names

    def _get_component_nodes(self, component) -> List[str]:
        """Get the SPICE nodes connected to a component, in pin-number order.

        Pin order is significant: a voltage/current source's pin 1 is + and pin 2
        is - (KiCad ``Sim.Pins "1=+ 2=-"``), and a transistor's pins are C/B/E or
        D/G/S. A live Component exposes a ``{pin_num: Pin}`` map, so we read the
        node for each pin in ascending pin-number order. Unlike the legacy net
        scan this does *not* dedupe or alphabetically sort, so polarity and
        terminal order are preserved.

        Falls back to the legacy net scan for dict/JSON-shaped circuits whose
        components have no live ``_pins`` map.
        """
        pin_map = getattr(component, "_pins", None)
        if isinstance(pin_map, dict) and pin_map:
            ordered = []
            for _num, pin in sorted(
                pin_map.items(), key=lambda kv: self._pin_sort_key(kv[0])
            ):
                net = getattr(pin, "net", None)
                net_name = getattr(net, "name", None)
                if net_name is None:
                    continue  # unconnected pin
                ordered.append(self.node_map.get(net_name, net_name))
            if ordered:
                return ordered
            # else: fall through to the net scan (e.g. pins present but netless)

        return self._get_component_nodes_by_net_scan(component)

    def _get_component_nodes_by_net_scan(self, component) -> List[str]:
        """Legacy fallback: recover a component's nodes by scanning nets.

        Used for dict/JSON-shaped circuits without live Pin objects. Nodes are
        alphabetically sorted (pin order is not recoverable here), which is fine
        for symmetric R/C/L but does not guarantee polarity for sources.
        """
        nodes = []

        # Get component connections from the circuit
        # circuit.nets is a dict, iterate over values
        if hasattr(self.circuit.nets, "values"):
            nets_to_check = self.circuit.nets.values()
        else:
            nets_to_check = self.circuit.nets

        for net in nets_to_check:
            net_name = getattr(net, "name", str(net))

            # Check if this net has pins connected to our component
            if hasattr(net, "pins"):
                for pin in net.pins:
                    # Each pin has a reference back to its component
                    # The pin string format is like "Pin(~ of R1, net=VIN)"
                    pin_str = str(pin)
                    component_ref = getattr(component, "ref", "")

                    # Check if this pin belongs to our component
                    if f" of {component_ref}," in pin_str:
                        # Map to SPICE node name
                        spice_node = self.node_map.get(net_name, net_name)
                        if spice_node not in nodes:
                            nodes.append(spice_node)
                        break

        # Sort nodes to ensure consistent pin ordering (important for SPICE)
        # Convert all to strings first to avoid type comparison issues
        nodes.sort(key=str)

        # If we didn't find connections, log for debugging
        if not nodes:
            logger.warning(
                f"No connections found for component {getattr(component, 'ref', 'unknown')}"
            )

        return nodes

    def _convert_value_to_spice(self, value: str, component_type: str) -> float:
        """Convert circuit-synth component value to SPICE format."""
        if not value:
            # Default values
            defaults = {"R": 1000, "C": 1e-6, "L": 1e-3}
            return defaults.get(component_type, 1.0)

        # Parse value string (e.g., "10k", "100nF", "1mH")
        value = str(value).strip().replace(" ", "")

        # Extract numeric part and suffix
        match = re.match(r"^([0-9.]+)([a-zA-Z]*)$", value)
        if not match:
            logger.warning(f"Could not parse value '{value}', using 1.0")
            return 1.0

        numeric_part = float(match.group(1))
        suffix = match.group(2).lower()

        # Convert suffixes to multipliers
        multipliers = {
            # Resistance
            "r": 1,
            "ohm": 1,
            "ohms": 1,
            "k": 1e3,
            "kohm": 1e3,
            "kohms": 1e3,
            "m": 1e6,
            "meg": 1e6,
            "mohm": 1e6,
            "mohms": 1e6,
            # Capacitance
            "f": 1,
            "pf": 1e-12,
            "nf": 1e-9,
            "uf": 1e-6,
            "mf": 1e-3,
            "p": 1e-12,
            "n": 1e-9,
            "u": 1e-6,
            # Inductance
            "h": 1,
            "nh": 1e-9,
            "uh": 1e-6,
            "mh": 1e-3,
            # Voltage
            "v": 1,
            "mv": 1e-3,
            "kv": 1e3,
            # Current
            "a": 1,
            "ma": 1e-3,
            "ua": 1e-6,
            "na": 1e-9,
        }

        multiplier = multipliers.get(suffix, 1.0)
        return numeric_part * multiplier

    def _add_power_sources(self):
        """Add power sources needed for simulation."""
        # Check if we need to add power sources based on net names
        power_nets = []

        # Handle both dict and list formats for nets
        if hasattr(self.circuit.nets, "values"):
            nets_to_process = self.circuit.nets.values()
        elif hasattr(self.circuit.nets, "__iter__"):
            nets_to_process = self.circuit.nets
        else:
            return

        for net in nets_to_process:
            orig_name = getattr(net, "name", str(net))
            # An explicit source component already drives this net; don't add a
            # second heuristic supply on top of it (that would over-constrain the
            # node / fight the real source).
            if orig_name in self.driven_nets:
                continue
            voltage = self._heuristic_source_voltage(orig_name)
            if voltage is not None:
                power_nets.append((orig_name, voltage))

        # Add voltage sources
        for i, (net_name, voltage) in enumerate(power_nets):
            source_name = f"V_supply_{i+1}"
            spice_node = self.node_map.get(net_name, net_name)
            self.spice_circuit.V(
                source_name, spice_node, self.spice_circuit.gnd, voltage @ u_V
            )
            logger.debug(f"Added voltage source {source_name}: {voltage}V")

    def _extract_voltage_from_net_name(self, net_name: str) -> Optional[float]:
        """Extract voltage value from net name (e.g., '+5V' -> 5.0)."""
        # Look for voltage patterns
        patterns = [
            r"\+?([0-9.]+)V",  # +5V, 3.3V, etc.
            r"VCC_?([0-9.]+)",  # VCC_5, VCC5, etc.
            r"VDD_?([0-9.]+)",  # VDD_3, VDD3, etc.
        ]

        for pattern in patterns:
            match = re.search(pattern, net_name.upper())
            if match:
                try:
                    return float(match.group(1))
                except ValueError:
                    continue

        return None

    def _heuristic_source_voltage(self, net_name: str) -> Optional[float]:
        """Voltage the net-name heuristic would assign to this net, or None.

        Single source of truth shared by ``_add_power_sources`` (which actually
        injects the supply) and ``validate`` (which must know whether a
        single-connection net will nonetheless be driven). A bare ``VCC`` with no
        embedded number is *not* driven (no voltage to assign); ``VIN``/``VSUPPLY``
        default to 5 V.
        """
        upper = net_name.upper()
        if any(p in upper for p in ["VCC", "VDD", "V+", "+5V", "+3V3", "+12V"]):
            return self._extract_voltage_from_net_name(upper)
        if "VIN" in upper or "VSUPPLY" in upper:
            return self._extract_voltage_from_net_name(upper) or 5.0
        return None

    # ------------------------------------------------------------------ #
    # Validation                                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _attr(obj, name, default=None):
        """Read an attribute from either a live object or a dict-shaped one."""
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    def _iter_components(self):
        comps = getattr(self.circuit, "components", None)
        if comps is None:
            return []
        return list(comps.values()) if hasattr(comps, "values") else list(comps)

    def _iter_nets(self):
        nets = getattr(self.circuit, "nets", None)
        if nets is None:
            return []
        return list(nets.values()) if hasattr(nets, "values") else list(nets)

    @staticmethod
    def _is_ground_name(name: str) -> bool:
        return str(name).upper() in ("GND", "GROUND", "VSS", "0")

    @staticmethod
    def _net_pin_count(net) -> Optional[int]:
        """Number of pins on a net, or None if the net can't report it."""
        pins = getattr(net, "pins", None)
        if pins is None:
            return None
        try:
            return len(pins)
        except TypeError:
            return None

    def _connected_pin_count(self, component) -> Optional[int]:
        """Number of a component's pins attached to a net, or None if unknown."""
        pin_map = getattr(component, "_pins", None)
        if not isinstance(pin_map, dict):
            return None
        return sum(
            1 for pin in pin_map.values() if getattr(pin, "net", None) is not None
        )

    def _all_driven_net_names(self) -> set:
        """Nets that will carry a source: explicit source components + heuristic rails."""
        driven = set()
        for component in self._iter_components():
            if self._classify(self._attr(component, "symbol", "")) in (
                "voltage_source",
                "current_source",
            ):
                driven |= self._component_net_names(component)
        for net in self._iter_nets():
            name = self._attr(net, "name", None) or str(net)
            if self._heuristic_source_voltage(name) is not None:
                driven.add(name)
        return driven

    def validate(self) -> None:
        """Check the circuit is safe to simulate; raise with every problem found.

        Catches the failure modes that otherwise produce a wrong-but-"successful"
        simulation or an opaque ngspice error:

        * a component whose symbol has no SPICE mapping (was silently skipped);
        * a net with a single connection that no source drives (floating node ->
          singular matrix);
        * no source at all (nothing to excite the circuit);
        * an op-amp with fewer than three connected pins (needs in+, in-, out).

        Checks that need data a dict/JSON-shaped circuit can't provide (live pin
        maps, net pin counts) are skipped for those inputs rather than raising
        false positives.
        """
        problems = []

        # 1. Every component must map to a known SPICE primitive.
        for component in self._iter_components():
            symbol = self._attr(component, "symbol", "")
            ref = self._attr(component, "ref", None) or "?"
            if self._classify(symbol) is None:
                problems.append(
                    f"{ref}: unrecognized symbol '{symbol}' has no SPICE mapping "
                    f"(would be silently skipped)"
                )

        driven = self._all_driven_net_names()

        # 2. There must be some excitation.
        if not driven:
            problems.append(
                "no voltage or current source found (declare a "
                "Simulation_SPICE:VDC/VAC, or use a rail-named net); simulation "
                "would have no excitation"
            )

        # 3. No floating nodes: every net needs >=2 connections, ground, or a source.
        for net in self._iter_nets():
            name = self._attr(net, "name", None) or str(net)
            if self._is_ground_name(name) or name in driven:
                continue
            count = self._net_pin_count(net)
            if count is None:
                continue  # can't tell (dict/JSON net) -> don't block
            if count < 2:
                problems.append(
                    f"net '{name}' has {count} connection(s); needs >=2 or a source "
                    f"(floating node)"
                )

        # 4. Op-amps need at least their three signal pins connected.
        for component in self._iter_components():
            if self._classify(self._attr(component, "symbol", "")) != "opamp":
                continue
            count = self._connected_pin_count(component)
            if count is None:
                continue
            if count < 3:
                ref = self._attr(component, "ref", None) or "?"
                problems.append(
                    f"{ref}: op-amp needs >=3 connected pins (in+, in-, out), "
                    f"found {count}"
                )

        if problems:
            raise SimulationValidationError(problems)
