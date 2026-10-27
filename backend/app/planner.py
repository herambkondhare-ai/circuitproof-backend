"""
Design planner (planner.py)

Nemotron proposes, the referee disposes:
  prompt -> Nemotron JSON -> schema check -> KiCad library check -> referee audit
        ^-------------- errors and FAIL findings fed back ----------------|
"""

import json
import time
from collections import Counter
from dataclasses import dataclass, field

from circuitproof_core import FAIL, GROUND_PIN, I2C_NET, PASSIVE_PREFIXES, SUPPLY_PIN, WARN, ref_prefix
import os
import re
import threading
from collections import OrderedDict

from design import Component, DesignError, Net, is_ground_net, parse_design, rail_voltage
from generator import MAIN_POWER_OUT, SchematicSynthesizer, SynthesisError
from llm_client import LLMBudgetError, LLMError, extract_json
from timing import set_stage, timed

# Overall time budget for one generation (seconds). Between repair rounds, if it is used up, the planner
# stops and returns the best valid design so far (needs_review) instead of starting another model call.
GEN_BUDGET_S = float(os.getenv("CP_GEN_BUDGET_S", "180"))
MIN_ROUND_S = 20          # don't start a repair round with less budget than this left
BLOCK_PLAN_TIMEOUT_S = 45  # the block plan is optional; a slow answer is replaced by the deterministic fallback
# Reply caps when thinking is off: a netlist JSON is a few thousand tokens; a runaway reply then stops early.
NETLIST_MAX_TOKENS, BLOCK_PLAN_MAX_TOKENS = 6000, 1500
CANDIDATE_MAX_PINS = 40   # library candidates: pins listed in the netlist prompt (supply/ground always kept)
CANDIDATE_MAX_DESC = 80
MAX_BLOCKS, PER_BLOCK = 6, 3
# Power inputs that must not be tied to the main rail automatically (USB bus power, core regulator caps,
# references): if left open they are sent back to the model instead.
NO_AUTO_SUPPLY = re.compile(r"^(VBUS\w*|VCAP\w*|VREF\w*|VUSB\w*)$", re.I)
# Default hardware I2C pins per chip family (symbol name pattern -> pin names), used when a model writes
# "SDA"/"SCL" for a chip whose pins are named by port/GPIO.
I2C_DEFAULT_PINS = [
    (r"^ESP32-WROOM-32|^ESP32-WROVER", {"SDA": "IO21", "SCL": "IO22"}),
    (r"^ESP32-C3", {"SDA": "IO8", "SCL": "IO9"}),
    (r"^ESP32-S3", {"SDA": "IO8", "SCL": "IO9"}),
    (r"^STM32F1|^STM32F4|^STM32G0|^STM32L0", {"SDA": "PB7", "SCL": "PB6"}),
    (r"^ATMEGA(328|168|88|48)", {"SDA": "PC4", "SCL": "PC5"}),
    (r"^ATMEGA(32U4)", {"SDA": "PD1", "SCL": "PD0"}),
    (r"^RP2040|^RASPBERRYPI_PICO", {"SDA": "GPIO4", "SCL": "GPIO5"}),
    (r"^OLED-128O064D", {"SDA": "D1", "SCL": "D0"}),   # SSD1306 module in I2C mode
]
# Indicator nets that may move to another free pin, and the pins they may move to (plain GPIOs only: no
# multi-function names like D13/SCK, no boot/UART pins).
INDICATOR_NET = re.compile(r"LED|STATUS|INDICATOR|BLINK", re.I)
FREE_GPIO = re.compile(r"^(GPIO(?!0$|1$|3$)\d+|IO(?!0$|1$|3$)\d+|P[A-K]\d{1,2}|D(?!0$|1$)\d+)$", re.I)
# MCU oscillator pin pairs (input, output) by name, for adding a requested crystal.
OSC_PIN_PAIRS = [(r"^XTAL1", r"^XTAL2"), (r"^XIN$", r"^XOUT$"), (r"^OSC_?IN", r"^OSC_?OUT"), (r"^XI$", r"^XO$")]
# Parts that bring power onto the board (their nets are real supplies even without a voltage in the name).
SOURCE_LIB_NAMES = re.compile(r"Battery|^Connector", re.I)
# Bus line names: two signal nets ending in the same one (I2C_SCL, BME280_SCL) that share a pin are one node.
BUS_SIGNALS = {"SCL", "SDA", "MOSI", "MISO", "SCK", "SCLK", "COPI", "CIPO", "TX", "RX", "TXD", "RXD",
               "CANH", "CANL", "DP", "DM"}
# Single-pin nets the planner completes: crystal nets, and signals that leave the board via a connector.
CRYSTAL_NET = re.compile(r"XTAL|OSC|XIN|XOUT|CRYSTAL", re.I)
OFFBOARD_NET = re.compile(r"CAN|RS-?485|^(A|B)$|RS485|USB|D[+-]$|_DP$|_DM$|UART|TXD?\b|RXD?\b|_TX|_RX|^TX|^RX|"
                          r"SWD|SWCLK|SWDIO|SWO|JTAG|TCK|TMS|TDI|TDO|EXT|PROG|CONN|HDR|HEADER", re.I)
# Generic pin names a model uses for "the ground pin" / "the supply pin" of any chip.
GENERIC_GROUND = re.compile(r"^(GND|VSS|0V|GROUND)$", re.I)
GENERIC_SUPPLY = re.compile(r"^(VCC|VDD|V\+|PWR|POWER|\+?3V3|\+?3\.3V?|\+?5V|\+?5\.0V?|VIN)$", re.I)
PLAN_CACHE_SIZE = 128

# Words that mean the request needs a functional block beyond an MCU/memory from the catalog plus R/C/LED.
NEEDS_LIBRARY = re.compile(
    r"\b(sensor|regulator|ldo|buck|boost|converter|usb|connector|header|jack|display|oled|lcd|motor|driver|"
    r"oscillator|button|switch|relay|transistor|mosfet|diode|buzzer|speaker|antenna|radio|wi-?fi|"
    r"bluetooth|ble|lora|can[ -]?bus|rs-?485|rs-?232|transceiver|battery|charger|eeprom|adc|dac|amplifier|op-?amp|"
    r"opamp|module|esp\w*|arduino|fuse|inductor|ethernet|sd|microsd|camera|gps|imu|accelerometer|gyro)\b", re.I)
# Tokens that look like part numbers (letters and digits mixed, e.g. BME280, AMS1117-3.3, STM32F103C8).
PART_TOKEN = re.compile(r"\b(?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*\d)[A-Z][A-Z0-9-]{3,}(?:\.\d+)?\b", re.I)
# ...but not rail names or MCU pin names (+3V3, 5V, PC13, PA5, GPIO2, IO4, ADC1).
NOT_A_PART = re.compile(r"^(\d+V\d*|V\d+V?\d*|P[A-K]\d{1,2}|GPIO\d+|IO\d+|ADC\d*|I2C\d?|SPI\d?|UART\d?|USART\d?)$", re.I)

GENERIC_PARTS = [
    {"lib_id": "Device:R", "part_id": "", "category": "Resistor"},
    {"lib_id": "Device:C", "part_id": "", "category": "Capacitor"},
    {"lib_id": "Device:LED", "part_id": "", "category": "LED"},
    {"lib_id": "Device:Crystal", "part_id": "", "category": "Crystal"},
]

SYSTEM_PROMPT = """You are the design planner inside CircuitProof, a hardware design verifier.
Turn the user's request into a schematic netlist. A deterministic rule checker audits your output
and you will be sent its findings to fix.

Reply with ONE JSON object and nothing else, in exactly this shape:
{
  "components": [
    {"reference": "U1", "lib_id": "<from the allowed parts list>", "value": "<part number or value, e.g. 4.7k>", "block": "power|mcu|peripheral"}
  ],
  "nets": [
    {"net": "+3V3", "connections": [["U1", "VDD"], ["R1", "1"]]}
  ],
  "rails": {"+3V3": 3.3, "GND": 0},
  "buses": [{"type": "spi", "clock_hz": 8000000, "devices": ["U3"]}]
}

Rules:
- Use only lib_id values from the allowed parts list, and pin names exactly as listed for that part.
  Resistors and capacitors use pin numbers "1" and "2".
- Every reference is unique (U1, U2, R1, C1 ...). A pin appears in at most one net.
- Power rails are ordinary nets named with their voltage ("+3V3", "+5V") plus "GND". Do not add power
  symbols. List every rail's voltage in "rails".
- Connect every supply and ground pin of every IC, including analog supplies (AVCC, VDDA) and the input of
  every regulator (VI/VIN) to the rail that feeds it. A regulator's output pin drives its own rail.
- A crystal is its own part: Device:Crystal (pins "1" and "2") between the MCU's XTAL/OSC pins, with a 22pF
  capacitor from each crystal pin to GND. Never put a frequency as the value of a capacitor.
- Add the support parts a real board needs, with values: a 100nF decoupling capacitor from each IC supply
  pin's rail to GND (one capacitor per supply pin), and pull-up resistors on I2C lines.
- An indicator LED is wired GPIO -> resistor -> LED anode, LED cathode -> GND, using separate nets for each
  connection (e.g. "LED_DRIVE" between the GPIO and the resistor, "LED_A" between the resistor and the LED).
- Include "buses" only for SPI links with a known clock speed; otherwise use an empty list.
- A net is ONE electrical node. Every pin appears in exactly one net, and each net name appears once.
  Never put a net name inside "connections"; every connection is exactly [reference, pin].

Worked example (an MCU with one decoupling capacitor and a status LED). Note every pin appears once:
{
  "components": [
    {"reference": "U1", "lib_id": "<MCU lib_id>", "value": "<MCU>", "block": "mcu"},
    {"reference": "C1", "lib_id": "Device:C", "value": "100nF", "block": "mcu"},
    {"reference": "R1", "lib_id": "Device:R", "value": "1k", "block": "peripheral"},
    {"reference": "D1", "lib_id": "Device:LED", "value": "LED", "block": "peripheral"}
  ],
  "nets": [
    {"net": "+3V3",      "connections": [["U1", "VDD"], ["C1", "1"]]},
    {"net": "GND",       "connections": [["U1", "VSS"], ["C1", "2"], ["D1", "K"]]},
    {"net": "LED_DRIVE", "connections": [["U1", "PC13"], ["R1", "1"]]},
    {"net": "LED_A",     "connections": [["R1", "2"], ["D1", "A"]]}
  ],
  "rails": {"+3V3": 3.3, "GND": 0},
  "buses": []
}
"""


