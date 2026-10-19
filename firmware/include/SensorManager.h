#ifndef SENSOR_MANAGER_H
#define SENSOR_MANAGER_H

#include "config.h"
#include <Adafruit_MAX31855.h>
#include <PwFusion_MAX31865.h>
#include <Arduino.h>
#include <SPI.h>
#include <stdint.h>
#include <HX711.h>


struct SensorData {
  // Temperatures (Celsius)
  float tempFeedstockReservoir;
  float tempFeedstockPreheater;
  float tempLiquidReactor;
  float tempGasReactorInt;
  float tempGasReactorExt;
  float tempElectronicsHousing;

  // Exact Error / Fault Bytes:
  // MAX31855: 0x01 Open, 0x02 Short GND, 0x04 Short VCC, 0x08 NaN/comm
  // MAX31865: 0x04 Voltage OOR, 0x08 RTDIn Low/Open, 0x10 RefIn Low/Open, 0x20 RefIn High, 0x40 Low Thresh, 0x80 High Thresh
  uint8_t errFeedstockReservoir;
  uint8_t errFeedstockPreheater;
  uint8_t errLiquidReactor;
  uint8_t errGasReactorInt;
  uint8_t errGasReactorExt;
  uint8_t errElectronicsHousing;

  // Analog Sensors
  float h2Millivolts;
  float weightKg;
  bool loadCellReady;
  bool loadCellCalibrated;

  // Status
  // 0 = OK. A bit is set immediately when no valid baseline exists, or after
  // 5 transient holds followed by a sixth consecutive failed read.
  uint32_t sensorStatus;
  bool sensorsHealthy; // aggregated simple flag
};

// Error Bits
#define ERR_TC_FEEDSTOCK_RESERVOIR (1 << 0)
#define ERR_RTD_FEEDSTOCK_PREHEATER (1 << 1)
#define ERR_RTD_LIQUID_REACTOR (1 << 2)
#define ERR_RTD_GAS_REACTOR_INT (1 << 3)
#define ERR_RTD_GAS_REACTOR_EXT (1 << 4)
#define ERR_TC_ELECTRONICS_HOUSING (1 << 5)
#define ERR_H2_SENSOR (1 << 6)
#define ERR_LOAD_CELL (1 << 7)

// Backward-compatible defines
#define ERR_TC_FEEDSTOCK_PREHEATER ERR_RTD_FEEDSTOCK_PREHEATER
#define ERR_TC_LIQUID_REACTOR ERR_RTD_LIQUID_REACTOR
#define ERR_TC_GAS_REACTOR_INT ERR_RTD_GAS_REACTOR_INT
#define ERR_TC_GAS_REACTOR_EXT ERR_RTD_GAS_REACTOR_EXT

class SensorManager {
public:
  SensorManager();
  void begin();
  void update();
  SensorData getLastReadings();

  bool tareLoadCell();
  bool calibrateLoadCell(float knownWeight);
  bool isLoadCellTared() const { return _loadCellTared; }

  static float calculateCvdTemperature(float rOhms);

private:
  // Thermocouple Objects (MAX31855 Software SPI)
  Adafruit_MAX31855 *_tcFeedstockReservoir;
  Adafruit_MAX31855 *_tcElectronicsHousing;

  // RTD Objects (PWFusion SEN-30203 Quad MAX31865 Hardware SPI)
  MAX31865 _rtdPreheater;
  MAX31865 _rtdLiquidReactor;
  MAX31865 _rtdGasReactorInt;
  MAX31865 _rtdGasReactorExt;

  HX711 _hx711;

  SensorData _currentData;

  // Debouncing / Transient Filter (6 channels: 0=FeedRes(TC), 1=FeedPre(RTD), 2=LiqReac(RTD), 3=GasInt(RTD), 4=GasExt(RTD), 5=ElecHousing(TC))
  float _lastValidTemp[6];
  bool _hasValidTemp[6];
  uint8_t _errorCount[6];
  uint8_t _lastExactErr[6];
  uint8_t _loadCellErrorCount;
  bool _hasValidWeight;
  bool _loadCellCalibrated;
  bool _loadCellTared;
};

#endif
