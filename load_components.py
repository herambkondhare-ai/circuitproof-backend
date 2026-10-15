"""
Load Components Script (load_components.py)
Reads component facts and inserts them into the CircuitProof SQLite knowledge store.
"""

import json
from knowledge_store import KnowledgeStore

components_data = {
  "components": [
    {
      "component_id": "STM32F103C8T6",
      "category": "MCU",
      "voltage_rails": {
        "VDD_min": 2.0,
        "VDD_max": 3.6,
        "VDD_typ": 3.3
      },
      "pins": {
        "PB7": {
          "functions": ["I2C1_SDA", "GPIO"],
          "v_ih_min": 2.0,
          "v_il_max": 0.8,
          "v_oh_min": 2.4,
          "v_ol_max": 0.4
        }
      }
    },
    {
      "component_id": "BNO085",
      "category": "Sensor",
      "voltage_rails": {
        "VDD_min": 1.7,
        "VDD_max": 3.6,
        "VDD_typ": 1.8
      },
      "pins": {
        "SDA": {
          "functions": ["I2C_SDA"],
          "v_ih_min": 1.4,
          "v_il_max": 0.4,
          "v_oh_min": 0.0,
          "v_ol_max": 0.4
        }
      }
    },
    {
      "component_id": "PCA9306",
      "category": "Level Translator",
      "voltage_rails": {
        "VREF1_min": 1.0,
        "VREF1_max": 3.3,
        "VREF2_min": 1.8,
        "VREF2_max": 5.0
      },
      "pins": {
        "EN": {
          "functions": ["Enable"],
          "v_ih_min": 0.7,
          "v_il_max": 0.3
        }
      }
    },
    {
      "component_id": "W25Q128JV_Flash",
      "category": "Memory",
      "voltage_rails": {
        "VDD_min": 2.7,
        "VDD_max": 3.6
      },
      "spi_specs": {
        "max_clock_hz": 50000000,  # 50 MHz max SPI clock
        "t_su_ns": 5.0,           # Data setup time requirement
        "t_h_ns": 2.0             # Data hold time requirement
      }
    }
  ]
}

def load_into_db():
    db = KnowledgeStore()
    
    for comp in components_data["components"]:
        comp_id = comp["component_id"]
        category = comp["category"]
        db.save_component(
            component_id=comp_id,
            category=category,
            facts_dict=comp,
            verified=1
        )
        
    db.close()
    print("All structured component facts (including SPI specs) loaded into SQLite!")

if __name__ == "__main__":
    load_into_db()