# CircuitProof Audit Report: example_02_esp32_imu

**Generated:** 2026-10-27 21:17:06  
**Mode:** Generated from prompt  
**Planner model:** nvidia/nemotron-3-nano-omni-30b-a3b-reasoning via nvidia (remote)  
**Checker:** CircuitProof deterministic referee

## Verdict: NEEDS_REVIEW

❌ FAIL: 0 · ⚠️ WARN: 0 · ❔ UNVERIFIED: 3 · ✅ PASS: 3

## Findings

| Status | Rule | Finding | Fix | Data |
| --- | --- | --- | --- | --- |
| ❔ UNVERIFIED | part-data | No datasheet facts for U3 (AMS1117-3.3); voltage and timing rules for these parts cannot be checked. | Add the parts to the knowledge store (load_components.py). | — |
| ❔ UNVERIFIED | i2c | Could not check 3.3 V on 'I2C_SDA' against U1.IO4 (max input voltage), U2.SDA (max input voltage): no data. | Add io thresholds / vin_max for these parts. | — |
| ❔ UNVERIFIED | i2c | Could not check 3.3 V on 'I2C_SCL' against U1.IO5 (max input voltage), U2.SCL (max input voltage): no data. | Add io thresholds / vin_max for these parts. | — |
| ✅ PASS | supply | U1.VDD on +3V3 (3.3 V) is within 3.0–3.6 V. | — | seed data, not yet verified |
| ✅ PASS | supply | U2.VDD on +3V3 (3.3 V) is within 2.375–3.46 V. | — | seed data, not yet verified |
| ✅ PASS | decoupling | Rail '+3V3' has 2 decoupling capacitor(s) for 2 supply pin(s). | — | — |

## Generate → verify → repair history

| Round | Result | Problems sent back |
| --- | --- | --- |
| 1 | rejected | Net '+5V': U1 (RF_Module:ESP32-WROOM-32) has no pin 'VBAT'. Valid pins: EN, GND, IO0, IO12, IO13, IO14, IO15, IO16, IO17, IO18, IO19, IO2, IO21, IO22, IO23, IO25, IO26, IO27, IO32, IO33, IO34, IO35, IO4, IO5, NC, RXD0/IO3, SCK/CLK, SCS/CMD, SDI/SD1, SDO/SD0, SENSOR_VN, SENSOR_VP, SHD/SD2, SWP/SD3, TXD0/IO1, VDD; Net 'I2C_SDA': U1 (RF_Module:ESP32-WROOM-32) has no pin 'PB9'. Valid pins: EN, GND, IO0, IO12, IO13, IO14, IO15, IO16, IO17, IO18, IO19, IO2, IO21, IO22, IO23, IO25, IO26, IO27, IO32, IO33, IO34, IO35, IO4, IO5, NC, RXD0/IO3, SCK/CLK, SCS/CMD, SDI/SD1, SDO/SD0, SENSOR_VN, SENSOR_VP, SHD/SD2, SWP/SD3, TXD0/IO1, VDD; Net 'I2C_SCL': U1 (RF_Module:ESP32-WROOM-32) has no pin 'PB8'. Valid pins: EN, GND, IO0, IO12, IO13, IO14, IO15, IO16, IO17, IO18, IO19, IO2, IO21, IO22, IO23, IO25, IO26, IO27, IO32, IO33, IO34, IO35, IO4, IO5, NC, RXD0/IO3, SCK/CLK, SCS/CMD, SDI/SD1, SDO/SD0, SENSOR_VN, SENSOR_VP, SHD/SD2, SWP/SD3, TXD0/IO1, VDD; Net 'LED_DRIVE': U1 (RF_Module:ESP32-WROOM-32) has no pin 'PC13'. Valid pins: EN, GND, IO0, IO12, IO13, IO14, IO15, IO16, IO17, IO18, IO19, IO2, IO21, IO22, IO23, IO25, IO26, IO27, IO32, IO33, IO34, IO35, IO4, IO5, NC, RXD0/IO3, SCK/CLK, SCS/CMD, SDI/SD1, SDO/SD0, SENSOR_VN, SENSOR_VP, SHD/SD2, SWP/SD3, TXD0/IO1, VDD |
| 2 | pass | none |

## KiCad ERC (kicad-cli)

Errors: 0 · Warnings: 0


## Components

| Ref | Symbol | Value |
| --- | --- | --- |
| U1 | RF_Module:ESP32-WROOM-32 | ESP32-WROOM-32 |
| U2 | Sensor_Motion:MPU-6050 | MPU-6050 |
| U3 | Regulator_Linear:AMS1117-3.3 | AMS1117-3.3 |
| R2 | Device:R | 1k |
| D1 | Device:LED | LED |
| SW1 | Switch:SW_Push_Shielded | SW_Push_Shielded |
| J1 | Connector:Barrel_Jack | Barrel_Jack |
| R3 | Device:R | 4.7k |
| R4 | Device:R | 4.7k |
| C1 | Device:C | 100nF |
| C2 | Device:C | 100nF |

## Files

- `schematic.kicad_sch`
