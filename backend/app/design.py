"""
CircuitProof shared design model (design.py)

One format used by every module:
  planner (Nemotron) -> produces it
  generator          -> draws it as .kicad_sch
  scan_project       -> extracts it from an existing .kicad_sch
  circuitproof_core  -> audits it

{
  "components": [{"reference": "U1", "lib_id": "MCU_ST_STM32F1:STM32F103C8Tx",
                  "value": "STM32F103C8T6", "part_id": "", "block": "mcu", "footprint": ""}],
  "nets":       [{"net": "+3V3", "connections": [["U1", "VDD"], ["R1", "1"]]}],
  "rails":      {"+3V3": 3.3},                                   # optional
  "buses":      [{"type": "spi", "clock_hz": 8e6, "devices": ["U3"]}]   # optional
}
"""

import re
from dataclasses import asdict, dataclass, field

BLOCKS = ("power", "mcu", "peripheral")


class DesignError(ValueError):
    """Raised with a list of human-readable problems (also fed back to the LLM)."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass
class Component:
    reference: str
    lib_id: str
    value: str = ""
    part_id: str = ""
    block: str = "peripheral"
    footprint: str = ""


@dataclass
class Net:
    net: str
    connections: list  # list of [reference, pin] pairs; pin is a name or a number


@dataclass
class Bus:
    type: str
    clock_hz: float
    devices: list


@dataclass
class Design:
    components: list
    nets: list
    rails: dict = field(default_factory=dict)
    buses: list = field(default_factory=list)

    def component(self, ref):
        return next((c for c in self.components if c.reference == ref), None)

    def to_dict(self):
        return asdict(self)

    def generator_inputs(self):
        comps = [asdict(c) for c in self.components]
        nets = [{"net": n.net, "connections": [tuple(c) for c in n.connections]} for n in self.nets]
        return comps, nets


def parse_design(data):
    """Validate a raw dict (e.g. LLM JSON) and return a Design, or raise DesignError."""
    errors = []
    if not isinstance(data, dict):
        raise DesignError(["Top level must be a JSON object"])

    comps_raw = data.get("components")
    if not isinstance(comps_raw, list) or not comps_raw:
        raise DesignError(["'components' must be a non-empty list"])

    components, refs = [], set()
    for i, c in enumerate(comps_raw):
        if not isinstance(c, dict):
            errors.append(f"components[{i}] must be an object")
            continue
        ref = str(c.get("reference", "")).strip()
        lib_id = str(c.get("lib_id", "")).strip()
        if not ref:
            errors.append(f"components[{i}] is missing 'reference'")
            continue
        if ":" not in lib_id:
            errors.append(f"{ref}: 'lib_id' must look like 'Library:Symbol' (got '{lib_id}')")
        if ref in refs:
            errors.append(f"Duplicate reference '{ref}'")
        refs.add(ref)
        block = str(c.get("block", "peripheral")).lower()
        components.append(Component(
            reference=ref, lib_id=lib_id, value=str(c.get("value") or ""),
            part_id=str(c.get("part_id") or ""),
            block=block if block in BLOCKS else "peripheral",
            footprint=str(c.get("footprint") or "")))

    nets_raw = data.get("nets", [])
    if not isinstance(nets_raw, list):
        errors.append("'nets' must be a list")
        nets_raw = []
    nets, names, used_pins = [], set(), {}
    for i, n in enumerate(nets_raw):
        if not isinstance(n, dict) or not str(n.get("net", "")).strip():
            errors.append(f"nets[{i}] needs a 'net' name")
            continue
        name = str(n["net"]).strip()
        if name in names:
            errors.append(f"Net '{name}' is defined twice; merge its connections")
        names.add(name)
        conns = []
        for pair in n.get("connections", []) or []:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                errors.append(f"Net '{name}': each connection must be [reference, pin], got {pair!r}")
                continue
            ref, pin = str(pair[0]).strip(), str(pair[1]).strip()
            if ref not in refs:
                errors.append(f"Net '{name}' uses unknown component '{ref}'")
                continue
            key = (ref, pin)
            if key in used_pins and used_pins[key] != name:
                errors.append(f"{ref} pin {pin} is in both '{used_pins[key]}' and '{name}' (short circuit)")
                continue
            used_pins[key] = name
            if [ref, pin] not in conns:
                conns.append([ref, pin])
        nets.append(Net(net=name, connections=conns))

    rails = {}
    for k, v in (data.get("rails") or {}).items():
        try:
            rails[str(k)] = float(v)
        except (TypeError, ValueError):
            errors.append(f"Rail '{k}' voltage must be a number (got {v!r})")

    buses = []
    for i, b in enumerate(data.get("buses") or []):
        try:
            btype = str(b["type"]).lower()
            hz = float(b["clock_hz"])
            devs = [str(d) for d in b.get("devices", [])]
        except (KeyError, TypeError, ValueError):
            errors.append(f"buses[{i}] needs 'type', numeric 'clock_hz' and 'devices'")
            continue
        unknown = [d for d in devs if d not in refs]
        if unknown:
            errors.append(f"buses[{i}] references unknown components {unknown}")
        buses.append(Bus(type=btype, clock_hz=hz, devices=devs))

    if errors:
        raise DesignError(errors)
    return Design(components=components, nets=nets, rails=rails, buses=buses)


# ---------------------------------------------------------------- rail helpers
_GROUND = re.compile(r"^(GND|AGND|DGND|PGND|GNDA|GNDD|GNDPWR|VSS|0V)$", re.I)


def net_basename(name):
    """KiCad prefixes local nets with the sheet path ('/SDA', '/sub/SDA')."""
    return str(name).rstrip("/").split("/")[-1]


def is_ground_net(name):
    return bool(_GROUND.match(net_basename(name).lstrip("+-")))


def rail_voltage(name, rails=None):
    """Voltage of a power net from explicit rails or its name (+3V3, 3.3V, VCC_5V, GND)."""
    rails = rails or {}
    if name in rails:
        return float(rails[name])
    base = net_basename(name)
    if base in rails:
        return float(rails[base])
    if is_ground_net(base):
        return 0.0
    m = re.search(r"(?<![\d.])(\d+)V(\d+)(?![\d])", base, re.I)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    m = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)V(?![A-Za-z0-9])", base, re.I)
    if m:
        return float(m.group(1))
    return None
