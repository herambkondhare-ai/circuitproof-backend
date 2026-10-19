"""
Offline self-test (no LLM, no KiCad needed):  python selftest.py
Checks the design parser, rail parsing, referee rules, netlist import and report output.
"""

import json
import os
import tempfile

import load_components
from circuitproof_core import FAIL, UNVERIFIED, CircuitReferee, parse_ohms, summarize
from design import DesignError, parse_design, rail_voltage
from knowledge_store import KnowledgeStore
from report_exporter import ReportExporter
from scan_project import design_from_netlist

NETLIST = """(export (version "E")
  (components
    (comp (ref "U1") (value "STM32F103C8T6") (libsource (lib "MCU_ST_STM32F1") (part "STM32F103C8Tx")))
    (comp (ref "R1") (value "4.7k") (libsource (lib "Device") (part "R"))))
  (nets
    (net (code "1") (name "/I2C_SDA") (node (ref "U1") (pin "43") (pinfunction "PB7")) (node (ref "R1") (pin "2")))
    (net (code "2") (name "+3V3") (node (ref "U1") (pin "24") (pinfunction "VDD")) (node (ref "R1") (pin "1")))
    (net (code "3") (name "GND") (node (ref "#PWR01") (pin "1")) (node (ref "U1") (pin "23") (pinfunction "VSS")))))"""

passed = 0


def check(cond, label):
    global passed
    if not cond:
        raise AssertionError(label)
    passed += 1
    print(f"  ok  {label}")


def main():
    tmp = tempfile.mkdtemp()
    os.environ["CP_DB"] = os.path.join(tmp, "test.db")
    load_components.main()
    store = KnowledgeStore(os.environ["CP_DB"])
    store.map_lib("MCU_ST_STM32F1:STM32F103C8Tx", "STM32F103C8T6")  # libraries may be absent here
    ref = CircuitReferee(store)
    print("\nRunning checks")

    check(rail_voltage("+3V3") == 3.3 and rail_voltage("3.3V") == 3.3 and rail_voltage("VCC_5V") == 5.0,
          "rail names parse to voltages")
    check(rail_voltage("GND") == 0 and rail_voltage("/SDA") is None and rail_voltage("VCC") is None,
          "ground and non-rail nets")
    check(parse_ohms("4k7") == 4700 and parse_ohms("10K") == 10000 and parse_ohms("2R2") == 2.2,
          "resistor values parse")

    try:
        parse_design({"components": [{"reference": "U1", "lib_id": "X:Y"}],
                      "nets": [{"net": "A", "connections": [("U1", "1"), ("U9", "2")]}]})
        check(False, "unknown component rejected")
    except DesignError as e:
        check(any("U9" in m for m in e.errors), "unknown component rejected")

    buggy = parse_design(json.load(open("demo/buggy_i2c.json"))["design"])
    f_bug = ref.audit(buggy)
    msgs = " | ".join(f.message for f in f_bug if f.status == FAIL)
    check("no pull-up" in msgs and "I2C_SCL" in msgs, "missing SCL pull-up caught")
    check("5 V" in msgs and "U2" in msgs, "sensor on 5 V rail caught")
    check(summarize(f_bug)["verdict"] == "FAIL", "buggy design verdict FAIL")

    good = parse_design(json.load(open("demo/good_i2c.json"))["design"])
    f_good = ref.audit(good)
    check(not [f for f in f_good if f.status == FAIL], "good design has no FAIL")
    check(any(f.status == UNVERIFIED and "max input" in f.message for f in f_good),
          "missing vin_max reported as UNVERIFIED, not assumed")
    check(all(f.data_verified is not True for f in f_good), "seed data is never reported as verified")

    scanned = design_from_netlist(NETLIST)
    check([c.reference for c in scanned.components] == ["U1", "R1"], "netlist import skips power symbols")
    sda = next(n for n in scanned.nets if n.net == "I2C_SDA")
    check(["U1", "PB7"] in sda.connections, "netlist import uses pin names and strips sheet path")

    path = ReportExporter(tmp).generate("selftest", f_bug, design=buggy)
    text = open(path, encoding="utf-8").read()
    check("Verdict: FAIL" in text and "seed data" in text, "report states verdict and data provenance")
    print(f"\nAll {passed} checks passed. Example report: {path}")


if __name__ == "__main__":
    main()
