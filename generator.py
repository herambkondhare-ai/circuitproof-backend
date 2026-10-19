"""
CircuitProof Bounded Schematic Synthesizer (generator.py)

Deterministic placement + net-label connectivity for KiCad 7/8/9 schematics.

- Symbols are loaded from real KiCad .kicad_sym libraries and embedded in lib_symbols,
  so KiCad can draw them without "missing symbol" errors.
- Pin positions are read from those symbols, so every wire starts exactly on a real pin.
- Every coordinate snaps to the 1.27 mm schematic grid.
- Placement is zone-based (power / mcu / peripheral) with bounding-box collision checks.
- Connectivity uses short wire stubs + net labels: no routing, no crossing wires.
- Anything wrong (unknown pin, overflowing sheet, pin in two nets) raises SynthesisError
  instead of silently writing a broken file.
"""

import copy
import glob
import os
import re
import uuid

GRID = 1.27          # KiCad schematic grid (50 mil)
STUB = 2.54          # wire stub length from pin end to its net label
PAD_X = 12.7         # horizontal room around a symbol for stubs + label text
PAD_Y = 5.08         # vertical room around a symbol
SCAN_STEP = 2.54     # placement search step (keeps origins on grid)
MIN_FILE_VERSION = 20230121  # KiCad 7 schematic format


class SynthesisError(Exception):
    pass


# ---------------------------------------------------------------- S-expressions
class QStr(str):
    """A quoted-string token (kept raw/escaped exactly as read)."""


_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))')


def parse_sexpr(text):
    stack, cur, pos = [], [], 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            if not text[pos:].strip():
                break
            raise SynthesisError(f"S-expression parse error near offset {pos}")
        pos = m.end()
        lp, rp, qs, atom = m.groups()
        if lp:
            stack.append(cur)
            cur = []
        elif rp:
            if not stack:
                raise SynthesisError("Unbalanced ')' in S-expression")
            done, cur = cur, stack.pop()
            cur.append(done)
        elif qs is not None:
            cur.append(QStr(qs))
        elif atom is not None:
            cur.append(atom)
    if stack:
        raise SynthesisError("Unbalanced '(' in S-expression")
    return cur[0] if cur else None


def _atom(a):
    return f'"{a}"' if isinstance(a, QStr) else str(a)


def dump_sexpr(node, indent=0):
    if not isinstance(node, list):
        return _atom(node)
    if not any(isinstance(c, list) for c in node):
        return "(" + " ".join(_atom(c) for c in node) + ")"
    pad = "  " * (indent + 1)
    parts = []
    for c in node:
        if isinstance(c, list):
            parts.append("\n" + pad + dump_sexpr(c, indent + 1))
        else:
            parts.append((" " if parts else "") + _atom(c))
    return "(" + "".join(parts) + ")"


def _head(n):
    return n[0] if isinstance(n, list) and n else None


def _children(n, name):
    return [c for c in n if isinstance(c, list) and c and c[0] == name]


def _child(n, name):
    found = _children(n, name)
    return found[0] if found else None


