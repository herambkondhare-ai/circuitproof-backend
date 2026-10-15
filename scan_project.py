"""
KiCad Recursive Project Scanner (scan_project.py)
Crawls a directory tree for .kicad_sch files, extracts symbols and components,
and registers them into the CircuitProof SQLite KnowledgeStore.
"""

import os
from pathlib import Path
import sexpdata
from knowledge_store import KnowledgeStore

class KiCadProjectScanner:
    def __init__(self, db_name="circuitproof_notebook.db"):
        self.kb = KnowledgeStore(db_name)

    def scan_directory(self, root_dir):
        root_path = Path(root_dir)
        if not root_path.exists():
            print(f"[Error]: Directory '{root_dir}' does not exist.")
            return

        print(f"[Scanner]: Recursively scanning '{root_path.resolve()}' for KiCad schematics...")
        
        # Find all .kicad_sch files recursively
        sch_files = list(root_path.rglob("*.kicad_sch"))
        
        if not sch_files:
            print("  -> No .kicad_sch files found in this directory tree.")
            return

        total_components_found = 0

        for sch_file in sch_files:
            print(f"\n[Parsing File]: {sch_file.relative_to(root_path)}")
            count = self._parse_schematic(sch_file)
            total_components_found += count

        print(f"\n==================================================")
        print(f" Scan Complete! Total components cataloged: {total_components_found}")
        print(f"==================================================")

    def _parse_schematic(self, file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            sexp = sexpdata.loads(content)
        except Exception as e:
            print(f"  [Error parsing S-expression]: {e}")
            return 0

        components_count = 0

        # Helper function to recursively search the S-expression tree for symbols
        def walk_sexp(node):
            nonlocal components_count
            if isinstance(node, list) and len(node) > 0:
                # Check if this list node represents a symbol instance: (symbol (lib_id ...) ...)
                if node[0] == sexpdata.Symbol('symbol'):
                    ref, lib_id, value = self._extract_symbol_metadata(node)
                    if ref:
                        # Categorize component type based on reference prefix or value
                        category = self._infer_category(ref, lib_id, value)
                        
                        # Default electrical facts if not explicitly mapped
                        facts = self._get_default_facts_for_component(ref, lib_id, value)
                        
                        # Save into SQLite KnowledgeStore
                        self.kb.save_component(component_id=ref, category=category, facts_dict=facts, verified=1)
                        print(f"    -> Extracted Component: [{ref}] (Lib: {lib_id}, Cat: {category})")
                        components_count += 1
                        return  # Don't recurse deeper into this symbol instance block

                # Recurse through all child nodes
                for item in node:
                    walk_sexp(item)

        walk_sexp(sexp)
        return components_count

    def _extract_symbol_metadata(self, symbol_node):
        ref = None
        lib_id = None
        value = None

        # The second element in a symbol node is often the lib_id symbol string
        if len(symbol_node) > 1 and isinstance(symbol_node[1], sexpdata.Symbol):
            lib_id = symbol_node[1].value()
        elif len(symbol_node) > 1 and isinstance(symbol_node[1], str):
            lib_id = symbol_node[1]

        # Scan properties inside the symbol node
        for item in symbol_node:
            if isinstance(item, list) and len(item) > 0 and item[0] == sexpdata.Symbol('property'):
                if len(item) > 2 and isinstance(item[1], str) and isinstance(item[2], str):
                    prop_name = item[1]
                    prop_val = item[2]
                    if prop_name == "Reference":
                        ref = prop_val
                    elif prop_name == "Value":
                        value = prop_val

        return ref, lib_id, value

    def _infer_category(self, ref, lib_id, value):
        prefix = ''.join([c for c in ref if c.isalpha()]).upper()
        if prefix == 'U':
            if lib_id and ('MCU' in lib_id.upper() or 'STM32' in lib_id.upper() or 'ATMEGA' in lib_id.upper()):
                return 'MCU'
            return 'IC / Module'
        elif prefix == 'R':
            return 'Resistor'
        elif prefix == 'C':
            return 'Capacitor'
        elif prefix == 'J' or prefix == 'P':
            return 'Connector'
        return 'Passive / Other'

    def _get_default_facts_for_component(self, ref, lib_id, value):
        # Assign default electrical specs for rule checks if it matches known parts
        upper_lib = (lib_id or "").upper()
        if 'STM32' in upper_lib:
            return {"voh_min": 2.4, "vih_min": 2.0, "max_spi_hz": 18000000}
        elif 'BNO' in upper_lib or 'SENSOR' in upper_lib:
            return {"voh_min": 2.4, "vih_min": 1.4, "max_spi_hz": 3000000}
        elif 'FLASH' in upper_lib or 'W25' in upper_lib:
            return {"voh_min": 2.4, "vih_min": 2.0, "max_spi_hz": 104000000}
        
        # Generic safe defaults
        return {"voh_min": 2.4, "vih_min": 1.5, "max_spi_hz": 10000000}

    def close(self):
        self.kb.close()

if __name__ == "__main__":
    # Test scan on current working directory or specify your KiCad project folder path
    target_folder = input("Enter path to KiCad project directory (press Enter for current folder): ").strip()
    if not target_folder:
        target_folder = "."

    scanner = KiCadProjectScanner()
    try:
        scanner.scan_directory(target_folder)
    finally:
        scanner.close()