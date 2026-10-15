"""
CircuitProof Core Referee Engine (circuitproof_core.py)
Provides deterministic engineering rule checks (I2C voltage matching & SPI timing)
backed by the local SQLite KnowledgeStore.
"""

from knowledge_store import KnowledgeStore

class CircuitReferee:
    def __init__(self, db_name="circuitproof_notebook.db"):
        self.db = KnowledgeStore(db_name)
        self._seed_default_components()

    def _seed_default_components(self):
        """Seeds baseline electronic components into SQLite if not already present."""
        defaults = [
            ("STM32F103C8T6", "MCU", {"voh_min": 2.4, "vih_min": 2.0, "max_spi_hz": 18000000}, 1),
            ("BNO085", "Sensor", {"voh_min": 2.4, "vih_min": 1.4, "max_spi_hz": 3000000}, 1),
            ("W25Q128JV", "Flash Memory", {"voh_min": 2.4, "vih_min": 2.0, "max_spi_hz": 104000000}, 1)
        ]
        
        cursor = self.db.conn.cursor()
        for cid, cat, facts, verified in defaults:
            cursor.execute("SELECT 1 FROM components WHERE component_id = ?", (cid,))
            if not cursor.fetchone():
                self.db.save_component(cid, cat, facts, verified)

    def get_component_facts(self, component_id):
        return self.db.get_component(component_id)

    def check_i2c_levels(self, mcu_id, sensor_id):
        """Deterministic rule check: Driver VOH(min) >= Receiver VIH(min)"""
        mcu = self.get_component_facts(mcu_id)
        sensor = self.get_component_facts(sensor_id)

        if not mcu or not sensor:
            print(f"[Referee Error]: Could not find facts for {mcu_id} or {sensor_id} in database.")
            return False

        mcu_voh = mcu.get('voh_min', 2.4)
        sensor_vih = sensor.get('vih_min', 1.4)

        print(f"[Referee I2C Check]: {mcu_id} (VOH min: {mcu_voh}V) -> {sensor_id} (VIH min: {sensor_vih}V)")

        if mcu_voh < sensor_vih:
            print(f"  [VIOLATION]: Logic level mismatch! {mcu_id} cannot reliably drive {sensor_id}.")
            return False
        else:
            print(f"  [PASS]: I2C voltage levels are compatible ({mcu_voh}V >= {sensor_vih}V).")
            return True

    def check_spi_timing(self, mcu_id, peripheral_id, configured_spi_hz):
        """Deterministic rule check: Configured SPI Clock <= Peripheral Max SPI Clock"""
        mcu = self.get_component_facts(mcu_id)
        periph = self.get_component_facts(peripheral_id)

        if not mcu or not periph:
            print(f"[Referee Error]: Could not find facts for {mcu_id} or {peripheral_id} in database.")
            return False

        max_periph_hz = periph.get('max_spi_hz', 10000000) # default 10MHz if missing

        print(f"[Referee SPI Check]: Bus Clock: {configured_spi_hz / 1e6:.1f} MHz | {peripheral_id} Max Limit: {max_periph_hz / 1e6:.1f} MHz")

        if configured_spi_hz > max_periph_hz:
            violation = f"SPI Overclock Violation: {mcu_id} driving {peripheral_id} at {configured_spi_hz/1e6}MHz (Max allowed: {max_periph_hz/1e6}MHz)"
            print(f"  [VIOLATION]: {violation}")
            
            # Log mistake to knowledge base
            fix = f"Lower SPI prescaler on {mcu_id} to drop bus speed below {max_periph_hz/1e6} MHz."
            self.db.log_mistake(violation, fix)
            return False
        else:
            print(f"  [PASS]: SPI frequency is within safe timing margins.")
            return True

    def close(self):
        self.db.close()

if __name__ == "__main__":
    ref = CircuitReferee()
    print("Running Referee test suite...")
    ref.check_i2c_levels("STM32F103C8T6", "BNO085")
    print("-" * 40)
    # Test safe SPI speed
    ref.check_spi_timing("STM32F103C8T6", "BNO085", 2000000) 
    print("-" * 40)
    # Test violating SPI overclock speed
    ref.check_spi_timing("STM32F103C8T6", "BNO085", 10000000)
    ref.close()