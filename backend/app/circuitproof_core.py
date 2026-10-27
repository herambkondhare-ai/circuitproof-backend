"""
CircuitProof Core Referee Engine (circuitproof_core.py)

Deterministic rule checks over a Design (see design.py). Every finding is one of:
  PASS        rule checked and satisfied
  FAIL        rule checked and violated (logged to the knowledge store)
  WARN        likely problem that needs a human look
  UNVERIFIED  rule could not be checked because data is missing; never silently assumed
"""

import re
from dataclasses import asdict, dataclass, field

from design import is_ground_net, rail_voltage

PASS, FAIL, WARN, UNVERIFIED = "PASS", "FAIL", "WARN", "UNVERIFIED"
STATUS_ORDER = {FAIL: 0, WARN: 1, UNVERIFIED: 2, PASS: 3}

SUPPLY_PIN = re.compile(r"^(VDD\w*|VCC\w*|AVDD|DVDD|VDDIO|VIN|VBAT|V\+|3V3|5V)$", re.I)
GROUND_PIN = re.compile(r"^(GND\w*|VSS\w*|AGND|DGND|PGND)$", re.I)
I2C_NET = re.compile(r"SDA|SCL", re.I)
IO_KEYS = ("vih_min", "vil_max", "voh_min", "vol_max", "vin_max")
# Reference prefixes that need no datasheet facts for these rules
PASSIVE_PREFIXES = {"R", "C", "L", "FB", "D", "LED", "J", "P", "TP", "SW", "Y", "F", "BT", "JP", "H", "MH"}


@dataclass
class Finding:
    rule: str
    status: str
    message: str
    refs: list = field(default_factory=list)
    fix: str = ""
    data_verified: object = None  # True/False when based on part facts, None otherwise

    def to_dict(self):
        return asdict(self)


def ref_prefix(ref):
    m = re.match(r"[A-Za-z#]+", ref)
    return m.group(0).upper() if m else ""


