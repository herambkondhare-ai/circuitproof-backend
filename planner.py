"""
Design planner (planner.py)

Nemotron proposes, the referee disposes:
  prompt -> Nemotron JSON -> schema check -> KiCad library check -> referee audit
        ^-------------- errors and FAIL findings fed back ----------------|
"""

import json
from dataclasses import dataclass, field

from circuitproof_core import FAIL
from design import DesignError, parse_design
from generator import SchematicSynthesizer, SynthesisError
from llm_client import extract_json

GENERIC_PARTS = [
    {"lib_id": "Device:R", "part_id": "", "category": "Resistor"},
    {"lib_id": "Device:C", "part_id": "", "category": "Capacitor"},
    {"lib_id": "Device:LED", "part_id": "", "category": "LED"},
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
- Connect every supply and ground pin of every IC.
- Add the support parts a real board needs (pull-up resistors on I2C lines, decoupling capacitors), with values.
- Include "buses" only for SPI links with a known clock speed; otherwise use an empty list.
"""


class PlanningError(RuntimeError):
    def __init__(self, message, history):
        super().__init__(message)
        self.history = history


@dataclass
class PlanResult:
    design: object
    findings: list
    history: list = field(default_factory=list)
    passed: bool = False


class DesignPlanner:
    def __init__(self, llm, store, synth, referee):
        self.llm, self.store, self.synth, self.referee = llm, store, synth, referee

    def catalog_text(self):
        lines, entries = [], self.store.catalog() + GENERIC_PARTS
        for e in entries:
            try:
                info = self.synth.lib.get(e["lib_id"])
            except SynthesisError:
                continue  # mapped in the DB but not in the installed KiCad libraries
            names = []
            for p in info["pins"]:
                label = p["name"] if p["name"] not in ("", "~") else p["number"]
                if label not in names:
                    names.append(label)
            label = e["part_id"] or "generic"
            lines.append(f"- {e['lib_id']} ({label}, {e['category']}): pins {', '.join(names[:120])}")
        if not lines:
            raise PlanningError("No usable parts: the knowledge store has no parts whose KiCad symbols "
                                "are installed. Run load_components.py and check CP_SYMBOL_DIR.", [])
        return "\n".join(lines)

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

    def run(self, prompt, max_repair_rounds=3):
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"Allowed parts:\n{self.catalog_text()}\n\n"
                                                f"Design request:\n{prompt}"}]
        history, last_good = [], None
        for round_no in range(1, max_repair_rounds + 2):
            raw = self.llm.chat(messages)
            design, errors, fails, findings, data = None, [], [], [], None
            try:
                data = extract_json(raw)
                design = parse_design(data)
                errors = self.library_errors(design)
            except DesignError as e:
                errors = e.errors
            except ValueError as e:
                errors = [str(e)]

            if design and not errors:
                findings = self.referee.audit(design, log_mistakes=False)
                fails = [f for f in findings if f.status == FAIL]
                last_good = (design, findings)

            history.append({"round": round_no, "schema_errors": errors,
                            "violations": [f.message for f in fails],
                            "status": "pass" if design and not errors and not fails else "rejected"})
            if design and not errors and not fails:
                return PlanResult(design, findings, history, passed=True)

            problems = [f"- {e}" for e in errors] + [f"- [{f.rule}] {f.message} Fix: {f.fix}" for f in fails]
            messages.append({"role": "assistant",
                             "content": json.dumps(data) if data is not None else raw[:4000]})
            messages.append({"role": "user", "content":
                             "Your design was rejected. Fix ALL of these problems and reply with the "
                             "complete corrected JSON object only:\n" + "\n".join(problems)})

        if last_good:
            design, findings = last_good
            return PlanResult(design, findings, history, passed=False)
        raise PlanningError("The model did not produce a valid design within the repair limit.", history)
