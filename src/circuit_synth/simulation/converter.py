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

    # Built-in generic SPICE model cards, keyed by model name. Each entry is
    # (ngspice device type, params dict). These back the ``Default*`` names the
    # device handlers assign, so any diode/BJT/MOSFET is simulatable out of the box
    # without a vendor model; a device's ``value=`` may also name one of these
    # built-ins directly. Only models a circuit actually uses are emitted (see
    # ``_emit_models``). Params are deliberately generic (textbook silicon values).
    GENERIC_MODELS = {
        "DefaultDiode": ("D", {"IS": 1e-14, "RS": 0.1, "N": 1.0, "CJO": 2e-12}),
        "DefaultNPN": ("NPN", {"BF": 100, "IS": 1e-14, "VAF": 100}),
        "DefaultPNP": ("PNP", {"BF": 100, "IS": 1e-14, "VAF": 100}),
        "DefaultNMOS": ("NMOS", {"VTO": 1.0, "KP": 2e-5, "LAMBDA": 0.02}),
        "DefaultPMOS": ("PMOS", {"VTO": -1.0, "KP": 2e-5, "LAMBDA": 0.02}),
    }

    # A bare device-type keyword in ``value`` selects the matching generic model,
    # so ``value="pnp"`` is treated as a type hint rather than a literal model name.
    _TYPE_KEYWORD_MODELS = {
        "npn": "DefaultNPN",
        "pnp": "DefaultPNP",
        "nmos": "DefaultNMOS",
        "pmos": "DefaultPMOS",
        "diode": "DefaultDiode",
    }

    def __init__(self, circuit_synth_circuit):
        self.circuit = circuit_synth_circuit
        self.spice_circuit = None
        self.voltage_sources = []
        self.node_map = {}
        # Nets driven by an explicit source component (Device:V, Simulation_SPICE:V*,
        # ...). _add_power_sources skips these so the net-name heuristic never adds a
        # second, conflicting supply on a net an explicit source already drives.
        self.driven_nets = set()
        # Model names referenced by diode/BJT/MOSFET devices; a matching .model card
        # is emitted for each that resolves to a built-in generic (see _emit_models).
        self.used_models = set()
        # Per-device model cards synthesized from Sim.Params overrides, keyed by a
        # derived name ({base}_{ref}) so two parts overriding the same base model
        # do not collide. Each value is (device_type, params). Emitted alongside
        # the built-in generics in _emit_models.
        self.derived_models = {}

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

        # Emit .model cards for the semiconductor models the components referenced.
        self._emit_models()

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

    # KiCad ``Sim.Device`` device tokens -> our SPICE primitive kinds. Lets a
    # simulation-only stand-in ride on any symbol (the schematic keeps its real
    # symbol/footprint; only the simulation model is redirected). SUBCKT is left
    # unmapped here (external-model attach is Stage 9.3).
    _SIM_DEVICE_KINDS = {
        "R": "resistor",
        "C": "capacitor",
        "L": "inductor",
        "D": "diode",
        "NPN": "bjt",
        "PNP": "bjt",
        "NMOS": "mosfet",
        "PMOS": "mosfet",
        "V": "voltage_source",
        "I": "current_source",
    }

    @staticmethod
    def _sim_props(component) -> dict:
        """KiCad ``Sim.*`` fields as ``{lowercased suffix: value}`` (empty if none).

        Reads them off ``_extra_fields`` like the waveform params. Accepts both the
        native dotted spelling (``Sim.Enable``) and an underscore fallback
        (``Sim_Enable``) in case a flow sanitizes the dot away.
        """
        extra = getattr(component, "_extra_fields", None)
        if not isinstance(extra, dict):
            return {}
        out = {}
        for k, v in extra.items():
            low = str(k).lower()
            if low.startswith("sim.") or low.startswith("sim_"):
                out[low[4:]] = v
        return out

    def _sim_excluded(self, component) -> bool:
        """True if ``Sim.Enable`` marks the component out of simulation.

        KiCad uses ``Sim.Enable="0"`` to keep a part on the schematic but exclude
        it from simulation. Excluded parts are skipped by both conversion and
        validation (and their pins do not count toward net connectivity).
        """
        val = self._sim_props(component).get("enable", None)
        if val is None:
            return False
        return str(val).strip().lower() in ("0", "false", "no", "off")

    def _kind(self, component) -> Optional[str]:
        """SPICE primitive kind for a component: ``Sim.Device`` wins over the symbol.

        Returns None for an unrecognized ``Sim.Device`` token or an unmapped symbol
        (validation reports it, naming the offending token/symbol).
        """
        device = self._sim_props(component).get("device", None)
        if device:
            return self._SIM_DEVICE_KINDS.get(str(device).strip().upper())
        return self._classify(self._attr(component, "symbol", ""))

    def _add_component(self, component):
        """Add a single component to the SPICE circuit."""
        symbol = getattr(component, "symbol", "")
        ref = getattr(component, "ref", "X")
        value = getattr(component, "value", None)

        if self._sim_excluded(component):
            logger.info(f"{ref}: excluded from simulation (Sim.Enable=0)")
            return

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
        handler = handlers.get(self._kind(component))
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

    def _device_model_name(self, component) -> Optional[str]:
        """Model name a diode/BJT/MOSFET references (shared by handlers + validate).

        Returns ``None`` if the component is not a modelled semiconductor. A bare
        type keyword in ``value`` (``'npn'``/``'pmos'``/...) selects the matching
        generic; an empty ``value`` falls back to the ``Default*`` generic implied
        by the symbol's polarity; any other ``value`` is used verbatim as the model
        name (which ``validate`` then checks resolves to a built-in).
        """
        kind = self._kind(component)
        if kind not in ("diode", "bjt", "mosfet"):
            return None
        symbol = str(self._attr(component, "symbol", "")).lower()
        # A Sim.Device token (NPN/PNP/NMOS/PMOS) sets polarity when the symbol
        # doesn't (e.g. a sim-only stand-in on a generic symbol).
        device = str(self._sim_props(component).get("device", "")).strip().lower()
        value = self._attr(component, "value", None)
        v = str(value).strip().lower() if value else ""
        if v in self._TYPE_KEYWORD_MODELS:
            return self._TYPE_KEYWORD_MODELS[v]
        if value:
            return str(value)
        if kind == "diode":
            return "DefaultDiode"
        if kind == "bjt":
            return "DefaultPNP" if ("pnp" in symbol or device == "pnp") else "DefaultNPN"
        return "DefaultPMOS" if ("pmos" in symbol or device == "pmos") else "DefaultNMOS"

    @staticmethod
    def _parse_sim_params(spec) -> dict:
        """Parse a KiCad ``Sim.Params`` string (``"bf=200 is=1e-14"``) to ``{K: v}``.

        Keys are upper-cased (ngspice model params are case-insensitive; upper-case
        matches the built-in generics). Accepts space- or comma-separated pairs.
        """
        out = {}
        if not spec:
            return out
        for token in str(spec).replace(",", " ").split():
            key, sep, val = token.partition("=")
            if sep and key.strip():
                out[key.strip().upper()] = val.strip()
        return out

    @staticmethod
    def _coerce_param(value):
        """Coerce a model-param value to float when it looks numeric, else keep it."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return value

    def _resolve_device_model(self, component, ref) -> Optional[str]:
        """Model name a device instance should reference, applying Sim.Params.

        Without overrides this is just ``_device_model_name`` (registered in
        ``used_models`` for card emission). With a ``Sim.Params`` override on a
        built-in base, a per-device derived card (``{base}_{ref}``) is synthesized
        so the override is local to this device. Returns None for non-semiconductors.
        """
        base = self._device_model_name(component)
        if base is None:
            return None
        overrides = self._parse_sim_params(self._sim_props(component).get("params"))
        if not overrides:
            self.used_models.add(base)
            return base
        spec = self.GENERIC_MODELS.get(base)
        if spec is None:
            # Unknown base (validate() will flag it); can't synthesize a card
            # without a device type, so reference the base verbatim.
            self.used_models.add(base)
            return base
        device_type, params = spec
        merged = dict(params)
        for key, val in overrides.items():
            merged[key] = self._coerce_param(val)
        derived = f"{base}_{ref}"
        self.derived_models[derived] = (device_type, merged)
        return derived

    def _emit_models(self):
        """Emit a ``.model`` card for each referenced built-in and derived model.

        Only models actually used by a component are emitted (PySpice would
        otherwise serialize every registered model). Unresolved custom model names
        are left to ``validate`` to report; in the lenient path they simply have no
        card (ngspice then errors on them, as before)."""
        for name in sorted(self.used_models):
            spec = self.GENERIC_MODELS.get(name)
            if spec is None:
                continue
            device_type, params = spec
            self.spice_circuit.model(name, device_type, **params)
            logger.debug(f"Emitted .model {name} {device_type}")
        for name in sorted(self.derived_models):
            device_type, params = self.derived_models[name]
            self.spice_circuit.model(name, device_type, **params)
            logger.debug(f"Emitted derived .model {name} {device_type}")

    def _add_diode(self, component, ref: str, value: str):
        """Add diode to SPICE circuit."""
        nodes = self._get_component_nodes(component)
        if len(nodes) < 2:
            logger.warning(f"Diode {ref} needs 2 connections, got {len(nodes)}")
            return

        model_name = self._resolve_device_model(component, ref) or "DefaultDiode"
        self.spice_circuit.D(ref, nodes[0], nodes[1], model=model_name)
        logger.debug(f"Added diode {ref}: {nodes[0]} -> {nodes[1]} model={model_name}")

    # Ideal op-amp open-loop gain. Large enough that closed-loop behaviour is set
    # by the feedback network, frequency-independent (infinite GBW) so an active
    # filter's response is the RC network alone.
    OPAMP_OPEN_LOOP_GAIN = 1e6

    def _opamp_terminals(self, component):
        """Resolve an op-amp's (out, in+, in-) SPICE nodes by pin function/name.

        Op-amp pins must be mapped semantically, not by position: KiCad pinouts
        vary (an LM358 unit is out=1, in-=2, in+=3), so pin-number order would
        swap the inputs. Uses the live pin map and considers only *connected*
        pins, so the unused unit of a dual op-amp (pins with no net) is skipped.
        Power pins (V+/V-) are not needed by the ideal model. Returns
        (out, in_plus, in_minus) spice nodes, or None if a signal terminal is
        missing or no live pin map is available.
        """
        pin_map = getattr(component, "_pins", None)
        if not isinstance(pin_map, dict):
            return None
        out = in_plus = in_minus = None
        for pin in pin_map.values():
            net = getattr(pin, "net", None)
            if net is None:
                continue  # unconnected (e.g. the unused unit of a dual op-amp)
            func = str(getattr(pin, "func", "")).lower()
            name = (getattr(pin, "name", "") or "").strip()
            node = self.node_map.get(net.name, net.name)
            if "output" in func:
                out = node
            elif name == "+":
                in_plus = node
            elif name == "-":
                in_minus = node
        if out is None or in_plus is None or in_minus is None:
            return None
        return out, in_plus, in_minus

    def _add_opamp(self, component, ref: str, value: str):
        """Add an op-amp as an ideal VCVS: Vout = Aol * (V(in+) - V(in-)).

        Terminals are resolved by pin function/name so the model is correct
        regardless of the symbol's pin numbering. Falls back to a positional guess
        only when no live pin map is available (dict/JSON-shaped circuits).
        """
        terminals = self._opamp_terminals(component)
        if terminals is None:
            nodes = self._get_component_nodes(component)
            if len(nodes) < 3:
                logger.warning(
                    f"Op-amp {ref} needs at least 3 connections, got {len(nodes)}"
                )
                return
            # Legacy positional fallback: [out, in+, in-].
            out, in_plus, in_minus = nodes[0], nodes[1], nodes[2]
        else:
            out, in_plus, in_minus = terminals

        gain = self.OPAMP_OPEN_LOOP_GAIN
        self.spice_circuit.VCVS(
            ref, out, self.spice_circuit.gnd, in_plus, in_minus, gain
        )
        logger.debug(
            f"Added op-amp {ref} (ideal VCVS, gain={gain}): "
            f"out={out}, in+={in_plus}, in-={in_minus}"
        )

    def _add_bjt_transistor(self, component, ref: str, value: str):
        """Add BJT transistor to SPICE circuit."""
        nodes = self._get_component_nodes(component)
        if len(nodes) < 3:
            logger.warning(f"BJT {ref} needs 3 connections (C,B,E), got {len(nodes)}")
            return

        # Determine model (NPN/PNP) from value keyword or symbol polarity, applying
        # any Sim.Params override.
        model_name = self._resolve_device_model(component, ref) or "DefaultNPN"

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

        # Determine model (NMOS/PMOS) from value keyword or symbol polarity, applying
        # any Sim.Params override.
        model_name = self._resolve_device_model(component, ref) or "DefaultNMOS"

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
        """Add voltage source to SPICE circuit.

        The emitted SPICE spec depends on the KiCad symbol:

        * ``VDC`` -> a DC value (from ``value``, default 5 V).
        * ``VAC`` -> ``DC 0 AC <mag>`` (small-signal only; ``value`` is the AC mag).
        * ``VSIN`` -> ``DC <off> AC <acmag> SIN(<off> <ampl> <freq> <td> <theta>)``
          -- both AC-analysis magnitude *and* a transient sinusoid, so one source
          works for both `.ac` and `.tran`. ``value`` sets ampl+acmag by default.
        * ``VPULSE`` -> ``PULSE(<v1> <v2> <td> <tr> <tf> <pw> <per>)``.
        * ``VPWL`` -> ``PWL(<t1> <v1> <t2> <v2> ...)`` (from a ``points`` field).

        Waveform parameters are read from the component's extra fields (any kwarg
        passed to ``Component`` lands in ``_extra_fields``), e.g.
        ``Component(symbol="Simulation_SPICE:VSIN", amplitude="1", frequency="1k")``.
        Numeric values keep their SI suffix (``1k``/``1m``/``1u``/``1n``) -- ngspice
        parses those directly.
        """
        nodes = self._get_component_nodes(component)
        if len(nodes) < 2:
            logger.warning(
                f"Voltage source {ref} needs 2 connections, got {len(nodes)}"
            )
            return

        # Add to list of voltage sources for tracking
        self.voltage_sources.append(ref)
        # Mark this source's nets so the net-name heuristic won't double-drive them.
        self.driven_nets.update(self._component_net_names(component))

        symbol = (getattr(component, "symbol", "") or "").upper()
        params = self._source_params(component)
        spec = self._voltage_source_spec(symbol, value, params)

        # nodes[] is in pin-number order, so nodes[0] is pin 1 (KiCad Sim.Pins
        # "1=+") and nodes[1] is pin 2 ("2=-"): V(name, +, -, spec).
        self.spice_circuit.V(ref, nodes[0], nodes[1], spec)
        logger.debug(
            f"Added voltage source {ref}: {nodes[0]}(+) -> {nodes[1]}(-) = {spec}"
        )

    def _source_params(self, component) -> dict:
        """Lowercased waveform-param map for a source (empty if none).

        Merges KiCad ``Sim.Params`` (as a base) with the component's own extra
        fields, where an explicit extra field wins over a ``Sim.Params`` value.
        The ``Sim.*`` fields themselves are dropped so they don't masquerade as
        waveform params.
        """
        extra = getattr(component, "_extra_fields", None)
        if not isinstance(extra, dict):
            return {}
        merged = {
            k.lower(): v
            for k, v in self._parse_sim_params(
                self._sim_props(component).get("params")
            ).items()
        }
        for k, v in extra.items():
            low = str(k).lower()
            if low.startswith("sim.") or low.startswith("sim_"):
                continue
            merged[low] = v
        return merged

    @staticmethod
    def _wave_num(value, default: str) -> str:
        """Normalize a waveform number, keeping its SI suffix for ngspice.

        Strips a trailing unit (V/A/Hz/s/F/H) so ``"1kHz"`` -> ``"1k"`` and
        ``"5V"`` -> ``"5"`` while leaving the SI prefix (k/m/u/n/p) intact.
        Returns ``default`` when ``value`` is None/empty.
        """
        if value is None:
            return default
        s = str(value).strip()
        if not s:
            return default
        for unit in ("hz", "v", "a", "s", "f", "h"):
            if len(s) > len(unit) and s.lower().endswith(unit):
                s = s[: -len(unit)]
                break
        return s

    def _pick(self, params: dict, keys, default: str) -> str:
        """First present param among ``keys`` (normalized), else ``default``."""
        for k in keys:
            if k in params and params[k] is not None and str(params[k]).strip():
                return self._wave_num(params[k], default)
        return default

    def _voltage_source_spec(self, symbol: str, value, params: dict):
        """Build the SPICE source spec (string or float) from symbol + params."""
        if "VPULSE" in symbol:
            v1 = self._pick(params, ("v1", "initial", "low"), "0")
            v2 = self._pick(
                params,
                ("v2", "pulsed", "high", "amplitude"),
                self._wave_num(value, "1"),
            )
            td = self._pick(params, ("td", "delay"), "0")
            tr = self._pick(params, ("tr", "rise"), "1n")
            tf = self._pick(params, ("tf", "fall"), "1n")
            pw = self._pick(params, ("pw", "width"), "0.5m")
            per = self._pick(params, ("per", "period"), "1m")
            return f"PULSE({v1} {v2} {td} {tr} {tf} {pw} {per})"

        if "VPWL" in symbol:
            pts = self._pwl_points(params)
            if pts:
                return f"PWL({pts})"
            # No points given -> degrade to a DC source at `value` (or 0).
            return self._convert_value_to_spice(value or "0V", "V")

        if "VSIN" in symbol:
            base = self._wave_num(value, "1")  # value sets ampl + AC mag by default
            offset = self._pick(params, ("offset", "dc", "voffset"), "0")
            ampl = self._pick(params, ("amplitude", "ampl", "amp"), base)
            freq = self._pick(params, ("frequency", "freq", "f"), "1k")
            td = self._pick(params, ("td", "delay"), "0")
            theta = self._pick(params, ("theta", "damping"), "0")
            ac_mag = self._pick(params, ("ac", "ac_mag", "acmag"), base)
            return f"DC {offset} AC {ac_mag} SIN({offset} {ampl} {freq} {td} {theta})"

        if "VAC" in symbol:
            ac_mag = self._convert_value_to_spice(value, "V") if value else 1.0
            return f"DC 0 AC {ac_mag}"

        # VDC / default: a plain DC value.
        return self._convert_value_to_spice(value or "5V", "V")

    def _pwl_points(self, params: dict) -> str:
        """Render VPWL points ('t1 v1 t2 v2 ...') from a ``points`` field.

        Accepts a list/tuple of (t, v) pairs, a flat list, or a whitespace/
        comma-separated string. Returns '' if no usable points are present.
        """
        pts = params.get("points")
        if pts is None:
            return ""
        if isinstance(pts, str):
            toks = [t for t in pts.replace(",", " ").split() if t]
            return " ".join(self._wave_num(t, "0") for t in toks)
        if isinstance(pts, (list, tuple)):
            flat = []
            for item in pts:
                if isinstance(item, (list, tuple)):
                    flat.extend(item)
                else:
                    flat.append(item)
            return " ".join(self._wave_num(t, "0") for t in flat)
        return ""

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

    def _net_live_pin_count(self, net, excluded_refs) -> Optional[int]:
        """Pins on a net whose owning component isn't excluded, or None if unknown.

        A ``Sim.Enable=0`` part's pins don't hold a net up, so a net left with
        fewer than two *live* pins is still floating.
        """
        pins = getattr(net, "pins", None)
        if pins is None:
            return None
        try:
            count = 0
            for pin in pins:
                comp = getattr(pin, "_component", None)
                cref = getattr(comp, "ref", None)
                if cref is not None and cref in excluded_refs:
                    continue
                count += 1
            return count
        except TypeError:
            return None

    def _all_driven_net_names(self) -> set:
        """Nets that will carry a source: explicit source components + heuristic rails.

        An excluded (``Sim.Enable=0``) source doesn't drive anything, so it isn't
        counted as excitation.
        """
        driven = set()
        for component in self._iter_components():
            if self._sim_excluded(component):
                continue
            if self._kind(component) in (
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

        # Parts opted out via Sim.Enable=0 are ignored by every check below and
        # don't count toward net connectivity.
        excluded_refs = set()
        for component in self._iter_components():
            if self._sim_excluded(component):
                excluded_refs.add(self._attr(component, "ref", None) or "?")

        # 1. Every component must map to a known SPICE primitive (Sim.Device wins).
        for component in self._iter_components():
            if self._sim_excluded(component):
                continue
            symbol = self._attr(component, "symbol", "")
            ref = self._attr(component, "ref", None) or "?"
            if self._kind(component) is None:
                device = self._sim_props(component).get("device", None)
                if device:
                    problems.append(
                        f"{ref}: Sim.Device '{device}' is not a supported device "
                        f"(known: {', '.join(sorted(self._SIM_DEVICE_KINDS))})"
                    )
                else:
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
            count = (
                self._net_live_pin_count(net, excluded_refs)
                if excluded_refs
                else self._net_pin_count(net)
            )
            if count is None:
                continue  # can't tell (dict/JSON net) -> don't block
            if count < 2:
                problems.append(
                    f"net '{name}' has {count} connection(s); needs >=2 or a source "
                    f"(floating node)"
                )

        # 4. Op-amps need at least their three signal pins connected.
        for component in self._iter_components():
            if self._sim_excluded(component):
                continue
            if self._kind(component) != "opamp":
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

        # 5. Every diode/BJT/MOSFET must reference a model that resolves to a
        #    built-in generic (otherwise ngspice errors on an undefined model).
        for component in self._iter_components():
            if self._sim_excluded(component):
                continue
            model = self._device_model_name(component)
            if model is None:
                continue
            if model not in self.GENERIC_MODELS:
                ref = self._attr(component, "ref", None) or "?"
                known = ", ".join(sorted(self.GENERIC_MODELS))
                problems.append(
                    f"{ref}: references SPICE model '{model}' with no .model card "
                    f"(no matching built-in; known generics: {known})"
                )

        if problems:
            raise SimulationValidationError(problems)