def parse_ohms(value):
    s = str(value).strip().upper().replace("Ω", "").replace("OHM", "").replace(" ", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([RKM]?)(\d*)", s)
    if not m:
        return None
    num, mult, tail = m.groups()
    if tail:
        if "." in num:
            return None
        num = f"{num}.{tail}"
    return float(num) * {"": 1, "R": 1, "K": 1e3, "M": 1e6}[mult]


def pin_io(facts, pin):
    io = dict(facts.get("io", {}))
    io.update({k: v for k, v in facts.get("pins", {}).get(pin, {}).items() if k in IO_KEYS})
    return io


def summarize(findings):
    counts = {s: sum(1 for f in findings if f.status == s) for s in (FAIL, WARN, UNVERIFIED, PASS)}
    if counts[FAIL]:
        verdict = "FAIL"
    elif counts[WARN] or counts[UNVERIFIED]:
        verdict = "NEEDS_REVIEW"
    else:
        verdict = "PASS"
    return {"verdict": verdict, "counts": counts}


class CircuitReferee:
    def __init__(self, store):
        self.store = store

    # ------------------------------------------------------------ entry point
    def audit(self, design, unconnected_pins=(), extra_warnings=(), log_mistakes=True, symbol_pins=None):
        ctx = self._context(design)
        ctx["symbol_pins"] = symbol_pins or {}
        findings = []
        findings += self._rule_known_parts(design, ctx)
        findings += self._rule_unconnected_power(unconnected_pins, ctx)
        findings += self._rule_net_connectivity(design, ctx)
        findings += self._rule_supply(design, ctx)
        findings += self._rule_decoupling(design, ctx)
        findings += self._rule_i2c(design, ctx)
        findings += self._rule_bus_clock(design, ctx)
        findings += [Finding("generator", WARN, w) for w in extra_warnings]
        findings.sort(key=lambda f: STATUS_ORDER[f.status])
        if log_mistakes:
            for f in findings:
                if f.status == FAIL:
                    self.store.log_mistake(f.message, f.fix)
        return findings

    def _context(self, design):
        comp_pins = {c.reference: {} for c in design.components}   # ref -> {pin: net}
        for n in design.nets:
            for ref, pin in n.connections:
                comp_pins.setdefault(ref, {})[pin] = n.net
        parts = {c.reference: self.store.resolve(c.lib_id, c.value, c.part_id)
                 for c in design.components}
        return {"comp_pins": comp_pins, "parts": parts, "rails": design.rails,
                "comps": {c.reference: c for c in design.components}}

    # ------------------------------------------------------------ rules
    def _rule_known_parts(self, design, ctx):
        missing = [c for c in design.components
                   if ref_prefix(c.reference) not in PASSIVE_PREFIXES and ctx["parts"][c.reference] is None]
        if not missing:
            return []
        names = ", ".join(f"{c.reference} ({c.value or c.lib_id})" for c in missing)
        return [Finding("part-data", UNVERIFIED,
                        f"No datasheet facts for {names}; voltage and timing rules for these parts "
                        f"cannot be checked.", [c.reference for c in missing],
                        "Add the parts to the knowledge store (load_components.py).")]

    def _rule_unconnected_power(self, unconnected_pins, ctx):
        out = []
        for ref, number, name in unconnected_pins:
            if SUPPLY_PIN.match(name or "") or GROUND_PIN.match(name or ""):
                out.append(Finding("power-pins", FAIL,
                                   f"{ref} power pin {name} (pin {number}) is not connected.", [ref],
                                   f"Connect {ref}.{name} to the correct rail."))
        return out

    def _rule_net_connectivity(self, design, ctx):
        out = []
        for n in design.nets:
            if len(n.connections) >= 2:
                continue
            if not n.connections:
                out.append(Finding("connectivity", WARN, f"Net '{n.net}' has no connections."))
                continue
            ref, pin = n.connections[0]
            if rail_voltage(n.net, ctx["rails"]) is not None:
                continue  # a rail reaching one pin is fine; it can be sourced off-sheet
            if SUPPLY_PIN.match(pin) or GROUND_PIN.match(pin):
                out.append(Finding("power-pins", FAIL, f"{ref} power pin {pin} is not connected "
                                   f"to anything else (net '{n.net}').", [ref],
                                   f"Connect {ref}.{pin} to its rail."))
            elif not n.net.startswith("unconnected-"):   # KiCad's name for deliberately unused pins
                out.append(Finding("connectivity", WARN,
                                   f"Net '{n.net}' only reaches {ref}.{pin}; it goes nowhere.", [ref],
                                   "Connect the other end, or remove the label."))
        return out

    def _rule_supply(self, design, ctx):
        out = []
        for c in design.components:
            ref = c.reference
            if ref_prefix(ref) in PASSIVE_PREFIXES:
                continue
            pins = ctx["comp_pins"].get(ref, {})
            part = ctx["parts"][ref]
            facts = part["facts"] if part else {}
            declared = facts.get("supply_pins") or []

            for p in declared:
                if p not in pins:
                    out.append(Finding("supply", FAIL, f"{ref} supply pin {p} is not connected.",
                                       [ref], f"Connect {ref}.{p} to a rail within "
                                       f"{facts.get('vdd_min')}–{facts.get('vdd_max')} V.",
                                       part["verified"]))

            for pin, net in pins.items():
                is_supply = (pin in declared) if declared else bool(SUPPLY_PIN.match(pin))
                if GROUND_PIN.match(pin) and not is_ground_net(net):
                    out.append(Finding("ground", FAIL, f"{ref} ground pin {pin} is on net '{net}', "
                                       f"not ground.", [ref], f"Connect {ref}.{pin} to GND."))
                if not is_supply:
                    continue
                v = rail_voltage(net, ctx["rails"])
                if v is None:
                    out.append(Finding("supply", WARN, f"{ref}.{pin} is on net '{net}', which is not a "
                                       f"recognisable power rail.", [ref],
                                       "Name rails with their voltage (+3V3, +5V) or declare them in 'rails'."))
                elif v == 0:
                    out.append(Finding("supply", FAIL, f"{ref} supply pin {pin} is tied to ground "
                                       f"('{net}').", [ref], f"Connect {ref}.{pin} to its supply rail."))
                elif "vdd_min" in facts and "vdd_max" in facts:
                    lo, hi = facts["vdd_min"], facts["vdd_max"]
                    if lo <= v <= hi:
                        out.append(Finding("supply", PASS, f"{ref}.{pin} on {net} ({v:g} V) is within "
                                           f"{lo}–{hi} V.", [ref], data_verified=part["verified"]))
                    else:
                        out.append(Finding("supply", FAIL, f"{ref}.{pin} is powered from {net} ({v:g} V) "
                                           f"but {part['part_id']} allows {lo}–{hi} V.", [ref],
                                           f"Power {ref} from a rail between {lo} and {hi} V.",
                                           part["verified"]))
                elif part:
                    out.append(Finding("supply", UNVERIFIED, f"{ref} has no supply range in the "
                                       f"knowledge store; {v:g} V on {pin} not checked.", [ref],
                                       "Add vdd_min / vdd_max for this part.", part["verified"]))
        return out

    def _rule_decoupling(self, design, ctx):
        """Every rail that powers an IC needs at least one capacitor to ground."""
        out = []
        caps_by_rail = {}
        for c in design.components:
            if ref_prefix(c.reference) != "C":
                continue
            nets = set(ctx["comp_pins"].get(c.reference, {}).values())
            if any(is_ground_net(n) for n in nets):
                for n in nets:
                    v = rail_voltage(n, ctx["rails"])
                    if v is not None and v > 0:
                        caps_by_rail.setdefault(n, []).append(c.reference)
        ics_by_rail, pins_by_rail = {}, {}
        for c in design.components:
            if ref_prefix(c.reference) in PASSIVE_PREFIXES:
                continue
            sym = ctx["symbol_pins"].get(c.reference)
            for pin, net in ctx["comp_pins"].get(c.reference, {}).items():
                v = rail_voltage(net, ctx["rails"])
                if not (SUPPLY_PIN.match(pin) and v is not None and v > 0):
                    continue
                ics_by_rail.setdefault(net, set()).add(c.reference)
                # physical pins behind this connection (e.g. "VDD" = pins 24, 36, 48 on an STM32)
                count = len({p["number"] for p in sym if pin in (p["number"], p["name"])}) if sym else 1
                pins_by_rail[net] = pins_by_rail.get(net, 0) + max(count, 1)
        for net, ics in sorted(ics_by_rail.items()):
            caps = caps_by_rail.get(net, [])
            ics, n_pins = sorted(ics), pins_by_rail.get(net, len(ics))
            if not caps:
                out.append(Finding("decoupling", FAIL, f"Rail '{net}' powers {', '.join(ics)} but has no "
                                   f"decoupling capacitor to ground.", ics,
                                   f"Add a 100nF capacitor from '{net}' to GND for each of the {n_pins} supply pin(s)."))
            elif len(caps) < n_pins:
                out.append(Finding("decoupling", WARN, f"Rail '{net}' feeds {n_pins} supply pin(s) on "
                                   f"{', '.join(ics)} but has only {len(caps)} decoupling capacitor(s).",
                                   ics, f"Add {n_pins - len(caps)} more 100nF capacitor(s) from '{net}' to GND, "
                                        f"one per supply pin."))
            else:
                out.append(Finding("decoupling", PASS, f"Rail '{net}' has {len(caps)} decoupling "
                                   f"capacitor(s) for {n_pins} supply pin(s).", ics))
        return out

    def _rule_i2c(self, design, ctx):
        out = []
        for n in design.nets:
            functions = []
            for ref, pin in n.connections:
                part = ctx["parts"].get(ref)
                if part:
                    functions += part["facts"].get("pins", {}).get(pin, {}).get("functions", [])
            if not (I2C_NET.search(n.net) or any(I2C_NET.search(f) for f in functions)):
                continue
            line = "SDA" if ("SDA" in n.net.upper() or any("SDA" in f.upper() for f in functions)) else "SCL"

            pullups, devices = [], []
            for ref, pin in n.connections:
                if ref_prefix(ref) == "R":
                    others = [net for p, net in ctx["comp_pins"].get(ref, {}).items() if net != n.net]
                    for other in others:
                        v = rail_voltage(other, ctx["rails"])
                        if v is None:
                            continue
                        if v == 0:
                            out.append(Finding("i2c", WARN, f"{ref} pulls {line} net '{n.net}' to ground; "
                                               f"I2C lines need pull-ups, not pull-downs.", [ref]))
                        else:
                            pullups.append((ref, v, other))
                else:
                    devices.append((ref, pin))

            if not pullups:
                out.append(Finding("i2c", FAIL, f"I2C {line} net '{n.net}' has no pull-up resistor. "
                                   f"I2C is open-drain, so the line can never go high.",
                                   [r for r, _ in devices],
                                   f"Add a pull-up (typically 2.2k–10k) from '{n.net}' to the I/O rail, e.g. +3V3."))
                continue
            if len(pullups) > 1:
                out.append(Finding("i2c", WARN, f"'{n.net}' has {len(pullups)} pull-ups "
                                   f"({', '.join(r for r, _, _ in pullups)}); they act in parallel.",
                                   [r for r, _, _ in pullups], "Keep one pull-up per line unless intended."))
            for r, _, _ in pullups:
                ohms = parse_ohms(ctx["comps"][r].value)
                if ohms is None:
                    out.append(Finding("i2c", UNVERIFIED, f"Pull-up {r} on '{n.net}' has no readable "
                                       f"resistance value ('{ctx['comps'][r].value}').", [r],
                                       "Set the value, e.g. 4.7k."))
                elif not 1e3 <= ohms <= 10e3:
                    out.append(Finding("i2c", WARN, f"Pull-up {r} on '{n.net}' is {ohms:g} Ω; the usual "
                                       f"range is 1k–10k.", [r], "Use 2.2k–4.7k for 100–400 kHz buses."))

            v_pu = max(v for _, v, _ in pullups)
            rail = next(net for _, v, net in pullups if v == v_pu)
            fails_here, unknown = 0, []
            for ref, pin in devices:
                part = ctx["parts"].get(ref)
                if not part:
                    unknown.append(f"{ref}.{pin}")
                    continue
                io = pin_io(part["facts"], pin)
                if "vih_min" in io and v_pu < io["vih_min"]:
                    fails_here += 1
                    out.append(Finding("i2c", FAIL, f"'{n.net}' is pulled up to {v_pu:g} V ({rail}), below "
                                       f"{ref}.{pin} VIH(min) {io['vih_min']} V; a high will not register.",
                                       [ref], "Pull up to a higher rail or add a level translator.",
                                       part["verified"]))
                if "vin_max" in io:
                    if v_pu > io["vin_max"]:
                        fails_here += 1
                        out.append(Finding("i2c", FAIL, f"'{n.net}' is pulled up to {v_pu:g} V ({rail}), "
                                           f"above {ref}.{pin} maximum input {io['vin_max']} V; this can "
                                           f"damage {ref}.", [ref],
                                           "Pull up to the lower rail or add a level translator (e.g. PCA9306).",
                                           part["verified"]))
                else:
                    unknown.append(f"{ref}.{pin} (max input voltage)")
            if unknown:
                out.append(Finding("i2c", UNVERIFIED, f"Could not check {v_pu:g} V on '{n.net}' against "
                                   f"{', '.join(unknown)}: no data.", [u.split('.')[0] for u in unknown],
                                   "Add io thresholds / vin_max for these parts."))
            if not fails_here and not unknown:
                out.append(Finding("i2c", PASS, f"'{n.net}' pulled up to {v_pu:g} V; all devices accept it.",
                                   [r for r, _ in devices]))
        return out

    def _rule_bus_clock(self, design, ctx):
        out = []
        for bus in design.buses:
            if bus.type != "spi":
                continue
            for ref in bus.devices:
                part = ctx["parts"].get(ref)
                limit = part["facts"].get("spi_max_hz") if part else None
                if limit is None:
                    out.append(Finding("spi", UNVERIFIED, f"No SPI clock limit known for {ref}; "
                                       f"{bus.clock_hz / 1e6:g} MHz not checked.", [ref],
                                       "Add spi_max_hz for this part."))
                elif bus.clock_hz > limit:
                    out.append(Finding("spi", FAIL, f"SPI runs at {bus.clock_hz / 1e6:g} MHz but {ref} "
                                       f"({part['part_id']}) allows {limit / 1e6:g} MHz.", [ref],
                                       f"Lower the SPI prescaler to ≤ {limit / 1e6:g} MHz.", part["verified"]))
                else:
                    out.append(Finding("spi", PASS, f"SPI {bus.clock_hz / 1e6:g} MHz ≤ {ref} limit "
                                       f"{limit / 1e6:g} MHz.", [ref], data_verified=part["verified"]))
        return out
