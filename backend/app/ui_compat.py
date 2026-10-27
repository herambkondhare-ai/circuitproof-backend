"""
Shapes backend results into the format the Web Studio (frontend/index.html) renders.
Kept separate from main.py so it can be tested without FastAPI.
"""

from circuitproof_core import FAIL, PASS, UNVERIFIED, WARN, ref_prefix

UI_RESULT = {PASS: "PASS", FAIL: "FAIL", WARN: "WARNING", UNVERIFIED: "NE"}
UI_SEVERITY = {FAIL: "Critical", WARN: "Warning", UNVERIFIED: "Info", PASS: "—"}
DATA_LABEL = {True: "datasheet-verified", False: "seed data (not yet verified)", None: "—"}
PREFIX_CATEGORY = {"R": "Resistor", "C": "Capacitor", "L": "Inductor", "D": "Diode", "LED": "LED",
                   "J": "Connector", "P": "Connector", "SW": "Switch", "Y": "Crystal", "F": "Fuse"}


def ui_findings(findings):
    out = []
    for i, f in enumerate(findings, 1):
        out.append({
            "id": f"F{i:02d}", "rule": f.rule, "res": UI_RESULT[f.status], "status": f.status,
            "sev": UI_SEVERITY[f.status], "ref": list(f.refs), "aff": ", ".join(f.refs) or "—",
            "message": f.message, "fix": f.fix or "—", "data": DATA_LABEL[f.data_verified],
        })
    return out


def ui_components(design, store):
    pins = {}
    for n in design.nets:
        for ref, pin in n.connections:
            pins.setdefault(ref, []).append(pin)
    out = []
    for c in design.components:
        part = store.resolve(c.lib_id, c.value, c.part_id)
        category = (part or {}).get("category") or PREFIX_CATEGORY.get(ref_prefix(c.reference), "IC")
        out.append({"reference": c.reference, "lib_id": c.lib_id, "value": c.value,
                    "part": c.value or c.lib_id.split(":")[-1], "category": category,
                    "block": c.block, "pins": pins.get(c.reference, [])})
    return out


def ui_nets(design):
    return [{"net": n.net, "connections": [list(x) for x in n.connections]} for n in design.nets]


def design_input_from_ui(components, nets):
    """The studio sends back its own component objects (with x/y etc.); keep what the referee needs."""
    comps = []
    for c in components or []:
        comps.append({"reference": c.get("reference", ""), "lib_id": c.get("lib_id", ""),
                      "value": c.get("value") or c.get("part") or "", "part_id": c.get("part_id", ""),
                      "block": c.get("block", "peripheral")})
    return {"components": comps, "nets": nets or [], "rails": {}, "buses": []}


def explanation(history, passed, llm_info, blocks=None):
    model = llm_info.get("model") or "the configured model"
    lines = [f"Planner: {model} via {llm_info.get('provider')}. "
             f"Every proposal was checked by the deterministic referee before synthesis."]
    if blocks:
        lines.append("Block plan: " + "; ".join(f"{b.get('role', 'block')}: {b.get('need')}" for b in blocks))
    for h in history:
        problems = h["schema_errors"] + h["violations"]
        fixes = h.get("auto_fixes") or []
        if fixes:
            lines.append(f"Round {h['round']}: {len(fixes)} naming slip(s) auto-corrected: " + "; ".join(fixes[:3]))
        if h["status"] == "pass":
            lines.append(f"Round {h['round']}: accepted by the referee.")
        else:
            lines.append(f"Round {h['round']}: rejected ({len(problems)} problem(s)), sent back for repair: "
                         + "; ".join(problems[:3]) + (" …" if len(problems) > 3 else ""))
    if not passed:
        lines.append("The repair limit was reached with violations remaining. See the findings before using this design.")
    return "\n\n".join(lines)
