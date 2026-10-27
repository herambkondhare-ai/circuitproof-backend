# CircuitProof Audit Report: example_01_stm32_oled

**Generated:** 2026-10-27 21:12:18  
**Mode:** Generated from prompt  
**Planner model:** nvidia/nemotron-3-nano-omni-30b-a3b-reasoning via nvidia (remote)  
**Checker:** CircuitProof deterministic referee

## Verdict: NEEDS_REVIEW

❌ FAIL: 0 · ⚠️ WARN: 0 · ❔ UNVERIFIED: 3 · ✅ PASS: 2

## Findings

| Status | Rule | Finding | Fix | Data |
| --- | --- | --- | --- | --- |
| ❔ UNVERIFIED | part-data | No datasheet facts for U2 (SSD1306); voltage and timing rules for these parts cannot be checked. | Add the parts to the knowledge store (load_components.py). | — |
| ❔ UNVERIFIED | i2c | Could not check 3.3 V on 'I2C_SCL' against U1.PB6 (max input voltage), U2.C2P: no data. | Add io thresholds / vin_max for these parts. | — |
| ❔ UNVERIFIED | i2c | Could not check 3.3 V on 'I2C_SDA' against U1.PB7 (max input voltage), U2.C2N: no data. | Add io thresholds / vin_max for these parts. | — |
| ✅ PASS | supply | U1.VDD on +3V3 (3.3 V) is within 2.0–3.6 V. | — | seed data, not yet verified |
| ✅ PASS | decoupling | Rail '+3V3' has 8 decoupling capacitor(s) for 8 supply pin(s). | — | — |

## Generate → verify → repair history

| Round | Result | Problems sent back |
| --- | --- | --- |
| 1 | rejected | Net 'I2C_SCL': U2 (Display_Graphic:OLED-128O064D) has no pin 'CLK'. Valid pins: BS0, BS1, BS2, C1N, C1P, C2N, C2P, D/~{C}, D0, D1, D2, D3, D4, D5, D6, D7, E/~{RD}, GND, IREF, NC, R/~{W}, VBAT, VCC, VCOMH, VDD, VLSS, VSS, ~{CS}, ~{RES}; Net 'I2C_SDA': U2 (Display_Graphic:OLED-128O064D) has no pin 'DI/IO_0'. Valid pins: BS0, BS1, BS2, C1N, C1P, C2N, C2P, D/~{C}, D0, D1, D2, D3, D4, D5, D6, D7, E/~{RD}, GND, IREF, NC, R/~{W}, VBAT, VCC, VCOMH, VDD, VLSS, VSS, ~{CS}, ~{RES} |
| 2 | pass | none |

## KiCad ERC (kicad-cli)

Errors: 0 · Warnings: 0


## Components

| Ref | Symbol | Value |
| --- | --- | --- |
| U1 | MCU_ST_STM32F1:STM32F103C8Tx | STM32F103C8Tx |
| U2 | Display_Graphic:OLED-128O064D | SSD1306 |
| R3 | Device:R | 1k |
| C1 | Device:C | 100nF |
| C2 | Device:C | 100nF |
| C3 | Device:C | 100nF |
| C4 | Device:C | 100nF |
| C5 | Device:C | 100nF |
| C6 | Device:C | 22pF |
| C7 | Device:C | 22pF |
| C8 | Device:C | 100nF |
| D1 | Device:LED | LED |
| R4 | Device:R | 4.7k |
| R5 | Device:R | 4.7k |

## Files

- `schematic.kicad_sch`