BLOCK_PROMPT = """You are planning an electronics design for CircuitProof.
List the main functional blocks the request needs. Do NOT list resistors, capacitors, LEDs or crystals;
those are always available. For each block give a short description and 2-4 search terms that would find a matching
KiCad schematic symbol: a common part number if you know one (e.g. "AMS1117-3.3", "ESP32-WROOM-32",
"BME280"), plus function words (e.g. "LDO regulator 3.3V", "USB-C receptacle", "temperature sensor I2C").

Reply with ONE JSON object and nothing else:
{"blocks": [{"role": "mcu|power|sensor|interface|connector|other", "need": "<what it does>", "search": ["<term>", "<term>"]}]}
"""


class PlanningError(RuntimeError):
    def __init__(self, message, history):
        super().__init__(message)
        self.history = history


class PlanningTimeout(PlanningError):
    """The time budget ran out before the model produced any valid design."""


@dataclass
class PlanResult:
    design: object
    findings: list
    history: list = field(default_factory=list)
    passed: bool = False
    blocks: list = field(default_factory=list)
    candidates: list = field(default_factory=list)


class DesignPlanner:
    def __init__(self, llm, store, synth, referee, index=None, erc_check=None):
        self.llm, self.store, self.synth, self.referee = llm, store, synth, referee
        self.index = index      # SymbolIndex over all installed KiCad symbols (optional)
        self.erc_check = erc_check  # design -> list of KiCad ERC error messages, or None when kicad-cli is missing
        self._extra = []        # library parts found for the current request
        self.blocks = []
        self._plan_cache = OrderedDict()   # normalized prompt -> (blocks, candidates), in memory only
        self._cache_lock = threading.Lock()

    # ---------- stage 1: block plan, stage 2: library search
    def catalog_blocks(self, prompt):
        """If every part the prompt names is in the knowledge-store catalog and it asks for nothing that needs
        another library part, return block entries for those catalog parts (no LLM call needed); else None."""
        if NEEDS_LIBRARY.search(prompt):
            return None
        tokens = [t for t in PART_TOKEN.findall(prompt) if not NOT_A_PART.match(t)]
        if not tokens:
            return None
        blocks = []
        for token in tokens:
            t = token.upper()
            hit = next((e for e in self.store.catalog()
                        if t in e["part_id"].upper() or t in e["lib_id"].split(":")[-1].upper()), None)
            if hit is None:
                return None
            blocks.append({"role": (hit["category"] or "other").lower(),
                           "need": f"{hit['part_id']} (from the parts catalog)", "search": []})
        return list({b["need"]: b for b in blocks}.values())

    def plan_blocks(self, prompt, deadline=None):
        try:
            data = extract_json(self.llm.chat([{"role": "system", "content": BLOCK_PROMPT},
                                               {"role": "user", "content": prompt}], label="block plan",
                                              deadline=deadline, timeout=BLOCK_PLAN_TIMEOUT_S,
                                              max_tokens=BLOCK_PLAN_MAX_TOKENS))
            blocks = [b for b in data.get("blocks", []) if isinstance(b, dict) and b.get("need")]
        except (ValueError, AttributeError):
            blocks = []
        except LLMError as e:  # the block plan is an optimisation: never fail or stall the request over it
            print(f"[timing] block plan skipped: {e}", flush=True)
            blocks = []
        if not blocks:  # deterministic fallback: each part number in the request, then the request itself
            print("[timing] block plan unusable; searching part numbers from the request", flush=True)
            parts = list(dict.fromkeys(t for t in PART_TOKEN.findall(prompt) if not NOT_A_PART.match(t)))
            blocks = [{"role": "other", "need": t, "search": [t]} for t in parts][:MAX_BLOCKS - 1]
            blocks.append({"role": "other", "need": prompt, "search": [prompt]})
        return blocks[:MAX_BLOCKS]

    def blocks_and_candidates(self, prompt, deadline=None):
        """Stages 1+2, skipped when the index isn't ready or the catalog covers the request; cached per prompt."""
        if self.index is None or not self.index.ready:
            print("[timing] block plan skipped: symbol index not ready", flush=True)
            return [], []
        key = " ".join(prompt.lower().split())
        with self._cache_lock:
            if key in self._plan_cache:
                self._plan_cache.move_to_end(key)
                print("[timing] block plan: cache hit", flush=True)
                blocks, found = self._plan_cache[key]
                return [dict(b) for b in blocks], [dict(c) for c in found]
        blocks = self.catalog_blocks(prompt)
        if blocks is not None:
            print("[timing] block plan skipped: every part is in the catalog", flush=True)
            return blocks, []
        set_stage("planning blocks")
        blocks = self.plan_blocks(prompt, deadline)
        set_stage("searching KiCad library")
        with timed("symbol search"):
            found = self.find_candidates(blocks)
        fallback = bool(blocks) and blocks[-1].get("search") == [prompt]
        if not fallback:  # cache only real model plans, so a busy model is asked again next time
            with self._cache_lock:
                self._plan_cache[key] = (blocks, found)
                while len(self._plan_cache) > PLAN_CACHE_SIZE:
                    self._plan_cache.popitem(last=False)
        return [dict(b) for b in blocks], [dict(c) for c in found]

    def find_candidates(self, blocks, per_block=PER_BLOCK):
        known = {e["lib_id"] for e in self.store.catalog() + GENERIC_PARTS}
        found, seen = [], set()
        for b in blocks:
            hits = {}
            for term in [b.get("need", "")] + list(b.get("search") or []):
                for r in self.index.search(str(term), k=10):
                    if r["lib_id"] not in hits or r["score"] > hits[r["lib_id"]]["score"]:
                        hits[r["lib_id"]] = r
            picked = 0
            for r in sorted(hits.values(), key=lambda r: -r["score"]):
                if picked >= per_block:
                    break
                if r["lib_id"] in known or r["lib_id"] in seen or not self.synth.lib.has(r["lib_id"]):
                    continue
                seen.add(r["lib_id"])
                picked += 1
                found.append({"lib_id": r["lib_id"], "part_id": "", "category": b.get("role", "other"),
                              "desc": r["desc"], "block": b.get("need", "")})
        return found

    def catalog_text(self):
        lines = []
        for title, entries, limits in (
                ("Parts with datasheet data (prefer these when they fit):", self.store.catalog(), {}),
                ("Always available:", GENERIC_PARTS, {}),
                ("KiCad library parts found for this request (choose the best fit per block):", self._extra,
                 {"max_pins": CANDIDATE_MAX_PINS, "max_desc": CANDIDATE_MAX_DESC})):
            section = self._catalog_lines(entries, **limits)
            if section:
                lines += [title] + section
        if not lines:
            raise PlanningError("No usable parts: no KiCad symbols are installed or indexed. "
                                "Check that KiCad is installed and /api/health shows symbols_ready.", [])
        return "\n".join(lines)

    def _catalog_lines(self, entries, max_pins=80, max_desc=110):
        lines = []
        for e in entries:
            try:
                info = self.synth.lib.get(e["lib_id"])
            except SynthesisError:
                continue  # mapped in the DB but not in the installed KiCad libraries
            units = info.get("units") or [1]
            if len(units) > 1:
                # Multi-unit part (dual op-amp, quad gate): list each unit's pins with numbers, because names repeat
                # across units ("+" on both amplifiers). A name means the first unit; other units by number.
                groups = []
                for u in [0] + units:
                    pins_u = [p for p in info["pins"] if p.get("unit", 0) == u]
                    if pins_u:
                        tag = "shared" if u == 0 else f"unit {chr(64 + u)}"
                        groups.append(f"{tag}: " + ", ".join(
                            f"{p['number']}={p['name'] if p['name'] not in ('', '~') else p['type']}" for p in pins_u))
                label = (e.get("part_id") or e.get("block") or "generic")[:60]
                desc = f" - {e['desc'][:max_desc]}" if e.get("desc") else ""
                lines.append(f"- {e['lib_id']} ({label}){desc}. Pins by NUMBER (use the number in connections): "
                             + "; ".join(groups))
                continue
            names = []
            for p in info["pins"]:
                label = p["name"] if p["name"] not in ("", "~") else p["number"]
                if label not in names:
                    names.append(label)
            if len(names) > max_pins:  # keep every supply/ground pin, then the first signal pins
                power = [n for n in names if SUPPLY_PIN.match(n) or GROUND_PIN.match(n)]
                names = power + [n for n in names if n not in power][:max(0, max_pins - len(power))]
                names.append(f"... ({len(info['pins'])} pins in total)")
            label = (e.get("part_id") or e.get("block") or "generic")[:60]
            desc = f" - {e['desc'][:max_desc]}" if e.get("desc") else ""
            lines.append(f"- {e['lib_id']} ({label}){desc}. Pins: {', '.join(names)}")
        return lines

    def allowed_parts(self):
        return self.store.catalog() + GENERIC_PARTS + self._extra

    def normalize(self, data):
        """Repair common small-model slips deterministically before validation.
        - lib_id given as just the symbol name, library name or part number -> full 'Library:Symbol'
        - net connections that name a part (e.g. 'W25Q128JVS') instead of its reference (e.g. 'U2')
        Returns a list of notes describing what was changed (shown in the repair history)."""
        notes = []
        if not isinstance(data, dict) or not isinstance(data.get("components"), list):
            return notes
        entries = self.allowed_parts()
        nick_count = Counter(e["lib_id"].split(":")[0] for e in entries)
        alias = {}
        for e in entries:
            nick, name = e["lib_id"].split(":", 1)
            for key in (e["lib_id"], name, e.get("part_id")):
                if key:
                    alias.setdefault(key.upper(), e["lib_id"])
            if nick_count[nick] == 1:
                alias.setdefault(nick.upper(), e["lib_id"])
        valid = {e["lib_id"] for e in entries}

        by_name = {}
        for c in data["components"]:
            if not isinstance(c, dict):
                continue
            lib = str(c.get("lib_id", "")).strip()
            if lib not in valid and not (":" in lib and self.synth.lib.has(lib)):
                fixed = (alias.get(lib.upper()) or alias.get(lib.split(":")[-1].upper())
                         or alias.get(str(c.get("value", "")).upper()) or alias.get(str(c.get("part_id", "")).upper()))
                if not fixed:  # 'MCU_Microchip_ATmega328P-A': ends with an allowed symbol's name
                    tail = re.sub(r"[^A-Z0-9]", "", lib.upper())
                    ends = {e["lib_id"] for e in entries
                            if len(e["lib_id"].split(":")[-1]) >= 4
                            and tail.endswith(re.sub(r"[^A-Z0-9]", "", e["lib_id"].split(":")[-1].upper()))}
                    fixed = ends.pop() if len(ends) == 1 else None
                if not fixed and self.index is not None and self.index.ready:
                    # 'Regulator:AMS1117': the symbol exists, in another library (Regulator_Linear). Use it when
                    # exactly one installed library has a symbol with that name.
                    name = lib.split(":")[-1].lower()
                    same = sorted({self.index.entries[i]["lib_id"] for i, n in enumerate(self.index.names)
                                   if n == name})
                    if len(same) == 1 and self.synth.lib.has(same[0]):
                        fixed = same[0]
                if fixed:
                    notes.append(f"{c.get('reference')}: lib_id '{lib}' corrected to '{fixed}'")
                    c["lib_id"] = lib = fixed
            for key in (c.get("value"), c.get("part_id"), lib, lib.split(":")[-1]):
                if key:
                    by_name.setdefault(str(key).upper(), []).append(str(c.get("reference")))

        refs = {str(c.get("reference")) for c in data["components"] if isinstance(c, dict)}

        # Same net name twice is the same node (KiCad treats it that way too): merge them.
        merged, order = {}, []
        for n in [n for n in (data.get("nets") or []) if isinstance(n, dict)]:
            key = str(n.get("net", "")).strip().upper()
            if key in merged:
                merged[key].setdefault("connections", []).extend(n.get("connections") or [])
                notes.append(f"Net '{n.get('net')}' was listed twice; merged")
            else:
                merged[key] = n
                order.append(key)
        if "nets" in data:
            data["nets"] = [merged[k] for k in order]

        # [something, ref, pin] where the last two are a valid reference and pin: keep [ref, pin].
        for n in data.get("nets") or []:
            fixed = []
            for pair in n.get("connections") or []:
                if isinstance(pair, list) and len(pair) == 3 and str(pair[1]) in refs:
                    notes.append(f"Net '{n.get('net')}': connection {pair} trimmed to {pair[1:]}")
                    pair = pair[1:]
                if isinstance(pair, list) and len(pair) == 1 and str(pair[0]) not in refs:
                    # ["GND"] inside a net means "this net is GND": handled like [net, pin] below (merge)
                    notes.append(f"Net '{n.get('net')}': connection {pair} read as a tie to net '{pair[0]}'")
                    pair = [str(pair[0]), ""]
                fixed.append(pair)
            n["connections"] = fixed

        # A "connection" that names a net or a rail (e.g. ["GND", "1"]) means "this net IS that net":
        # merge the two nets instead of rejecting the design.
        nets = [n for n in (data.get("nets") or []) if isinstance(n, dict)]
        by_net = {str(n.get("net")).upper(): n for n in nets}
        merged_away = []
        for n in nets:
            conns = n.get("connections") or []
            targets = [pair for pair in conns if isinstance(pair, list) and len(pair) == 2
                       and str(pair[0]) not in refs
                       and (str(pair[0]).upper() in by_net or rail_voltage(str(pair[0])) is not None)]
            for pair in targets:
                conns.remove(pair)
                target_name = str(pair[0])
                target = by_net.get(target_name.upper())
                if target is n:
                    continue
                if target is None:
                    notes.append(f"Net '{n.get('net')}' tied to rail '{target_name}'; renamed to '{target_name}'")
                    by_net.pop(str(n.get("net")).upper(), None)
                    n["net"] = target_name
                    by_net[target_name.upper()] = n
                else:
                    notes.append(f"Net '{n.get('net')}' tied to '{target['net']}'; merged into '{target['net']}'")
                    target.setdefault("connections", []).extend(conns)
                    merged_away.append(n)
                    break
        if merged_away:
            data["nets"] = [n for n in data.get("nets") or [] if not any(n is m for m in merged_away)]

        for n in data.get("nets") or []:
            for pair in n.get("connections") or [] if isinstance(n, dict) else []:
                if isinstance(pair, list) and len(pair) == 2 and str(pair[0]) not in refs:
                    match = by_name.get(str(pair[0]).upper(), [])
                    if len(set(match)) == 1:
                        notes.append(f"Net '{n.get('net')}': '{pair[0]}' replaced by its reference '{match[0]}'")
                        pair[0] = match[0]

        # Generic power pin names the symbol doesn't have ("GND" on an STM32 whose pins are VSS, "3V3" on an
        # ESP32 module whose pin is VDD): use the symbol's own ground / supply pin.
        lib_of = {str(c.get("reference")): str(c.get("lib_id", "")) for c in data["components"] if isinstance(c, dict)}
        for n in data.get("nets") or []:
            for pair in n.get("connections") or [] if isinstance(n, dict) else []:
                if not (isinstance(pair, list) and len(pair) == 2 and str(pair[0]) in lib_of):
                    continue
                fixed = self._pin_alias(lib_of[str(pair[0])], str(pair[1]))
                if fixed:
                    notes.append(f"{pair[0]}: pin '{pair[1]}' renamed to '{fixed}' (net '{n.get('net')}')")
                    pair[1] = fixed
                elif ref_prefix(str(pair[0])) not in PASSIVE_PREFIXES and str(pair[1]).isdigit():
                    # A chip pin given by number ("2") and elsewhere by name ("VCC") is one physical pin: use the
                    # name everywhere so a short between the two is seen (and resolved) before drawing.
                    try:
                        hit = [p for p in self.synth.lib.get(lib_of[str(pair[0])])["pins"]
                               if p["number"] == str(pair[1])]
                    except SynthesisError:
                        hit = []
                    if len(hit) == 1 and hit[0]["name"] not in ("", "~") and not hit[0]["name"].isdigit():
                        pair[1] = hit[0]["name"]

        # A connection to a part that was never added (a "Barrel_Jack" feeding +5V, a connector's library name
        # used as a reference): it cannot be wired, so drop it. A rail's off-board source is represented by a
        # power flag; a signal left with one pin is completed or reported later.
        rails = data.get("rails") if isinstance(data.get("rails"), dict) else None
        lib_refs = {}
        for c in data["components"]:
            if isinstance(c, dict):
                lib_refs.setdefault(str(c.get("lib_id", "")).upper(), []).append(str(c.get("reference")))
        for n in data.get("nets") or []:
            if not isinstance(n, dict):
                continue
            kept = []
            for pair in n.get("connections") or []:
                if isinstance(pair, list) and len(pair) == 2 and str(pair[0]) not in refs:
                    same_lib = lib_refs.get(str(pair[0]).upper(), [])
                    if len(same_lib) == 1:  # the part's lib_id used instead of its reference
                        notes.append(f"Net '{n.get('net')}': '{pair[0]}' replaced by its reference '{same_lib[0]}'")
                        kept.append([same_lib[0], pair[1]])
                        continue
                    where = "the rail is supplied off-board" if rail_voltage(str(n.get("net")), rails) is not None \
                        else "it is not a component in this design"
                    notes.append(f"Net '{n.get('net')}': removed '{pair[0]}' ({where})")
                    continue
                kept.append(pair)
            n["connections"] = kept

        notes += self._move_indicator_pins(data, lib_of)
        notes += self._resolve_shorts(data, rails)

        # The renames above can make the same [ref, pin] appear twice in one net: keep it once.
        for n in data.get("nets") or []:
            if isinstance(n, dict):
                seen, unique = set(), []
                for pair in n.get("connections") or []:
                    key = tuple(map(str, pair)) if isinstance(pair, list) else id(pair)
                    if key not in seen:
                        seen.add(key)
                        unique.append(pair)
                n["connections"] = unique
        return notes

    def _move_indicator_pins(self, data, lib_of):
        """An LED/indicator net sharing a chip pin with another signal (Arduino habit: LED on D13, which is also
        SPI SCK): the other signal keeps the pin and the LED moves to a free general-purpose pin of that chip."""
        notes = []
        nets = [n for n in data.get("nets") or [] if isinstance(n, dict)]
        used = {(str(p[0]), str(p[1]).upper()) for n in nets for p in n.get("connections") or []
                if isinstance(p, list) and len(p) == 2}
        for led in [n for n in nets if INDICATOR_NET.search(str(n.get("net")))]:
            for pair in list(led.get("connections") or []):
                if not (isinstance(pair, list) and len(pair) == 2) or str(pair[0]) not in lib_of:
                    continue
                ref, pin = str(pair[0]), str(pair[1])
                clash = [n for n in nets if n is not led and any(
                    isinstance(p, list) and [str(x) for x in p] == [ref, pin] for p in n.get("connections") or [])]
                if not clash:
                    continue
                try:
                    pins = self.synth.lib.get(lib_of[ref])["pins"]
                except SynthesisError:
                    continue
                taken = {p for r, p in used if r == ref}
                free = [p["name"] for p in pins if FREE_GPIO.match(p["name"]) and p.get("type") == "bidirectional"
                        and p["name"].upper() not in taken and p["number"].upper() not in taken]
                if not free:
                    continue
                led["connections"] = [p for p in led["connections"] if p is not pair] + [[ref, free[0]]]
                used.add((ref, free[0].upper()))
                notes.append(f"{ref}.{pin} is also used by '{clash[0].get('net')}': '{led.get('net')}' moved to "
                             f"{ref}.{free[0]}")
        return notes

    @staticmethod
    def _resolve_shorts(data, rails):
        """A pin listed in two nets is a short circuit. Resolve the two unambiguous cases:
        - every net involved is a ground (GND + USB_GND, GND + SHIELD): they are one node, merge them;
        - one signal net plus rail/ground nets (R1.1 in '+5V' and 'LED_DRIVE'): the signal net is the specific
          connection, so drop the pin from the rails.
        Two signal nets, or two different supply rails, stay errors for the model to fix."""
        notes = []

        def kind(name):
            if is_ground_net(name) or re.search(r"GND|SHIELD", name, re.I):
                return "ground"
            return "rail" if rail_voltage(name, rails) is not None else "signal"

        for _ in range(100):  # each pass fixes one conflict; stop when none is resolvable
            nets = [n for n in data.get("nets") or [] if isinstance(n, dict)]
            where = {}
            for n in nets:
                for pair in n.get("connections") or []:
                    if isinstance(pair, list) and len(pair) == 2:
                        where.setdefault((str(pair[0]), str(pair[1])), [])
                        if not any(m is n for m in where[(str(pair[0]), str(pair[1]))]):
                            where[(str(pair[0]), str(pair[1]))].append(n)
            changed = False
            for (ref, pin), owners in where.items():
                if len(owners) < 2:
                    continue
                kinds = [kind(str(n.get("net"))) for n in owners]
                tails = {re.split(r"[_\-/]", str(n.get("net")).upper())[-1] for n in owners}
                same_signal = all(k == "signal" for k in kinds) and (
                    (len(tails) == 1 and tails <= BUS_SIGNALS)
                    # a net named after the very pin it holds ('PC13' next to 'LED_DRIVE') is just that pin's label
                    # (or after one of its names: XTAL1/PB6 in nets 'XTAL1' and 'PB6')
                    or (len(owners) == 2 and any(str(n.get("net")).upper() in str(pin).upper().split("/")
                                                 for n in owners)))
                if all(k == "ground" for k in kinds) or same_signal:
                    # grounds, or one bus line under two names (I2C_SCL / BME280_SCL): one node, merge them
                    keep = next((n for n in owners if is_ground_net(str(n.get("net")))),
                                next((n for n in owners if str(n.get("net")).upper() not in str(pin).upper().split("/")),
                                     owners[0]))
                    for n in owners:
                        if n is not keep:
                            keep.setdefault("connections", []).extend(n.get("connections") or [])
                            data["nets"] = [m for m in data["nets"] if m is not n]
                            notes.append(f"{ref}.{pin} was in '{keep.get('net')}' and '{n.get('net')}': "
                                         f"same node, merged into '{keep.get('net')}'")
                    changed = True
                elif kinds.count("signal") == 1 and (SUPPLY_PIN.match(pin) or GROUND_PIN.match(pin)):
                    # a power pin (DS3231 VCC in '+3V3' and 'XTAL2') belongs on its rail: drop it from the signal
                    signal = owners[kinds.index("signal")]
                    signal["connections"] = [p for p in signal.get("connections") or []
                                             if not (isinstance(p, list) and [str(x) for x in p] == [ref, pin])]
                    notes.append(f"{ref}.{pin} is a power pin: removed from signal net '{signal.get('net')}'")
                    changed = True
                elif kinds.count("signal") == 1:
                    signal = owners[kinds.index("signal")]
                    for n in owners:
                        if n is not signal:
                            n["connections"] = [p for p in n.get("connections") or []
                                                if not (isinstance(p, list) and [str(x) for x in p] == [ref, pin])]
                            notes.append(f"{ref}.{pin} was in both '{signal.get('net')}' and '{n.get('net')}': "
                                         f"kept in '{signal.get('net')}'")
                    changed = True
                if changed:
                    break
            if not changed:
                break
        return notes

    def _pin_alias(self, lib_id, pin):
        """The symbol's real pin for a name it does not have: generic power/ground names, bus names
        (SDA/SCL on a chip whose I2C pins are IO21/IO22 or SDI/SCK) and GPIO numbering (GP4 / IO4 / GPIO4)."""
        fixed = self._power_pin_alias(lib_id, pin)
        if fixed:
            return fixed
        try:
            pins = self.synth.lib.get(lib_id)["pins"]
        except SynthesisError:
            return None
        if SchematicSynthesizer._match_pins(pins, pin):
            return None
        names = {p["name"].upper(): p["name"] for p in pins}

        # KiCad formatting the model drops: DI/IO_{0} -> DI/IO_0, ~{CS} -> CS; or one name of D13/SCK.
        def canon(s):
            return re.sub(r"[~{}\s]", "", str(s)).upper()
        wanted = canon(pin)
        for match in (lambda n: canon(n) == wanted, lambda n: wanted in canon(n).split("/")):
            hits = list(dict.fromkeys(n for n in names.values() if match(n)))
            if len(hits) == 1:
                return hits[0]

        key = re.sub(r"^I2C\d?_", "", pin.upper())
        if key in ("SDA", "SCL"):
            symbol = lib_id.split(":")[-1].upper()
            for pattern, mapping in I2C_DEFAULT_PINS:
                if re.search(pattern, symbol) and mapping[key].upper() in names:
                    return names[mapping[key].upper()]
            for alt in {"SDA": ("SDI", "SDA/SDI", "DIN"), "SCL": ("SCK", "SCLK", "SCL/SCK")}[key]:
                if alt in names:
                    return names[alt]
        m = re.match(r"^(?:GPIO|GP|IO|P)?(\d{1,2})$", pin.upper())
        if m:
            hits = [n for u, n in names.items() if re.match(rf"^(?:GPIO|GP|IO){int(m.group(1))}(?:_\w+)?$", u)]
            if len(hits) == 1:
                return hits[0]
        return None

    def _power_pin_alias(self, lib_id, pin):
        """The symbol's real pin for a generic ground/supply pin name it does not have, else None."""
        try:
            pins = self.synth.lib.get(lib_id)["pins"]
        except SynthesisError:
            return None
        if SchematicSynthesizer._match_pins(pins, pin):
            return None  # the pin exists; nothing to fix
        names = list(dict.fromkeys(p["name"] for p in pins))
        if GENERIC_GROUND.match(pin):
            grounds = [n for n in names if GROUND_PIN.match(n)]
            return next((n for n in grounds if n.upper() in ("GND", "VSS")), grounds[0] if grounds else None)
        if GENERIC_SUPPLY.match(pin):
            supplies = [p["name"] for p in pins if p.get("type") == "power_in" and not GROUND_PIN.match(p["name"])
                        and not NO_AUTO_SUPPLY.match(p["name"])]
            supplies = list(dict.fromkeys(supplies)) or [n for n in names if SUPPLY_PIN.match(n)]
            main = [n for n in supplies if n.upper() in ("VDD", "VCC", "3V3", "VIN", "VI")]
            return main[0] if main else (supplies[0] if len(supplies) == 1 else None)
        return None

    def _supply_rail(self, design, ref, positive_rails, is_supply):
        """The positive rail an already-wired supply input of `ref` sits on, or None."""
        comp = design.component(ref)
        if comp is None:
            return None
        pins = self.synth.lib.get(comp.lib_id)["pins"]
        for n in design.nets:
            if n.net not in positive_rails:
                continue
            for r, pin in n.connections:
                if r == ref and any(p.get("type") != "power_out" and is_supply(p)
                                    for p in SchematicSynthesizer._match_pins(pins, str(pin))):
                    return n.net
        return None

    def _add_i2c_pullups(self, design, nets, ground_pin):
        """One 4.7k pull-up from every I2C SDA/SCL net that has none to the rail of a chip on that bus."""
        notes = []
        if not self.synth.lib.has("Device:R"):
            return notes
        pin_net = {(ref, str(pin)): n.net for n in design.nets for ref, pin in n.connections}
        positive = sorted((rail_voltage(n.net, design.rails), n.net) for n in design.nets
                          if (rail_voltage(n.net, design.rails) or 0) > 0)

        def supply_rail(ref):
            comp = design.component(ref)
            for (r, pin), net in pin_net.items():
                if r == ref and not ground_pin.match(pin) and (rail_voltage(net, design.rails) or 0) > 0:
                    pins = SchematicSynthesizer._match_pins(self.synth.lib.get(comp.lib_id)["pins"], pin)
                    if any(p.get("type") == "power_in" or SUPPLY_PIN.match(p["name"]) for p in pins):
                        return net
            return None

        def functions(n):  # pin functions from the knowledge store, e.g. STM32 PB6 -> I2C1_SCL (as the referee sees it)
            out = []
            for ref, pin in n.connections:
                comp = design.component(ref)
                part = self.store.resolve(comp.lib_id, comp.value, comp.part_id) if comp else None
                out += ((part or {}).get("facts") or {}).get("pins", {}).get(str(pin), {}).get("functions", [])
            return out

        refs = {c.reference for c in design.components}
        for n in list(design.nets):
            if not (I2C_NET.search(n.net) or any(I2C_NET.search(f) for f in functions(n))):
                continue
            has_pullup = any(ref_prefix(ref) == "R" and any(
                (rail_voltage(net, design.rails) or 0) > 0 for (r, _), net in pin_net.items()
                if r == ref and net != n.net) for ref, _ in n.connections)
            if has_pullup:
                continue
            chips = [ref for ref, _ in n.connections if ref_prefix(ref) not in PASSIVE_PREFIXES]
            rail = next((r for r in map(supply_rail, chips) if r), None)
            if rail is None:  # no chip rail found: the lowest logic rail (e.g. +3V3 before +5V)
                rail = positive[0][1] if positive else None
            if rail is None:
                continue
            i = 1
            while f"R{i}" in refs:
                i += 1
            ref = f"R{i}"
            refs.add(ref)
            design.components.append(Component(reference=ref, lib_id="Device:R", value="4.7k", block="peripheral"))
            n.connections.append([ref, "1"])
            nets[rail].connections.append([ref, "2"])
            pin_net[(ref, "1")], pin_net[(ref, "2")] = n.net, rail
            notes.append(f"Added {ref} 4.7k I2C pull-up {n.net} -> {rail}")
        return notes

    # ---------- passives the model listed but did not (fully) wire
    def _passive_pins(self, comp):
        """The terminals of a passive part (ignores mounting pads)."""
        pins = self.synth.lib.get(comp.lib_id)["pins"]
        return [p for p in pins if p["name"].upper() not in ("PAD", "EP", "SHIELD", "MP")] or pins

    def _pin_nets(self, design, ref, pin):
        """Nets holding this pin (matched by number or name) that also reach something else."""
        keys = {str(pin["number"]).upper(), str(pin["name"]).upper()}
        return [n for n in design.nets if len(n.connections) >= 2
                and any(r == ref and str(p).upper() in keys for r, p in n.connections)]

    def _open_terminals(self, design, comp):
        return [p for p in self._passive_pins(comp) if not self._pin_nets(design, comp.reference, p)]

    @staticmethod
    def _is_passive(comp):
        return ref_prefix(comp.reference) in ("R", "C", "L", "FB", "D", "LED", "Y") and not comp.lib_id.startswith(
            ("MCU", "RF_", "Sensor", "Interface", "Regulator", "Memory"))

    def _remove_component(self, design, comp):
        for n in design.nets:
            n.connections = [pair for pair in n.connections if pair[0] != comp.reference]
        design.components.remove(comp)

    def _join(self, design, nets, a, b, name):
        """Put connection pairs a and b on one new net (or a's existing net)."""
        base, i = name, 2
        while name in nets:
            name, i = f"{base}_{i}", i + 1
        net = Net(net=name, connections=[a, b])
        design.nets.append(net)
        nets[name] = net
        return net

    def _tidy_passives(self, design, nets, gnd_name):
        """Resistors, capacitors and LEDs the model listed but left (partly) unwired. KiCad's ERC does not
        notice (an unused passive pin just gets a no-connect marker), so a design could pass while parts hang in
        the air. Clear cases are completed; parts that cannot affect anything are removed; the rest is reported
        by open_passive_pins() and goes back to the model."""
        notes = []
        positive = sorted((rail_voltage(n.net, design.rails), n.net) for n in design.nets
                          if (rail_voltage(n.net, design.rails) or 0) > 0)
        has_i2c = any(I2C_NET.search(n.net) for n in design.nets)
        for c in list(design.components):
            if not self._is_passive(c) or "crystal" in c.lib_id.lower():
                continue
            terms = self._passive_pins(c)
            if len(terms) != 2:
                continue
            open_ = self._open_terminals(design, c)
            kind = "LED" if "LED" in c.lib_id.upper() else ref_prefix(c.reference)
            if len(open_) == 2:
                if kind in ("C", "R", "L", "FB"):  # wired to nothing, it does nothing: always safe to drop
                    self._remove_component(design, c)
                    notes.append(f"Removed {c.reference} ({c.value}): it was not connected to anything "
                                 f"(decoupling and I2C pull-ups are added where needed)")
                continue
            if len(open_) != 1:
                continue
            free = open_[0]
            other = next(p for p in terms if p is not free)
            joined = self._pin_nets(design, c.reference, other)[0]
            on_ground = is_ground_net(joined.net)
            on_rail = (rail_voltage(joined.net, design.rails) or 0) > 0
            if kind == "C":
                if on_ground:
                    self._remove_component(design, c)
                    notes.append(f"Removed {c.reference} ({c.value}): only one end was connected (to {joined.net})")
                else:
                    nets[gnd_name].connections.append([c.reference, free["number"]])
                    notes.append(f"{c.reference} other end connected to {gnd_name}")
            elif kind == "R":
                if I2C_NET.search(joined.net) and positive:
                    nets[positive[0][1]].connections.append([c.reference, free["number"]])
                    notes.append(f"{c.reference} completed as I2C pull-up {joined.net} -> {positive[0][1]}")
                elif on_ground or on_rail:
                    self._remove_component(design, c)
                    notes.append(f"Removed {c.reference} ({c.value}): only one end was connected (to {joined.net})")
                else:  # GPIO -> R -> LED: join the resistor to an LED whose anode is still open
                    leds = [d for d in design.components if "LED" in d.lib_id.upper() and self._is_passive(d)
                            and any(p["name"].upper() == "A" for p in self._open_terminals(design, d))]
                    if len(leds) >= 1:
                        led = leds[0]
                        anode = next(p for p in self._passive_pins(led) if p["name"].upper() == "A")
                        self._join(design, nets, [c.reference, free["number"]], [led.reference, anode["number"]],
                                   f"{led.reference}_A")
                        notes.append(f"{c.reference} connected to {led.reference} anode (LED drive)")
            elif kind == "LED":
                if free["name"].upper() == "K" and not on_ground:
                    nets[gnd_name].connections.append([c.reference, free["number"]])
                    notes.append(f"{c.reference} cathode connected to {gnd_name}")
        # LEDs still completely unwired, next to a resistor whose far end is still open: the usual LED circuit
        for led in [d for d in design.components if "LED" in d.lib_id.upper() and self._is_passive(d)]:
            if len(self._open_terminals(design, led)) != 2:
                continue
            for r in [d for d in design.components if ref_prefix(d.reference) == "R"]:
                r_open = self._open_terminals(design, r)
                if len(r_open) == 1:
                    pins = {p["name"].upper(): p for p in self._passive_pins(led)}
                    if "A" in pins and "K" in pins:
                        self._join(design, nets, [r.reference, r_open[0]["number"]], [led.reference, pins["A"]["number"]],
                                   f"{led.reference}_A")
                        nets[gnd_name].connections.append([led.reference, pins["K"]["number"]])
                        notes.append(f"{led.reference} wired as {r.reference} -> LED -> {gnd_name}")
                    break
        # LEDs still completely unwired with no resistor to use: the standard indicator circuit,
        # free GPIO -> 1k -> LED -> GND, on the main controller
        for led in [d for d in design.components if "LED" in d.lib_id.upper() and self._is_passive(d)]:
            pins = {p["name"].upper(): p for p in self._passive_pins(led)}
            if len(self._open_terminals(design, led)) != 2 or not {"A", "K"} <= set(pins):
                continue
            gpio = self._free_gpio(design)
            if gpio is None or not self.synth.lib.has("Device:R"):
                continue
            mcu, pin = gpio
            refs = {x.reference for x in design.components}
            i = 1
            while f"R{i}" in refs:
                i += 1
            r = f"R{i}"
            design.components.append(Component(reference=r, lib_id="Device:R", value="1k", block="peripheral"))
            self._join(design, nets, [mcu, pin], [r, "1"], f"{led.reference}_DRIVE")
            self._join(design, nets, [r, "2"], [led.reference, pins["A"]["number"]], f"{led.reference}_A")
            nets[gnd_name].connections.append([led.reference, pins["K"]["number"]])
            notes.append(f"{led.reference} wired as {mcu}.{pin} -> {r} 1k -> LED -> {gnd_name} (status LED)")
        return notes

    def _free_gpio(self, design):
        """(reference, pin) of an unused general-purpose pin on the main controller, or None."""
        for c in design.components:
            if not c.lib_id.startswith(("MCU", "RF_Module")):
                continue
            used = {str(p).upper() for n in design.nets for r, p in n.connections if r == c.reference}
            for p in self.synth.lib.get(c.lib_id)["pins"]:
                if FREE_GPIO.match(p["name"]) and p.get("type") == "bidirectional" \
                        and p["name"].upper() not in used and p["number"] not in used:
                    return c.reference, p["name"]
        return None

    def open_passive_pins(self, design):
        """Passive parts with an unconnected terminal after completion: a design is not accepted with them."""
        out = []
        for c in design.components:
            if not self._is_passive(c):
                continue
            for p in self._open_terminals(design, c):
                out.append(f"[dangling-part] {c.reference} ({c.value or c.lib_id}) pin {p['number']}"
                           f"{' (' + p['name'] + ')' if p['name'] not in ('', '~', p['number']) else ''} is not "
                           f"connected to anything. Fix: connect it where it belongs, or remove {c.reference}.")
        return out

    # ---------- a crystal the request asked for
    def _add_requested_crystal(self, design, nets, gnd_name):
        """The request names a crystal (or a clock in MHz) but the design has none: add it between the MCU's
        oscillator pins, with a 22pF load capacitor from each side to ground."""
        notes = []
        prompt = getattr(self, "_prompt", "") or ""
        if not re.search(r"crystal|\bxtal\b|\d\s*MHz", prompt, re.I) or not self.synth.lib.has("Device:Crystal"):
            return notes
        if any("crystal" in c.lib_id.lower() or ref_prefix(c.reference) == "Y" for c in design.components):
            return notes
        for c in design.components:
            pins = self.synth.lib.get(c.lib_id)["pins"]
            names = [p["name"] for p in pins]
            pair = None
            for a, b in OSC_PIN_PAIRS:
                pa = next((n for n in names if re.match(a, n, re.I)), None)
                pb = next((n for n in names if re.match(b, n, re.I)), None)
                if pa and pb:
                    pair = (pa, pb)
                    break
            if pair is None and c.lib_id.startswith("MCU_ST_STM32") and {"PD0", "PD1"} <= set(names):
                pair = ("PD0", "PD1")  # OSC_IN / OSC_OUT on these STM32 packages
            if pair is None:
                continue
            m = re.search(r"(\d+(?:\.\d+)?)\s*MHz", prompt, re.I)
            refs = {x.reference for x in design.components}

            def next_ref(prefix):
                i = 1
                while f"{prefix}{i}" in refs:
                    i += 1
                refs.add(f"{prefix}{i}")
                return f"{prefix}{i}"

            y = next_ref("Y")
            design.components.append(Component(reference=y, lib_id="Device:Crystal",
                                               value=f"{m.group(1)}MHz" if m else "Crystal", block="mcu"))
            for side, (pin, ypin) in enumerate(zip(pair, ("1", "2")), 1):
                net = next((n for n in design.nets for r, p in n.connections if r == c.reference and p == pin), None)
                if net is None:
                    net = Net(net=f"XTAL{side}", connections=[[c.reference, pin]])
                    design.nets.append(net)
                    nets[net.net] = net
                net.connections.append([y, ypin])
                cap = next_ref("C")
                design.components.append(Component(reference=cap, lib_id="Device:C", value="22pF", block="mcu"))
                net.connections.append([cap, "1"])
                nets[gnd_name].connections.append([cap, "2"])
            notes.append(f"Added {y} {m.group(1) + 'MHz' if m else ''} crystal on {c.reference} {pair[0]}/{pair[1]} "
                         f"with two 22pF load capacitors (the request asked for a crystal)")
            break
        return notes

    def _remove_redundant_regulators(self, design):
        """A rail driven both by a module's own regulator output (a Pico's 3V3) and by a separate regulator
        (an AMS1117's VO) is two sources fighting (ERC: power output to power output). The module already
        makes that voltage, so the separate regulator is redundant: remove it (noted in the history)."""
        notes = []
        for n in list(design.nets):
            if (rail_voltage(n.net, design.rails) or 0) <= 0:
                continue
            drivers = [(ref, pin) for ref, pin in n.connections if self._pin_type(design, ref, pin) == "power_out"]
            regs = {ref for ref, _ in drivers if design.component(ref).lib_id.startswith("Regulator")}
            others = {ref for ref, _ in drivers} - regs
            if not regs or not others:
                continue
            for ref in regs:
                comp = design.component(ref)
                for m in design.nets:
                    m.connections = [pair for pair in m.connections if pair[0] != ref]
                design.components.remove(comp)
                notes.append(f"Removed {ref} ({comp.value or comp.lib_id}): {sorted(others)[0]} already supplies "
                             f"{n.net} from its own regulator")
        return notes

    def _pin_type(self, design, ref, pin):
        comp = design.component(ref)
        if comp is None:
            return None
        matched = SchematicSynthesizer._match_pins(self.synth.lib.get(comp.lib_id)["pins"], str(pin))
        types = [p.get("type") for p in matched]
        # one name can cover several pins (a Pico's seven GNDs, one of them typed as an output): strongest wins
        return next((t for t in ("power_out", "power_in") if t in types), types[0] if types else None)

    def _resolve_floating_supply_nets(self, design, nets):
        """A net with no voltage in its name that only feeds power inputs ('VDD_U2' = MCP2551.VDD + AMS1117.VI,
        'VBAT' = DS3231.VBAT alone) has no source, so KiCad reports its pins as not driven. Merge it into the one
        rail that suits every part on it; if none does and it feeds a regulator input, into the regulator's
        input rail (+5V, or +12V for a 5 V regulator). Otherwise free its supply pins, so the normal power
        completion wires them."""
        notes = []
        for n in list(design.nets):
            if rail_voltage(n.net, design.rails) is not None or is_ground_net(n.net) or not n.connections:
                continue
            chips = [(ref, pin) for ref, pin in n.connections if ref_prefix(ref) not in PASSIVE_PREFIXES]
            types = [self._pin_type(design, ref, pin) for ref, pin in chips]
            if not chips or any(t != "power_in" for t in types) or GROUND_PIN.match(str(chips[0][1])):
                continue
            if any(SOURCE_LIB_NAMES.search(design.component(ref).lib_id) for ref, _ in n.connections
                   if design.component(ref) is not None):
                continue  # fed by a connector or battery: a real (named) supply, the generator flags it
            rails = [r.net for r in design.nets if (rail_voltage(r.net, design.rails) or 0) > 0]
            fit = []
            for r in rails:
                v = rail_voltage(r, design.rails)
                ok = True
                for ref, _ in chips:
                    comp = design.component(ref)
                    facts = (self.store.resolve(comp.lib_id, comp.value, comp.part_id) or {}).get("facts") or {}
                    driven_by_it = any(r2 == ref and self._pin_type(design, r2, p2) == "power_out"
                                       for m in design.nets if m.net == r for r2, p2 in m.connections)
                    if driven_by_it or ("vdd_min" in facts and not facts["vdd_min"] <= v <= facts.get("vdd_max", v)):
                        ok = False
                fit.append(r) if ok else None
            regulator_input = any(re.match(r"^(VI|VIN|IN|VREG_IN)$", str(pin), re.I) and any(
                self._pin_type(design, ref, p["number"]) == "power_out"
                for p in self.synth.lib.get(design.component(ref).lib_id)["pins"]) for ref, pin in chips)
            target = fit[0] if len(fit) == 1 else None
            if target is None and not fit and regulator_input:
                target = "+5V"
                if target not in nets:
                    nets[target] = Net(net=target, connections=[])
                    design.nets.append(nets[target])
                    if isinstance(design.rails, dict):
                        design.rails.setdefault(target, 5.0)
            if target:
                nets[target].connections.extend(n.connections)
                design.nets.remove(n)
                nets.pop(n.net, None)
                notes.append(f"Net '{n.net}' only fed power inputs with no source; merged into {target}")
            else:
                n.connections = [pair for pair in n.connections if pair not in [list(c) for c in chips]]
                notes.append(f"Net '{n.net}' had no source; its supply pins are wired to a rail instead")
        return notes

    def _connect_regulator_outputs(self, design, nets):
        """A regulator whose main output (VO/VOUT) is open drives the one rail that has no source yet and matches
        its voltage (from the symbol name, e.g. AMS1117-3.3 -> 3.3 V; else the only undriven rail)."""
        notes = []
        driven = set()
        for n in design.nets:
            for ref, pin in n.connections:
                comp = design.component(ref)
                if comp is not None and any(p.get("type") == "power_out" for p in SchematicSynthesizer._match_pins(
                        self.synth.lib.get(comp.lib_id)["pins"], str(pin))):
                    driven.add(n.net)
        for c in design.components:
            pins = self.synth.lib.get(c.lib_id)["pins"]
            used = {str(pin).upper() for n in design.nets for ref, pin in n.connections if ref == c.reference}
            outputs = [p for p in pins if p.get("type") == "power_out" and MAIN_POWER_OUT.match(p["name"])
                       and p["number"].upper() not in used and p["name"].upper() not in used]
            if not outputs:
                continue
            m = re.search(r"[-_](\d+)[.V](\d+)$|[-_](\d+(?:\.\d+)?)V?$", c.lib_id.split(":")[-1], re.I)
            volts = None
            if m:
                volts = float(f"{m.group(1)}.{m.group(2)}") if m.group(1) else float(m.group(3))
            candidates = [n.net for n in design.nets if n.net not in driven
                          and (rail_voltage(n.net, design.rails) or 0) > 0
                          and (volts is None or abs(rail_voltage(n.net, design.rails) - volts) < 0.05)]
            if len(candidates) == 1:
                nets[candidates[0]].connections.append([c.reference, outputs[0]["name"]])
                driven.add(candidates[0])
                notes.append(f"{c.reference}.{outputs[0]['name']} connected to {candidates[0]} (regulator output)")
        return notes

    def _fix_supply_voltages(self, design, nets, ground_pin):
        """A supply pin on a rail outside the part's allowed range (an MPU-6050 on +5V) moves to the one rail
        in the design that fits, when there is exactly one. Ranges come from the knowledge store."""
        notes = []
        positive = [n.net for n in design.nets if (rail_voltage(n.net, design.rails) or 0) > 0]
        for c in design.components:
            facts = (self.store.resolve(c.lib_id, c.value, c.part_id) or {}).get("facts") or {}
            if "vdd_min" not in facts or "vdd_max" not in facts:
                continue
            lo, hi = facts["vdd_min"], facts["vdd_max"]
            pins = self.synth.lib.get(c.lib_id)["pins"]
            outputs = {n.net for n in design.nets for ref, pin in n.connections if ref == c.reference and any(
                p.get("type") == "power_out" for p in SchematicSynthesizer._match_pins(pins, str(pin)))}
            fitting = [r for r in positive if r not in outputs and lo <= rail_voltage(r, design.rails) <= hi]
            if not fitting:
                # no rail suits it (an STM32 on +5V, no 3.3 V rail): add the standard one it needs, supplied
                # off-board, then move it there
                v_std, name_std = next(((v, n) for v, n in ((3.3, "+3V3"), (5.0, "+5V"), (12.0, "+12V"))
                                        if lo <= v <= hi), (None, None))
                if name_std is None or name_std in nets:
                    continue
                nets[name_std] = Net(net=name_std, connections=[])
                design.nets.append(nets[name_std])
                if isinstance(design.rails, dict):
                    design.rails.setdefault(name_std, v_std)
                positive.append(name_std)
                fitting = [name_std]
                notes.append(f"Created {name_std} rail for {c.reference} ({c.value or c.lib_id} allows {lo:g}-{hi:g} V)")
            if len(fitting) != 1:
                continue
            for n in design.nets:
                if n.net not in positive or lo <= rail_voltage(n.net, design.rails) <= hi:
                    continue
                for pair in list(n.connections):
                    ref, pin = pair
                    if ref != c.reference or ground_pin.match(str(pin)):
                        continue
                    matched = SchematicSynthesizer._match_pins(pins, str(pin))
                    if any(p.get("type") == "power_in" or SUPPLY_PIN.match(p["name"]) for p in matched):
                        n.connections.remove(pair)
                        nets[fitting[0]].connections.append(pair)
                        notes.append(f"{ref}.{pin} moved from {n.net} to {fitting[0]} "
                                     f"({c.value or c.lib_id} allows {lo:g}-{hi:g} V)")
        return notes

    def _battery_net(self, design, nets, gnd_name):
        """The net on a backup battery's + terminal (created if the battery is not wired yet), or None."""
        bat = next((c for c in design.components
                    if "battery" in c.lib_id.lower() or ref_prefix(c.reference) == "BT"), None)
        if bat is None:
            return None
        pins = {p["name"]: p["number"] for p in self.synth.lib.get(bat.lib_id)["pins"]}
        plus, minus = ("+", "-") if "+" in pins else ("1", "2")
        for n in design.nets:
            if any(r == bat.reference and str(p) in (plus, pins.get(plus)) for r, p in n.connections):
                return n if (rail_voltage(n.net, design.rails) or 1) > 0 else None
        net = Net(net="VBAT_BACKUP", connections=[[bat.reference, plus]])
        design.nets.append(net)
        nets[net.net] = net
        if not any(r == bat.reference and str(p) in (minus, pins.get(minus)) for n in design.nets for r, p in n.connections):
            nets[gnd_name].connections.append([bat.reference, minus])
        return net

    def _complete_signals(self, design, nets):
        """Finish signal nets that reach only one pin, which KiCad's ERC rejects:
        - a crystal net (XTAL/OSC/XIN/XOUT) -> the crystal's free terminal
        - signals that leave the board (CAN, RS-485, USB data, UART, SWD...) -> one header carrying them all"""
        notes = []
        used = {(ref, str(pin)) for n in design.nets for ref, pin in n.connections}
        single = [n for n in design.nets if len(n.connections) == 1
                  and rail_voltage(n.net, design.rails) is None and not is_ground_net(n.net)]
        offboard = []
        for n in single:
            if CRYSTAL_NET.search(n.net):
                for y in (c for c in design.components if "crystal" in c.lib_id.lower()):
                    m = re.search(r"GND(\d)(\d)", y.lib_id)
                    gnd_pins = set(m.groups()) if m else set()
                    free = [p["number"] for p in self.synth.lib.get(y.lib_id)["pins"]
                            if p["number"] not in gnd_pins and (y.reference, p["number"]) not in used]
                    if free:
                        n.connections.append([y.reference, free[0]])
                        used.add((y.reference, free[0]))
                        notes.append(f"{y.reference} pin {free[0]} connected to '{n.net}' (crystal terminal)")
                        break
            elif OFFBOARD_NET.search(n.net):
                offboard.append(n)
        if offboard:
            lib_id = f"Connector_Generic:Conn_01x{len(offboard):02d}"
            if self.synth.lib.has(lib_id):
                refs = {c.reference for c in design.components}
                i = 1
                while f"J{i}" in refs:
                    i += 1
                ref = f"J{i}"
                design.components.append(Component(reference=ref, lib_id=lib_id, value="Off-board",
                                                   block="peripheral"))
                for k, n in enumerate(offboard, 1):
                    n.connections.append([ref, str(k)])
                notes.append(f"Added {ref} header for off-board signals: {', '.join(n.net for n in offboard)}")
        return notes

    def complete_power(self, design):
        """Deterministically finish standard power wiring the model often forgets:
        - unconnected ground pins (VSS, VSSA, GND...) -> the ground net
        - unconnected supply pins (VDD*, VCC*, VBAT...) -> the rail the IC's other supply pins already use
        - one 100nF capacitor per physical supply pin on each rail
        Every change is returned as a note and shown in the repair history."""
        notes = []
        ground_pin = re.compile(r"^(\w*GND\w*|V\w*SS\w*|0V)$", re.I)   # GND, AGND, VSS, VSSA, VLSS ...
        supply_pin = re.compile(r"^(VDD\w*|VCC\w*|AVDD|DVDD|VDDIO|VBAT)$", re.I)
        # Names that clearly mean "positive supply": only these are wired to a rail automatically. Any other
        # unconnected power input (VEE, VPP, odd vendor names) is sent back to the model instead of guessed.
        positive_supply = re.compile(r"^(A?VDD\w*|A?VCC\w*|DVDD|IOVDD|VLOGIC|VBAT|VIN|VI|VSYS|VIO\w*|VS|V\+|"
                                     r"\+?\d+V\d*)$", re.I)
        # Analog reference / analog supply inputs: tied to the lowest rail the part already uses (a Pico's
        # ADC_VREF goes on its own 3.3 V, never on the 5 V that feeds VSYS).
        reference_supply = re.compile(r"^(ADC_VREF|ADC_AVDD|VREF\+?|AREF_IN)$", re.I)
        nets = {n.net: n for n in design.nets}
        gnd_name = next((n.net for n in design.nets if is_ground_net(n.net)), "GND")
        if gnd_name not in nets:
            nets[gnd_name] = Net(net=gnd_name, connections=[])
            design.nets.append(nets[gnd_name])

        # A chip with nothing but ground connected (a regulator the model unplugged instead of deleting) does
        # nothing and only produces ERC errors: remove it.
        for c in list(design.components):
            if ref_prefix(c.reference) in PASSIVE_PREFIXES:
                continue
            mine = [(n, pin) for n in design.nets for ref, pin in n.connections if ref == c.reference]
            if all(is_ground_net(n.net) or GROUND_PIN.match(str(pin)) for n, pin in mine):
                for n, _ in mine:
                    n.connections = [pair for pair in n.connections if pair[0] != c.reference]
                design.components.remove(c)
                notes.append(f"Removed {c.reference} ({c.value or c.lib_id}): nothing but ground was connected to it")

        def outputs_on(net):
            return any(self._pin_type(design, ref, pin) == "power_out" for ref, pin in net.connections)

        # Extra ground nets ('AGND', 'GND_CAN', 'CAN_GND', 'VSS_U3') are the same ground on these boards, unless
        # merging would join two power outputs (a Pico's GND and AGND pins are both "outputs" in its symbol).
        for n in list(design.nets):
            if n.net != gnd_name and (is_ground_net(n.net) or re.search(r"GND|^VSS|_VSS", n.net, re.I)) \
                    and rail_voltage(n.net, design.rails) in (None, 0) \
                    and not (outputs_on(n) and outputs_on(nets[gnd_name])):
                nets[gnd_name].connections.extend(n.connections)
                design.nets.remove(n)
                nets.pop(n.net, None)
                notes.append(f"Net '{n.net}' merged into {gnd_name} (ground)")

        notes += self._remove_redundant_regulators(design)

        # A chip's ground pin wired into a signal net (e.g. an OLED's GND on 'LED_A') belongs on ground.
        for n in design.nets:
            if is_ground_net(n.net):
                continue
            keep = []
            for ref, pin in n.connections:
                comp = design.component(ref)
                if comp is not None and GROUND_PIN.match(str(pin)) and ref_prefix(ref) not in PASSIVE_PREFIXES:
                    nets[gnd_name].connections.append([ref, pin])
                    notes.append(f"{ref}.{pin} moved from '{n.net}' to {gnd_name}")
                else:
                    keep.append([ref, pin])
            n.connections = keep

        notes += self._resolve_floating_supply_nets(design, nets)

        positive_rails = [n.net for n in design.nets if (rail_voltage(n.net, design.rails) or 0) > 0]
        for c in design.components:
            pins = self.synth.lib.get(c.lib_id)["pins"]
            has_power_in = any(p.get("type") == "power_in" for p in pins)
            if not has_power_in and c.reference[:1].upper() != "U" and not c.reference.upper().startswith("IC"):
                continue

            def is_supply(p):  # KiCad's pin type is authoritative (AVCC, VI, VDDA...); the name list covers the rest
                return not ground_pin.match(p["name"]) and (p.get("type") == "power_in" or supply_pin.match(p["name"]))

            # Which rail do this part's supply inputs already use, and which rails does it drive itself?
            rail, own_outputs = None, set()
            for n in design.nets:
                for ref, pin in n.connections:
                    if ref != c.reference:
                        continue
                    for p in SchematicSynthesizer._match_pins(pins, str(pin)):
                        if p.get("type") == "power_out":
                            own_outputs.add(n.net)
                        elif rail is None and is_supply(p) and n.net in positive_rails:
                            rail = n.net
            # The part's allowed supply range, when the knowledge store has one: never pick a rail outside it.
            facts = (self.store.resolve(c.lib_id, c.value, c.part_id) or {}).get("facts") or {}

            def fits(r):
                v = rail_voltage(r, design.rails) or 0
                return "vdd_min" not in facts or facts["vdd_min"] <= v <= facts.get("vdd_max", v)

            if rail is None:  # nothing to copy: only guess when exactly one rail could be this part's supply
                options = [r for r in positive_rails if r not in own_outputs and fits(r)]
                rail = options[0] if len(options) == 1 else None
            if rail is None and own_outputs and not [r for r in positive_rails if r not in own_outputs]:
                # A regulator whose input has nowhere to go (the design only has the rail it makes): give it the
                # usual input rail, +5V (or +12V for a 5 V regulator), supplied off-board through a power flag.
                v_out = max(rail_voltage(r, design.rails) or 0 for r in own_outputs)
                name_in, v_in = ("+5V", 5.0) if v_out < 4.5 else ("+12V", 12.0)
                if name_in not in nets:
                    nets[name_in] = Net(net=name_in, connections=[])
                    design.nets.append(nets[name_in])
                    notes.append(f"Created {name_in} input rail for {c.reference} (supplied off-board)")
                if isinstance(design.rails, dict):
                    design.rails.setdefault(name_in, v_in)
                positive_rails.append(name_in)
                rail = name_in
            if rail is None:
                # Several rails: a peripheral runs from the rail of the chips it shares signals with (a BME280 on
                # the ESP32's I2C lines is on the ESP32's +3V3). Only when all of those chips agree.
                neighbour_rails = set()
                for n in design.nets:
                    if rail_voltage(n.net, design.rails) is not None or is_ground_net(n.net):
                        continue
                    refs_here = {ref for ref, _ in n.connections}
                    if c.reference not in refs_here:
                        continue
                    for other in refs_here - {c.reference}:
                        if ref_prefix(other) not in PASSIVE_PREFIXES:
                            r = self._supply_rail(design, other, positive_rails, is_supply)
                            if r:
                                neighbour_rails.add(r)
                if len(neighbour_rails) == 1 and fits(next(iter(neighbour_rails))):
                    rail = neighbour_rails.pop()
            if rail is None and "vdd_min" in facts and not any(fits(r) for r in positive_rails if r not in own_outputs):
                # A known part whose range fits no rail in the design (an MCP2551 needs 5 V; only +3V3 exists):
                # add the standard rail it needs, supplied off-board through a power flag.
                for v_std, name_std in ((3.3, "+3V3"), (5.0, "+5V"), (12.0, "+12V")):
                    if facts["vdd_min"] <= v_std <= facts.get("vdd_max", v_std):
                        if name_std not in nets:
                            nets[name_std] = Net(net=name_std, connections=[])
                            design.nets.append(nets[name_std])
                            notes.append(f"Created {name_std} rail for {c.reference} "
                                         f"({c.value or c.lib_id} needs {facts['vdd_min']:g}-{facts['vdd_max']:g} V)")
                        if isinstance(design.rails, dict):
                            design.rails.setdefault(name_std, v_std)
                        positive_rails.append(name_std)
                        rail = name_std
                        break

            used = {pin for n in design.nets for ref, pin in n.connections if ref == c.reference}
            used_up = {u.upper() for u in used}
            done = set()
            for p in pins:
                name = p["name"]
                if p["number"] in used or name.upper() in used_up or name in done:
                    continue
                if p.get("type") not in ("power_in", "passive", "") and not (
                        ground_pin.match(name) and p.get("type") == "power_out"):  # a module's GND/AGND "outputs"
                    continue
                if re.match(r"^(V-|VEE|VNEG)$", name, re.I) and p.get("type") == "power_in" and not any(
                        (rail_voltage(n.net, design.rails) or 0) < 0 for n in design.nets):
                    # single-supply design (no negative rail): an op-amp's V- goes to ground
                    nets[gnd_name].connections.append([c.reference, name])
                    notes.append(f"{c.reference}.{name} connected to {gnd_name} (single supply)")
                    done.add(name)
                    continue
                if ground_pin.match(name) and p.get("type") == "power_out" and outputs_on(nets[gnd_name]):
                    # a second ground "output" (Pico AGND) gets its own analog-ground net instead of a conflict
                    own = nets.get(name.upper()) or Net(net=name.upper(), connections=[])
                    if own.net not in nets:
                        nets[own.net] = own
                        design.nets.append(own)
                    own.connections.append([c.reference, name])
                    notes.append(f"{c.reference}.{name} on its own ground net {own.net}")
                    done.add(name)
                elif ground_pin.match(name):
                    nets[gnd_name].connections.append([c.reference, name])
                    notes.append(f"{c.reference}.{name} connected to {gnd_name}")
                    done.add(name)
                elif re.match(r"^VBAT", name, re.I) and self._battery_net(design, nets, gnd_name):
                    # A backup-battery input belongs on the battery (an RTC's coin cell), not on the main rail.
                    bat = self._battery_net(design, nets, gnd_name)
                    bat.connections.append([c.reference, name])
                    notes.append(f"{c.reference}.{name} connected to the backup battery ({bat.net})")
                    done.add(name)
                elif reference_supply.match(name):
                    touched = [n.net for n in design.nets if n.net in positive_rails
                               and any(r == c.reference for r, _ in n.connections)]
                    low = min(touched, key=lambda r: rail_voltage(r, design.rails), default=None)
                    if low:
                        nets[low].connections.append([c.reference, name])
                        notes.append(f"{c.reference}.{name} connected to {low} (reference input, lowest rail)")
                        done.add(name)
                elif positive_supply.match(name) and rail and not NO_AUTO_SUPPLY.match(name):
                    nets[rail].connections.append([c.reference, name])
                    notes.append(f"{c.reference}.{name} connected to {rail}")
                    done.add(name)

        notes += self._connect_regulator_outputs(design, nets)
        notes += self._fix_supply_voltages(design, nets, ground_pin)
        notes += self._add_i2c_pullups(design, nets, ground_pin)
        notes += self._complete_signals(design, nets)
        notes += self._add_requested_crystal(design, nets, gnd_name)
        notes += self._tidy_passives(design, nets, gnd_name)

        # decoupling: one 100nF per physical supply pin per rail
        if self.synth.lib.has("Device:C"):
            refs = {c.reference for c in design.components}
            for net in list(nets.values()):
                v = rail_voltage(net.net, design.rails)
                if not v or v <= 0:
                    continue
                need = 0
                for ref, pin in net.connections:
                    comp = design.component(ref)
                    if comp is None or not (ref[:1].upper() == "U" and SUPPLY_PIN.match(pin)):
                        continue
                    need += len({p["number"] for p in self.synth.lib.get(comp.lib_id)["pins"]
                                 if pin in (p["number"], p["name"])}) or 1
                have = sum(1 for ref, pin in net.connections if ref[:1].upper() == "C"
                           and any(r == ref for r, _ in nets[gnd_name].connections))
                for _ in range(max(0, need - have)):
                    i = 1
                    while f"C{i}" in refs:
                        i += 1
                    ref = f"C{i}"
                    refs.add(ref)
                    design.components.append(Component(reference=ref, lib_id="Device:C", value="100nF", block="mcu"))
                    net.connections.append([ref, "1"])
                    nets[gnd_name].connections.append([ref, "2"])
                    notes.append(f"Added {ref} 100nF decoupling {net.net} -> {gnd_name}")
        return notes

    def open_power_pins(self, design):
        """Power-input pins (by KiCad pin type) still unconnected after complete_power: KiCad's ERC fails these."""
        comps, nets = design.generator_inputs()
        rails = ", ".join(n.net for n in design.nets if rail_voltage(n.net, design.rails) is not None)
        out, seen = [], set()
        for ref, number, name in self.synth.unconnected_pins(comps, nets):
            comp = design.component(ref)
            pin = next((p for p in self.synth.lib.get(comp.lib_id)["pins"] if p["number"] == number), None)
            if pin is None or pin.get("type") != "power_in" or (ref, name) in seen:
                continue
            seen.add((ref, name))
            out.append(f"[power-pins] {ref}.{name} (pin {number}) is a power input and is not connected. "
                       f"Fix: add it to the net of the rail that supplies {ref} (rails in this design: {rails}).")
        return out

    def library_errors(self, design):
        errors = []
        for c in design.components:
            try:
                self.synth.lib.get(c.lib_id)
            except SynthesisError as e:
                errors.append(f"{c.reference}: {e}")
        if errors:
            return errors
        for n in design.nets:
            for ref, pin in n.connections:
                comp = design.component(ref)
                pins = self.synth.lib.get(comp.lib_id)["pins"]
                if not SchematicSynthesizer._match_pins(pins, pin):
                    avail = sorted({p["name"] if p["name"] not in ("", "~") else p["number"] for p in pins})
                    errors.append(f"Net '{n.net}': {ref} ({comp.lib_id}) has no pin '{pin}'. "
                                  f"Valid pins: {', '.join(avail[:60])}")
        return errors

    def run(self, prompt, max_repair_rounds=3, budget_s=None):
        deadline = time.monotonic() + (GEN_BUDGET_S if budget_s is None else budget_s)
        self._prompt = prompt  # completion steps check what was asked for (e.g. a crystal)
        self._extra, self.blocks = [], []
        self.blocks, self._extra = self.blocks_and_candidates(prompt, deadline)
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"Allowed parts:\n{self.catalog_text()}\n\n"
                                                f"Design request:\n{prompt}"}]
        history, last_good, out_of_time = [], None, False
        for round_no in range(1, max_repair_rounds + 2):
            left = deadline - time.monotonic()
            if round_no > 1 and left < MIN_ROUND_S:
                print(f"[timing] budget used up before round {round_no} ({left:.0f}s left); stopping repairs", flush=True)
                out_of_time = True
                break
            set_stage(f"designing netlist, round {round_no}")
            try:
                raw = self.llm.chat(messages, label=f"netlist round {round_no}", deadline=deadline,
                                    max_tokens=NETLIST_MAX_TOKENS)
            except LLMBudgetError as e:
                print(f"[timing] budget used up during round {round_no}: {e}", flush=True)
                out_of_time = True
                break
            except LLMError as e:
                if last_good is None:
                    # No design yet: while the budget allows, ask again (the next round) instead of failing the job.
                    if deadline - time.monotonic() >= MIN_ROUND_S and round_no <= max_repair_rounds:
                        print(f"[timing] model unavailable in round {round_no} ({e}); asking again", flush=True)
                        history.append({"round": round_no, "auto_fixes": [], "schema_errors": [f"Model unavailable: {e}"],
                                        "violations": [], "status": "rejected"})
                        continue
                    raise
                # The model became unreachable mid-repair: keep the best design instead of losing it.
                print(f"[timing] model unavailable in round {round_no} ({e}); returning the best design so far",
                      flush=True)
                break
            set_stage("checking")
            check_started = time.perf_counter()
            design, errors, fails, findings, data, erc_errors = None, [], [], [], None, []
            notes = []
            try:
                data = extract_json(raw)
                notes = self.normalize(data)
                design = parse_design(data)
                errors = self.library_errors(design)
            except DesignError as e:
                errors = e.errors
            except ValueError as e:
                errors = [str(e)]

            if design and not errors:
                notes += self.complete_power(design)
                comps, nets = design.generator_inputs()
                findings = self.referee.audit(design, log_mistakes=False,
                                              unconnected_pins=self.synth.unconnected_pins(comps, nets),
                                              symbol_pins=self.synth.symbol_pins(comps))
                fails = [f for f in findings if f.status == FAIL]
                # KiCad's ERC must pass too: open power inputs first (cheap), then the real ERC on a draft schematic.
                erc_errors = self.open_power_pins(design) + self.open_passive_pins(design)
                if not fails and not erc_errors and self.erc_check is not None:
                    with timed(f"kicad-cli ERC check round {round_no}"):
                        erc_errors = self.erc_check(design) or []
                if last_good is None or len(fails) + len(erc_errors) <= last_good[2]:  # fewest problems wins
                    last_good = (design, findings, len(fails) + len(erc_errors))
            print(f"[timing] referee check round {round_no}: {time.perf_counter() - check_started:.2f}s", flush=True)

            if design and not errors:
                # Ask for missing decoupling while repair rounds remain, but don't block on it.
                nudges = [f for f in findings if f.status == WARN and f.rule == "decoupling"]
                same_as_before = bool(history) and history[-1].get("violations") == [f.message for f in nudges]
                if not fails and not erc_errors and nudges and round_no <= max_repair_rounds and not same_as_before:
                    history.append({"round": round_no, "auto_fixes": notes, "schema_errors": [],
                                    "violations": [f.message for f in nudges], "status": "improving"})
                    # Only the request, the latest design and its problems go back: the prompt stays the same size
                    # every round instead of growing with the whole history (each round got slower before).
                    messages = messages[:2] + [
                        {"role": "assistant", "content": json.dumps(data, separators=(",", ":"))},
                        {"role": "user", "content": "Almost there. Improve this and reply with the complete "
                         "corrected JSON only:\n" + "\n".join(f"- {f.message} Fix: {f.fix}" for f in nudges)}]
                    continue

            accepted = design and not errors and not fails and not erc_errors
            history.append({"round": round_no, "auto_fixes": notes, "schema_errors": errors,
                            "violations": [f.message for f in fails] + erc_errors,
                            "status": "pass" if accepted else "rejected"})
            if accepted:
                return PlanResult(design, findings, history, passed=True, blocks=self.blocks, candidates=self._extra)

            problems = ([f"- {e}" for e in errors] + [f"- [{f.rule}] {f.message} Fix: {f.fix}" for f in fails]
                        + [f"- {e}" for e in erc_errors])
            if any(e.count("Power output") >= 2 for e in erc_errors):
                problems.append("- How to fix two power outputs on one rail: a rail may have only ONE source. If a "
                                "module already makes this voltage (its 3V3 output), remove the extra regulator "
                                "component, or leave the module's output pin out of every net.")
            if any("short circuit" in e for e in errors):
                problems.append("- How to fix 'short circuit': a pin may be listed in only one net. If two nets "
                                "share a pin, they are really one node: either merge them into one net, or remove "
                                "the pin from the net it does not belong to. For an LED: GPIO -> R1 pin 1 (net "
                                "LED_DRIVE), R1 pin 2 -> LED anode (net LED_A), LED cathode in the GND net.")
            if any("lib_id" in e or "Library" in e or "unknown component" in e for e in errors):
                problems.append("- Reminder: 'lib_id' must be copied exactly from this list: "
                                + ", ".join(e["lib_id"] for e in self.allowed_parts())
                                + ". In 'connections', use the component's reference (U1, R1, C1), not its part name. "
                                "Never put a net name (like GND) inside 'connections'; to tie a pin to GND, list "
                                "that pin in the GND net.")
            messages = messages[:2] + [
                {"role": "assistant",
                 "content": json.dumps(data, separators=(",", ":")) if data is not None else raw[:4000]},
                {"role": "user", "content": "Your design was rejected. Fix ALL of these problems and reply with the "
                 "complete corrected JSON object only:\n" + "\n".join(problems)}]

        if last_good:
            design, findings, _ = last_good
            return PlanResult(design, findings, history, passed=False, blocks=self.blocks, candidates=self._extra)
        if out_of_time:
            raise PlanningTimeout(f"No valid design within the {GEN_BUDGET_S if budget_s is None else budget_s:.0f}s "
                                  "time budget (CP_GEN_BUDGET_S). Try again, or simplify the request.", history)
        raise PlanningError("The model did not produce a valid design within the repair limit.", history)