def q(s):
    """Quote a user-supplied string for KiCad."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def fmt(v):
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def snap(v):
    return round(round(v / GRID) * GRID, 4)


# ---------------------------------------------------------------- symbol library
def _unit_ok(subname):
    """Keep only unit 0/1, body style 0/1 graphics and pins (sub-symbol NAME_u_s)."""
    m = re.search(r"_(\d+)_(\d+)$", subname)
    return not m or (int(m.group(1)) in (0, 1) and int(m.group(2)) in (0, 1))


def _extract_pins(sym):
    pins = []
    for sub in _children(sym, "symbol"):
        if not _unit_ok(str(sub[1])):
            continue
        for p in _children(sub, "pin"):
            at, name, num = _child(p, "at"), _child(p, "name"), _child(p, "number")
            pins.append({
                "name": str(name[1]) if name else "",
                "number": str(num[1]) if num else "",
                "x": float(at[1]),
                "y": float(at[2]),
                "angle": int(float(at[3])) % 360 if len(at) > 3 else 0,
            })
    return pins


def _extract_bbox(sym):
    xs, ys = [], []

    def walk(n):
        if not isinstance(n, list):
            return
        if _head(n) in ("at", "start", "end", "xy", "center", "mid") and len(n) >= 3:
            try:
                xs.append(float(n[1]))
                ys.append(float(n[2]))
            except ValueError:
                pass
        for c in n:
            walk(c)

    for sub in _children(sym, "symbol"):
        if _unit_ok(str(sub[1])):
            walk(sub)
    if not xs:
        return (-2.54, -2.54, 2.54, 2.54)
    return (min(xs), min(ys), max(xs), max(ys))  # library coords, Y up


class SymbolLibrary:
    def __init__(self, search_dirs):
        self.dirs = [d for d in search_dirs if d and os.path.isdir(d)]
        if not self.dirs:
            raise SynthesisError(
                "No KiCad symbol directory found. Pass symbol_dirs=[...] or set "
                "KICAD8_SYMBOL_DIR, or copy the needed .kicad_sym files into ./symbols")
        self._libs, self._cache = {}, {}
        self.max_version = MIN_FILE_VERSION

    def _load_lib(self, nick):
        """Load a library as a single .kicad_sym file or a .kicad_symdir folder."""
        if nick not in self._libs:
            for d in self.dirs:
                single = os.path.join(d, nick + ".kicad_sym")
                folder = os.path.join(d, nick + ".kicad_symdir")
                if os.path.isfile(single):
                    files = [single]
                elif os.path.isdir(folder):
                    files = sorted(glob.glob(os.path.join(folder, "*.kicad_sym")))
                else:
                    continue
                symbols = {}
                for path in files:
                    with open(path, encoding="utf-8") as f:
                        tree = parse_sexpr(f.read())
                    ver = _child(tree, "version")
                    if ver:
                        self.max_version = max(self.max_version, int(ver[1]))
                    symbols.update({str(s[1]): s for s in _children(tree, "symbol")})
                self._libs[nick] = symbols
                break
            else:
                raise SynthesisError(f"Library '{nick}' (.kicad_sym or .kicad_symdir) "
                                     f"not found in {self.dirs}")
        return self._libs[nick]

    def has(self, lib_id):
        try:
            self.get(lib_id)
            return True
        except SynthesisError:
            return False

    def _flatten(self, lib, name, depth=0):
        """Resolve (extends "Parent") symbols into a standalone definition."""
        if name not in lib:
            raise SynthesisError(f"Symbol '{name}' not found")
        sym = copy.deepcopy(lib[name])
        ext = _child(sym, "extends")
        if ext is None:
            return sym
        if depth > 5:
            raise SynthesisError(f"'extends' chain too deep for '{name}'")
        parent = self._flatten(lib, str(ext[1]), depth + 1)
        pname = str(parent[1])
        rest = [c for c in parent[2:] if _head(c) != "property"]
        for c in rest:
            if _head(c) == "symbol" and str(c[1]).startswith(pname + "_"):
                c[1] = QStr(name + str(c[1])[len(pname):])
        idx = next((i for i, c in enumerate(rest) if _head(c) == "symbol"), len(rest))
        return ["symbol", QStr(name)] + rest[:idx] + _children(sym, "property") + rest[idx:]

    def get(self, lib_id):
        if lib_id in self._cache:
            return self._cache[lib_id]
        if ":" not in lib_id:
            raise SynthesisError(f"lib_id '{lib_id}' must look like 'Library:Symbol'")
        nick, name = lib_id.split(":", 1)
        lib = self._load_lib(nick)
        if name not in lib:
            raise SynthesisError(f"Symbol '{name}' not in library '{nick}'")
        sym = self._flatten(lib, name)
        sym[1] = QStr(lib_id)  # embedded symbols are named "Lib:Symbol"
        info = {"sexpr": sym, "pins": _extract_pins(sym), "bbox": _extract_bbox(sym)}
        self._cache[lib_id] = info
        return info


# ---------------------------------------------------------------- synthesizer
# Outward direction (screen coords, Y down) for each KiCad pin angle,
# plus the label angle/justification that reads naturally at that side.
_PIN_OUT = {
    0:   ((-1, 0), 180, "right"),   # pin on left side  -> stub goes left
    180: ((1, 0),  0,   "left"),    # pin on right side -> stub goes right
    90:  ((0, 1),  270, "right"),   # pin on bottom     -> stub goes down
    270: ((0, -1), 90,  "left"),    # pin on top        -> stub goes up
}


class SchematicSynthesizer:
    MIN_X, MAX_X = 30.0, 260.0
    MIN_Y, MAX_Y = 30.0, 170.0
    ZONES = {
        "power":      (30.0, 95.0),
        "mcu":        (95.0, 180.0),
        "peripheral": (180.0, 260.0),
    }

    def __init__(self, output_dir=".", symbol_dirs=None):
        self.output_dir = output_dir
        dirs = list(symbol_dirs or []) + [
            os.environ.get("KICAD9_SYMBOL_DIR"),
            os.environ.get("KICAD8_SYMBOL_DIR"),
            os.environ.get("KICAD7_SYMBOL_DIR"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "symbols"),
        ]
        self.lib = SymbolLibrary(dirs)
        self.last_unconnected = []

    @staticmethod
    def _uuid():
        return str(uuid.uuid4())

    # ---------- placement
    def _screen_box(self, bbox, X, Y):
        bx0, by0, bx1, by1 = bbox
        return (X + bx0 - PAD_X, Y - by1 - PAD_Y, X + bx1 + PAD_X, Y - by0 + PAD_Y)

    def _place(self, info, block, placed):
        own = self.ZONES.get(block, self.ZONES["peripheral"])
        for zx0, zx1 in (own, (self.MIN_X, self.MAX_X)):  # own zone, then anywhere
            Y = snap(self.MIN_Y)
            while Y <= self.MAX_Y:
                X = snap(zx0)
                while X <= zx1:
                    box = self._screen_box(info["bbox"], X, Y)
                    inside = (box[0] >= zx0 and box[2] <= zx1 and
                              box[1] >= self.MIN_Y and box[3] <= self.MAX_Y)
                    if inside and not any(box[0] < b[2] and box[2] > b[0] and
                                          box[1] < b[3] and box[3] > b[1] for b in placed):
                        return X, Y, box
                    X = round(X + SCAN_STEP, 4)
                Y = round(Y + SCAN_STEP, 4)
        raise SynthesisError("Design does not fit inside the A4 bounds; split it into sheets "
                             "or reduce components.")

    # ---------- pins
    @staticmethod
    def _match_pins(pins, key):
        by_num = [p for p in pins if p["number"] == key]
        if by_num:
            return by_num
        return [p for p in pins if p["name"].upper() == key.upper()]

    def _stub(self, inst, pin, net):
        if pin["angle"] not in _PIN_OUT:
            raise SynthesisError(f"Unsupported pin angle {pin['angle']} on {inst['ref']}")
        (dx, dy), lab_angle, justify = _PIN_OUT[pin["angle"]]
        px = round(inst["X"] + pin["x"], 4)
        py = round(inst["Y"] - pin["y"], 4)  # library Y is up, schematic Y is down
        ex, ey = round(px + dx * STUB, 4), round(py + dy * STUB, 4)
        return {"from": (px, py), "to": (ex, ey), "net": net,
                "angle": lab_angle, "justify": justify}

    # ---------- main entry
    def synthesize(self, schematic_name, components_list, nets_list=None, project_name=None):
        """
        components_list: [{"reference": "U1", "lib_id": "MCU_ST_STM32F1:STM32F103C8Tx",
                           "value": "STM32F103", "footprint": "...", "block": "mcu"}, ...]
        nets_list: [{"net": "I2C_SDA", "connections": [("U1", "PB7"), ("U2", "SDA")]}, ...]
                   Pins may be given by number ("21") or by name ("SDA").
        Returns (file_path, warnings). Unconnected pins are left in self.last_unconnected
        as (reference, pin_number, pin_name) tuples for the referee.
        """
        if not components_list:
            raise SynthesisError("components_list is empty")
        if nets_list is None:
            raise SynthesisError("nets_list is required (no default nets are assumed)")

        file_path = os.path.join(self.output_dir, schematic_name)
        project = project_name or os.path.splitext(os.path.basename(schematic_name))[0]
        root_uuid = self._uuid()
        warnings = []

        # 1. Resolve symbols and place them
        insts, placed = {}, []
        for comp in components_list:
            ref, lib_id = comp.get("reference"), comp.get("lib_id")
            if not ref or not lib_id:
                raise SynthesisError(f"Component needs 'reference' and 'lib_id': {comp}")
            if ref in insts:
                raise SynthesisError(f"Duplicate reference '{ref}'")
            info = self.lib.get(lib_id)
            X, Y, box = self._place(info, comp.get("block", "peripheral"), placed)
            placed.append(box)
            insts[ref] = {"ref": ref, "comp": comp, "info": info, "X": X, "Y": Y}

        # 2. Resolve nets to real pins and build stubs
        pin_net, stubs = {}, []
        for net in nets_list:
            name = net["net"]
            conns = net.get("connections", [])
            if len(conns) < 2:
                warnings.append(f"Net '{name}' has fewer than 2 connections")
            for ref, pin_key in conns:
                inst = insts.get(ref)
                if inst is None:
                    raise SynthesisError(f"Net '{name}' references unknown component '{ref}'")
                pins = self._match_pins(inst["info"]["pins"], str(pin_key))
                if not pins:
                    avail = sorted({p["name"] or p["number"] for p in inst["info"]["pins"]})
                    raise SynthesisError(
                        f"{ref} ({inst['comp']['lib_id']}) has no pin '{pin_key}'. "
                        f"Available: {', '.join(avail)}")
                for p in pins:
                    key = (ref, p["number"], p["x"], p["y"])
                    if key in pin_net:
                        if pin_net[key] != name:
                            raise SynthesisError(
                                f"{ref} pin {p['number']} is in both '{pin_net[key]}' and '{name}'")
                        continue
                    pin_net[key] = name
                    stubs.append(self._stub(inst, p, name))

        self.last_unconnected = []
        for ref, inst in insts.items():
            for p in inst["info"]["pins"]:
                if (ref, p["number"], p["x"], p["y"]) not in pin_net:
                    self.last_unconnected.append((ref, p["number"], p["name"]))

        # 3. Write the file
        out = [f"(kicad_sch (version {self.lib.max_version}) (generator \"circuitproof\")",
               f"  (uuid {q(root_uuid)})",
               '  (paper "A4")',
               "  (lib_symbols"]
        for info in {i["comp"]["lib_id"]: i["info"] for i in insts.values()}.values():
            out.append("    " + dump_sexpr(info["sexpr"], 2))
        out.append("  )")

        for ref, inst in insts.items():
            out.append(self._symbol_sexpr(inst, project, root_uuid))

        for s in stubs:
            (x1, y1), (x2, y2) = s["from"], s["to"]
            out.append(f"  (wire (pts (xy {fmt(x1)} {fmt(y1)}) (xy {fmt(x2)} {fmt(y2)}))\n"
                       f"    (stroke (width 0) (type default))\n"
                       f"    (uuid {q(self._uuid())}))")
            out.append(f"  (label {q(s['net'])} (at {fmt(x2)} {fmt(y2)} {s['angle']})\n"
                       f"    (effects (font (size 1.27 1.27)) (justify {s['justify']} bottom))\n"
                       f"    (uuid {q(self._uuid())}))")

        out.append('  (sheet_instances (path "/" (page "1")))')
        out.append(")")

        text = "\n".join(out) + "\n"
        parse_sexpr(text)  # self-check: output must be well-formed
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(text)
        return file_path, warnings

    def _symbol_sexpr(self, inst, project, root_uuid):
        comp, info, X, Y = inst["comp"], inst["info"], inst["X"], inst["Y"]
        bx0, by0, bx1, by1 = info["bbox"]
        side_pins = any(p["angle"] in (0, 180) for p in info["pins"])
        if side_pins:   # ICs: text above / below the body
            ref_at = (X, snap(Y - by1 - 2.54), "")
            val_at = (X, snap(Y - by0 + 2.54), "")
        else:           # 2-pin vertical parts: text to the right
            ref_at = (snap(X + bx1 + 1.27), snap(Y - 1.27), " (justify left)")
            val_at = (snap(X + bx1 + 1.27), snap(Y + 1.27), " (justify left)")
        value = comp.get("value") or comp["lib_id"].split(":")[-1]

        def prop(name, val, at, hidden=False):
            x, y, just = at
            hide = " hide" if hidden else ""
            return (f"    (property {q(name)} {q(val)} (at {fmt(x)} {fmt(y)} 0)\n"
                    f"      (effects (font (size 1.27 1.27)){just}{hide}))")

        lines = [f"  (symbol (lib_id {q(comp['lib_id'])}) (at {fmt(X)} {fmt(Y)} 0) (unit 1)",
                 "    (in_bom yes) (on_board yes)",
                 f"    (uuid {q(self._uuid())})",
                 prop("Reference", inst["ref"], ref_at),
                 prop("Value", value, val_at),
                 prop("Footprint", comp.get("footprint", ""), (X, Y, ""), hidden=True),
                 prop("Datasheet", comp.get("datasheet", "~"), (X, Y, ""), hidden=True)]
        for num in dict.fromkeys(p["number"] for p in info["pins"] if p["number"]):
            lines.append(f"    (pin {q(num)} (uuid {q(self._uuid())}))")
        lines.append(f"    (instances (project {q(project)}\n"
                     f"      (path {q('/' + root_uuid)} (reference {q(inst['ref'])}) (unit 1))))")
        lines.append("  )")
        return "\n".join(lines)
