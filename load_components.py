"""
Seed the knowledge store (load_components.py)

IMPORTANT: these numbers are carried over from the original seed data and have NOT been checked
against the datasheets. They load as verified=False, and every report row that uses them says so.
After checking a part against its datasheet, set "verified": True and re-run this script.

Each part may list KiCad symbols it maps to. A mapping is only stored if the symbol exists in your
installed libraries (CP_SYMBOL_DIR / KICAD*_SYMBOL_DIR); otherwise the script tells you.
Usage:  python load_components.py
"""

import os

from generator import SymbolLibrary, SynthesisError
from knowledge_store import KnowledgeStore

PARTS = [
    {
        "part_id": "STM32F103C8T6", "category": "MCU", "verified": False,
        "source": "Original seed values; check against the ST STM32F103x8/xB datasheet",
        "lib_ids": ["MCU_ST_STM32F1:STM32F103C8Tx"],
        "facts": {
            "vdd_min": 2.0, "vdd_max": 3.6, "supply_pins": ["VDD"],
            "io": {"vih_min": 2.0, "vil_max": 0.8, "voh_min": 2.4, "vol_max": 0.4},
            "pins": {"PB7": {"functions": ["I2C1_SDA", "GPIO"]},
                     "PB6": {"functions": ["I2C1_SCL", "GPIO"]}},
            "spi_max_hz": 18_000_000,
        },
    },
    {
        "part_id": "BNO085", "category": "Sensor", "verified": False,
        "source": "Original seed values; check against the CEVA BNO08x datasheet",
        "lib_ids": [],  # add the KiCad symbol you use for this sensor
        "facts": {
            "vdd_min": 1.7, "vdd_max": 3.6,
            "io": {"vih_min": 1.4, "vil_max": 0.4, "vol_max": 0.4},
            "pins": {"SDA": {"functions": ["I2C_SDA"]}, "SCL": {"functions": ["I2C_SCL"]}},
            "spi_max_hz": 3_000_000,
        },
    },
    {
        "part_id": "PCA9306", "category": "Level Translator", "verified": False,
        "source": "Original seed values; check against the TI PCA9306 datasheet",
        "lib_ids": [],
        "facts": {"vref1_min": 1.0, "vref1_max": 3.3, "vref2_min": 1.8, "vref2_max": 5.0,
                  "pins": {"EN": {"vih_min": 0.7, "vil_max": 0.3}}},
    },
    {
        "part_id": "W25Q128JV", "category": "Memory", "verified": False,
        "source": "The old code had two different SPI limits (50 MHz and 104 MHz); the lower one is kept. "
                  "Check against the Winbond W25Q128JV datasheet.",
        "lib_ids": ["Memory_Flash:W25Q128JVS"],
        "facts": {"vdd_min": 2.7, "vdd_max": 3.6, "spi_max_hz": 50_000_000,
                  "spi_setup_ns": 5.0, "spi_hold_ns": 2.0},
    },
]


def main():
    store = KnowledgeStore(os.getenv("CP_DB", "circuitproof_notebook.db"))
    try:
        lib = SymbolLibrary([os.getenv("CP_SYMBOL_DIR"), os.getenv("KICAD9_SYMBOL_DIR"),
                             os.getenv("KICAD8_SYMBOL_DIR"), "symbols"])
    except SynthesisError as e:
        lib = None
        print(f"[warn] {e}\n       Symbol mappings will be skipped until libraries are available.")

    for p in PARTS:
        store.save_part(p["part_id"], p["category"], p["facts"], p["verified"], p["source"])
        flag = "verified" if p["verified"] else "UNVERIFIED seed data"
        print(f"[part] {p['part_id']:<14} saved ({flag})")
        for lib_id in p["lib_ids"]:
            if lib is not None and lib.has(lib_id):
                store.map_lib(lib_id, p["part_id"])
                print(f"       mapped -> {lib_id}")
            else:
                print(f"       [skip] symbol {lib_id} not found in installed libraries; fix the name")
    print("\nCatalog the planner will offer to Nemotron:")
    for e in store.catalog():
        print(f"  {e['lib_id']} -> {e['part_id']}")


if __name__ == "__main__":
    main()
