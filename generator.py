"""
CircuitProof Bounded Schematic Synthesizer (generator.py)
Enforces strict A4 page boundaries, grid coordinate math, and auto-routing.
"""

import os
import uuid

class SchematicSynthesizer:
    def __init__(self, output_dir="."):
        self.output_dir = output_dir
        
        # A4 Schematic Sheet Boundaries (in mm)
        self.MIN_X = 30.0
        self.MAX_X = 260.0
        self.MIN_Y = 30.0
        self.MAX_Y = 170.0
        
        # Grid spacing parameters
        self.COL_SPACING = 70.0
        self.ROW_SPACING = 50.0

    def _generate_uuid(self):
        return str(uuid.uuid4())

    def _calculate_bounded_coordinates(self, index: int, total_components: int) -> dict:
        """
        Calculates X, Y coordinates ensuring components stay strictly 
        within the A4 boundary box with automatic row-wrapping.
        """
        # Determine max columns that fit safely within MAX_X
        available_width = self.MAX_X - self.MIN_X
        cols = max(1, int(available_width / self.COL_SPACING))
        
        col_idx = index % cols
        row_idx = index // cols
        
        x = self.MIN_X + (col_idx * self.COL_SPACING)
        y = self.MIN_Y + (row_idx * self.ROW_SPACING)
        
        # Safety clamp to ensure it never exceeds boundaries
        x = min(max(x, self.MIN_X), self.MAX_X)
        y = min(max(y, self.MIN_Y), self.MAX_Y)
        
        return {"x": x, "y": y}

    def synthesize(self, schematic_name, components_list, nets_list=None):
        file_path = os.path.join(self.output_dir, schematic_name)
        print(f"[Synthesizer Pro]: Synthesizing bounded schematic -> {file_path}")

        if nets_list is None:
            nets_list = [
                {"net": "VCC", "connections": [("U1", "VDD"), ("U2", "VCC")]},
                {"net": "GND", "connections": [("U1", "GND"), ("U2", "GND")]},
                {"net": "I2C_SDA", "connections": [("U1", "PB7"), ("U2", "SDA")]},
                {"net": "I2C_SCL", "connections": [("U1", "PB6"), ("U2", "SCL")]}
            ]

        # Root S-Expression header for KiCad 7/8 schema with A4 paper definition
        sexp = '(kicad_sch (version 20230121) (generator "CircuitProof-Bounded-Engine")\n\n'
        sexp += f'  (uuid "{self._generate_uuid()}")\n\n'
        sexp += '  (paper "A4")\n\n'

        component_coords = {}
        total_comps = len(components_list)

        # 1. Place Components within Bounded Grid Coordinates
        for idx, comp in enumerate(components_list):
            ref = comp.get('reference', f'U{idx+1}')
            lib_id = comp.get('lib_id', 'Device:R')
            
            # Get safe bounded coordinates
            coords = self._calculate_bounded_coordinates(idx, total_comps)
            pos_x = coords["x"]
            pos_y = coords["y"]
            
            component_coords[ref] = {"x": pos_x, "y": pos_y}
            comp_uuid = self._generate_uuid()

            sexp += f'  (symbol (lib_id "{lib_id}") (at {pos_x} {pos_y} 0) (unit 1)\n'
            sexp += f'    (in_bom yes) (on_board yes)\n'
            sexp += f'    (property "Reference" "{ref}" (at {pos_x} {pos_y - 6} 0)\n'
            sexp += '      (effects (font (size 1.27 1.27)))\n'
            sexp += '    )\n'
            sexp += f'    (property "Value" "{lib_id.split(":")[-1]}" (at {pos_x} {pos_y + 6} 0)\n'
            sexp += '      (effects (font (size 1.27 1.27)))\n'
            sexp += '    )\n'
            sexp += f'    (uuid "{comp_uuid}")\n'
            sexp += '  )\n\n'

        # 2. Draw Bounded Wires and Net Labels between Connected Pins
        for net_item in nets_list:
            net_name = net_item["net"]
            conns = net_item["connections"]
            
            if len(conns) >= 2:
                ref1, _ = conns[0]
                ref2, _ = conns[1]
                
                if ref1 in component_coords and ref2 in component_coords:
                    pt1_x = component_coords[ref1]["x"] + 10.0
                    pt1_y = component_coords[ref1]["y"] + 5.0
                    pt2_x = component_coords[ref2]["x"] - 10.0
                    pt2_y = component_coords[ref2]["y"] + 5.0

                    # Clamp wire points to sheet boundaries
                    pt1_x = min(max(pt1_x, self.MIN_X), self.MAX_X)
                    pt2_x = min(max(pt2_x, self.MIN_X), self.MAX_X)

                    # Write KiCad Wire Segment S-expression
                    sexp += f'  (wire (pts (xy {pt1_x} {pt1_y}) (xy {pt2_x} {pt2_y}))\n'
                    sexp += '    (stroke (width 0) (type default))\n'
                    sexp += f'    (uuid "{self._generate_uuid()}")\n'
                    sexp += '  )\n\n'

                    # Write Net Label S-expression
                    mid_x = (pt1_x + pt2_x) / 2.0
                    mid_y = pt1_y - 2.0
                    sexp += f'  (label "{net_name}" (at {mid_x} {mid_y} 0)\n'
                    sexp += '    (effects (font (size 1.27 1.27)) (justify left bottom))\n'
                    sexp += f'    (uuid "{self._generate_uuid()}")\n'
                    sexp += '  )\n\n'

        sexp += ')\n'

        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(sexp)

        print(f"[Success Bounded]: Schematic synthesized at '{file_path}' within A4 boundaries!")
        return file_path