"""
KiCad project scanner (scan_project.py)

Turns an existing .kicad_sch (made by a person, EasyEDA export, or another AI tool) into a
CircuitProof Design, so the referee can audit designs CircuitProof did not create.

Connectivity comes from KiCad's own netlist exporter (kicad-cli), so labels, power symbols,
hierarchical sheets and buses are resolved exactly as KiCad resolves them.
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from design import Component, Design, Net, net_basename
from generator import _child, _children, parse_sexpr


class KiCadCliError(RuntimeError):
    pass


def find_kicad_cli():
    candidates = [os.getenv("KICAD_CLI"), shutil.which("kicad-cli")]
    candidates += sorted(glob.glob(r"C:\Program Files\KiCad\*\bin\kicad-cli.exe"), reverse=True)
    candidates += ["/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    raise KiCadCliError("kicad-cli not found. Install KiCad 8 or newer, or set KICAD_CLI to its path.")


def _run(args):
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise KiCadCliError(f"Running kicad-cli failed: {e}") from e
    return proc


def export_netlist(sch_path):
    cli = find_kicad_cli()
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "netlist.net")
        proc = _run([cli, "sch", "export", "netlist", "--format", "kicadsexpr", "--output", out, str(sch_path)])
        if not os.path.isfile(out):
            raise KiCadCliError(f"Netlist export failed: {(proc.stderr or proc.stdout).strip()}")
        with open(out, encoding="utf-8") as f:
            return f.read()


def design_from_netlist(text):
    tree = parse_sexpr(text)
    components = []
    comps_node = _child(tree, "components") or []
    for comp in _children(comps_node, "comp"):
        ref = str(_child(comp, "ref")[1])
        if ref.startswith("#"):
            continue
        value = _child(comp, "value")
        footprint = _child(comp, "footprint")
        src = _child(comp, "libsource")
        lib_id = ""
        if src:
            lib, part = _child(src, "lib"), _child(src, "part")
            lib_id = f"{lib[1] if lib else ''}:{part[1] if part else ''}"
        components.append(Component(reference=ref, lib_id=lib_id, value=str(value[1]) if value else "",
                                    footprint=str(footprint[1]) if footprint else ""))

    refs = {c.reference for c in components}
    nets = []
    for net in _children(_child(tree, "nets") or [], "net"):
        name = net_basename(str(_child(net, "name")[1]))
        conns = []
        for node in _children(net, "node"):
            ref = str(_child(node, "ref")[1])
            if ref not in refs:
                continue
            func = _child(node, "pinfunction")
            pin = str(func[1]) if func and str(func[1]) not in ("", "~") else str(_child(node, "pin")[1])
            if [ref, pin] not in conns:
                conns.append([ref, pin])
        if conns:
            nets.append(Net(net=name, connections=conns))
    return Design(components=components, nets=nets)


def scan_schematic(sch_path, store=None, project=None):
    design = design_from_netlist(export_netlist(sch_path))
    if store is not None:
        for c in design.components:
            store.record_observed(c.lib_id, c.value, project or Path(sch_path).stem)
    return design


def run_erc(sch_path):
    """Run KiCad's own ERC. Returns {"errors": n, "warnings": n, "items": [...]} or None if unavailable."""
    try:
        cli = find_kicad_cli()
    except KiCadCliError:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "erc.json")
        _run([cli, "sch", "erc", "--format", "json", "--severity-all", "--output", out, str(sch_path)])
        if not os.path.isfile(out):
            return None
        with open(out, encoding="utf-8") as f:
            report = json.load(f)
    items = []
    for sheet in report.get("sheets", []):
        for v in sheet.get("violations", []):
            items.append({"severity": v.get("severity", ""), "type": v.get("type", ""),
                          "description": v.get("description", ""),
                          "pins": [i.get("description", "") for i in v.get("items", [])]})
    return {"errors": sum(1 for i in items if i["severity"] == "error"),
            "warnings": sum(1 for i in items if i["severity"] == "warning"),
            "items": items}


def scan_directory(root_dir, store=None):
    results = []
    for sch in sorted(Path(root_dir).rglob("*.kicad_sch")):
        try:
            results.append((sch, scan_schematic(sch, store, sch.parent.name), None))
        except KiCadCliError as e:
            results.append((sch, None, str(e)))
    return results


if __name__ == "__main__":
    from knowledge_store import KnowledgeStore
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    for path, design, err in scan_directory(target, KnowledgeStore()):
        if err:
            print(f"[FAIL] {path}: {err}")
        else:
            print(f"[OK]   {path}: {len(design.components)} components, {len(design.nets)} nets")
