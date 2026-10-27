# CircuitProof Audit Report: example_01_stm32_oled

**Generated:** 2026-10-27 22:06:01  
**Mode:** Generated from prompt  
**Planner model:** nvidia/nemotron-3-nano-omni-30b-a3b-reasoning via nvidia (remote)  
**Checker:** CircuitProof deterministic referee

## Verdict: NEEDS_REVIEW

❌ FAIL: 0 · ⚠️ WARN: 0 · ❔ UNVERIFIED: 2 · ✅ PASS: 2

## Findings

| Status | Rule | Finding | Fix | Data |
| --- | --- | --- | --- | --- |
| ❔ UNVERIFIED | i2c | Could not check 3.3 V on 'I2C_SCL' against U1.PB8 (max input voltage), J1.3: no data. | Add io thresholds / vin_max for these parts. | — |
| ❔ UNVERIFIED | i2c | Could not check 3.3 V on 'I2C_SDA' against U1.PB9 (max input voltage), J1.4: no data. | Add io thresholds / vin_max for these parts. | — |
| ✅ PASS | supply | U1.VDD on +3V3 (3.3 V) is within 2.0–3.6 V. | — | seed data, not yet verified |
| ✅ PASS | decoupling | Rail '+3V3' has 5 decoupling capacitor(s) for 5 supply pin(s). | — | — |

## Generate → verify → repair history

| Round | Result | Problems sent back |
| --- | --- | --- |
| 1 | pass | none |

## KiCad ERC (kicad-cli)

Errors: 0 · Warnings: 0


## Components

| Ref | Symbol | Value |
| --- | --- | --- |
| U1 | MCU_ST_STM32F1:STM32F103C8Tx | STM32F103C8Tx |
| J1 | Connector_Generic:Conn_01x04 | SSD1306 OLED module (1 GND, 2 VCC, 3 SCL, 4 SDA) |
| R3 | Device:R | 1k |
| C1 | Device:C | 100nF |
| C3 | Device:C | 22pF |
| C4 | Device:C | 22pF |
| D1 | Device:LED | LED |
| R4 | Device:R | 4.7k |
| R5 | Device:R | 4.7k |
| C2 | Device:C | 100nF |
| C5 | Device:C | 100nF |

## Files

- `schematic.kicad_sch`
