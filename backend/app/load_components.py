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

from generator import SymbolLibrary, SynthesisError, default_symbol_dirs
from knowledge_store import DEFAULT_DB, KnowledgeStore

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
    # Supply ranges for parts the planner commonly picks from the KiCad library. They are matched by KiCad
    # symbol name prefix (no lib_ids, so they don't enlarge the planner's catalog). Typical datasheet values,
    # NOT yet checked: verify each against its datasheet and set "verified": True.
    {"part_id": "ESP32-WROOM-32", "category": "MCU", "verified": False, "lib_ids": [],
     "source": "Typical value; check against the Espressif ESP32-WROOM-32 datasheet",
     "facts": {"vdd_min": 3.0, "vdd_max": 3.6}},
    {"part_id": "BME280", "category": "Sensor", "verified": False, "lib_ids": [],
     "source": "Typical value; check against the Bosch BME280 datasheet",
     "facts": {"vdd_min": 1.71, "vdd_max": 3.6}},
    {"part_id": "BMP280", "category": "Sensor", "verified": False, "lib_ids": [],
     "source": "Typical value; check against the Bosch BMP280 datasheet",
     "facts": {"vdd_min": 1.71, "vdd_max": 3.6}},
    {"part_id": "MPU-6050", "category": "Sensor", "verified": False, "lib_ids": [],
     "source": "Typical value; check against the InvenSense MPU-6050 datasheet",
     "facts": {"vdd_min": 2.375, "vdd_max": 3.46}},
    {"part_id": "DS3231", "category": "RTC", "verified": False, "lib_ids": [],
     "source": "Typical value; check against the Analog Devices DS3231 datasheet",
     "facts": {"vdd_min": 2.3, "vdd_max": 5.5}},
    {"part_id": "ATmega328P", "category": "MCU", "verified": False, "lib_ids": [],
     "source": "Typical value; check against the Microchip ATmega328P datasheet",
     "facts": {"vdd_min": 1.8, "vdd_max": 5.5}},
    {"part_id": "CH340G", "category": "Interface", "verified": False, "lib_ids": [],
     "source": "Typical value; check against the WCH CH340 datasheet",
     "facts": {"vdd_min": 3.0, "vdd_max": 5.5}},
    {"part_id": "MCP2551", "category": "Interface", "verified": False, "lib_ids": [],
     "source": "Typical value; check against the Microchip MCP2551 datasheet",
     "facts": {"vdd_min": 4.5, "vdd_max": 5.5}},
    {"part_id": "MAX3485", "category": "Interface", "verified": False, "lib_ids": [],
     "source": "Typical value; check against the Analog Devices MAX3485 datasheet",
     "facts": {"vdd_min": 3.0, "vdd_max": 3.6}},
]


def seed(store, lib=None, quiet=False, only_missing=False):
    """Save the seed parts and map them to KiCad symbols that exist in `lib`.
    only_missing=True (used at server start) adds new seed parts and never overwrites stored ones."""
    say = (lambda *a: None) if quiet else print
    for p in PARTS:
        if only_missing and store.has_part(p["part_id"]):
            continue
        store.save_part(p["part_id"], p["category"], p["facts"], p["verified"], p["source"])
        say(f"[part] {p['part_id']:<14} saved ({'verified' if p['verified'] else 'UNVERIFIED seed data'})")
        for lib_id in p["lib_ids"]:
            if lib is not None and lib.has(lib_id):
                store.map_lib(lib_id, p["part_id"])
                say(f"       mapped -> {lib_id}")
            else:
                say(f"       [skip] symbol {lib_id} not found in installed libraries; fix the name")


def main():
    store = KnowledgeStore(os.getenv("CP_DB") or DEFAULT_DB)
    try:
        lib = SymbolLibrary(default_symbol_dirs([os.getenv("CP_SYMBOL_DIR")]))
    except SynthesisError as e:
        lib = None
        print(f"[warn] {e}\n       Symbol mappings will be skipped until libraries are available.")
    seed(store, lib)
    print("\nCatalog the planner will offer to Nemotron:")
    for e in store.catalog():
        print(f"  {e['lib_id']} -> {e['part_id']}")


if __name__ == "__main__":
    main()
