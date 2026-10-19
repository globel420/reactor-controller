#include "SensorManager.h"
#include <EEPROM.h>
#include <math.h>

const int EEPROM_CALIBRATION_ADDR = 0;
const uint32_t CALIBRATION_MAGIC = 0x1A2B3C4D;
const uint16_t CALIBRATION_VERSION = 1;
const uint8_t SENSOR_FAULT_THRESHOLD = 6;
const float MIN_CALIBRATION_SCALE_ABS = 1.0f;
const float MAX_CALIBRATION_SCALE_ABS = 1000000000.0f;

struct LoadCellCalibration {
  uint32_t magic;
  uint16_t version;
  float scale;
  long offset;
  uint32_t checksum;
};

uint32_t checksumMix(uint32_t hash, uint32_t value) {
  for (uint8_t shift = 0; shift < 32; shift += 8) {
    hash ^= static_cast<uint8_t>(value >> shift);
    hash *= 0x01000193UL;
  }
  return hash;
}

uint32_t calibrationChecksum(uint16_t version, float scale, long offset) {
  union {
    float value;
    uint32_t bits;
  } scaleBits = {scale};
  uint32_t hash = 0x811C9DC5UL;
  hash = checksumMix(hash, version);
  hash = checksumMix(hash, scaleBits.bits);
  return checksumMix(hash, static_cast<uint32_t>(offset));
}

bool calibrationScaleIsValid(float scale) {
  float magnitude = fabs(scale);
  return isfinite(scale) && magnitude >= MIN_CALIBRATION_SCALE_ABS &&
         magnitude <= MAX_CALIBRATION_SCALE_ABS;
}

bool calibrationRecordIsValid(const LoadCellCalibration &cal) {
  return cal.magic == CALIBRATION_MAGIC &&
         cal.version == CALIBRATION_VERSION &&
         calibrationScaleIsValid(cal.scale) &&
         cal.checksum ==
             calibrationChecksum(cal.version, cal.scale, cal.offset);
}

void writeCalibrationRecord(float scale, long offset) {
  LoadCellCalibration cal = {};
  cal.magic = CALIBRATION_MAGIC;
  cal.version = CALIBRATION_VERSION;
  cal.scale = scale;
  cal.offset = offset;
  cal.checksum = calibrationChecksum(cal.version, cal.scale, cal.offset);
  EEPROM.put(EEPROM_CALIBRATION_ADDR, cal);
}

SensorManager::SensorManager() {
  _currentData = {};
  _tcFeedstockReservoir =
      new Adafruit_MAX31855(PIN_TC_SOFT_SCK,
                           PIN_SPI_CS_TC_FEEDSTOCK_RESERVOIR,
                           PIN_TC_SOFT_MISO);
  _tcElectronicsHousing =
      new Adafruit_MAX31855(PIN_TC_SOFT_SCK,
                           PIN_SPI_CS_TC_ELECTRONICS_HOUSING,
                           PIN_TC_SOFT_MISO);

  for (int i = 0; i < 6; i++) {
    _lastValidTemp[i] = 25.0f; // Storage initializer; gated by _hasValidTemp.
    _hasValidTemp[i] = false;
    _errorCount[i] = 0;
    _lastExactErr[i] = 0;
  }
  _loadCellErrorCount = 0;
  _hasValidWeight = false;
  _loadCellCalibrated = false;
  _loadCellTared = false;
}

void SensorManager::begin() {
  // ATmega2560 Hardware SS (Pin 53) MUST be OUTPUT HIGH for SPI Master Mode
  pinMode(53, OUTPUT);
  digitalWrite(53, HIGH);

  // Initialize Thermocouple Chip Selects
  pinMode(PIN_SPI_CS_TC_FEEDSTOCK_RESERVOIR, OUTPUT);
  pinMode(PIN_SPI_CS_TC_ELECTRONICS_HOUSING, OUTPUT);
  digitalWrite(PIN_SPI_CS_TC_FEEDSTOCK_RESERVOIR, HIGH);
  digitalWrite(PIN_SPI_CS_TC_ELECTRONICS_HOUSING, HIGH);

  // Initialize RTD Chip Selects (SEN-30203 Quad Shield)
  pinMode(PIN_SPI_CS_RTD_FEEDSTOCK_PREHEATER, OUTPUT);
  pinMode(PIN_SPI_CS_RTD_LIQUID_REACTOR, OUTPUT);
  pinMode(PIN_SPI_CS_RTD_GAS_REACTOR_INT, OUTPUT);
  pinMode(PIN_SPI_CS_RTD_GAS_REACTOR_EXT, OUTPUT);
  digitalWrite(PIN_SPI_CS_RTD_FEEDSTOCK_PREHEATER, HIGH);
  digitalWrite(PIN_SPI_CS_RTD_LIQUID_REACTOR, HIGH);
  digitalWrite(PIN_SPI_CS_RTD_GAS_REACTOR_INT, HIGH);
  digitalWrite(PIN_SPI_CS_RTD_GAS_REACTOR_EXT, HIGH);

  // Initialize Hardware SPI for MAX31865 RTD shield
  SPI.begin();
  SPI.setClockDivider(SPI_CLOCK_DIV16);

  // Initialize RTD channels (3-wire PT100)
  _rtdPreheater.begin(PIN_SPI_CS_RTD_FEEDSTOCK_PREHEATER, RTD_3_WIRE, RTD_TYPE_PT100, SPI);
  _rtdLiquidReactor.begin(PIN_SPI_CS_RTD_LIQUID_REACTOR, RTD_3_WIRE, RTD_TYPE_PT100, SPI);
  _rtdGasReactorInt.begin(PIN_SPI_CS_RTD_GAS_REACTOR_INT, RTD_3_WIRE, RTD_TYPE_PT100, SPI);
  _rtdGasReactorExt.begin(PIN_SPI_CS_RTD_GAS_REACTOR_EXT, RTD_3_WIRE, RTD_TYPE_PT100, SPI);

  // Initialize Software SPI TC instances (auxiliary channels)
  if (!_tcFeedstockReservoir->begin())
    Serial.println("TC Feedstock Res init failed");
  if (!_tcElectronicsHousing->begin())
    Serial.println("TC Electronics Housing init failed");

  // Initialize Analog H2 Sensor Pin
  pinMode(PIN_H2_SENSOR, INPUT);

  // Initialize HX711
  Serial.println("Init: HX711 Load Cell...");
  _hx711.begin(PIN_HX711_DT, PIN_HX711_SCK);
  
  LoadCellCalibration cal = {};
  EEPROM.get(EEPROM_CALIBRATION_ADDR, cal);

  if (calibrationRecordIsValid(cal)) {
    Serial.println("Loading Load Cell calibration from EEPROM");
    _hx711.set_scale(cal.scale);
    _hx711.set_offset(cal.offset);
    _loadCellCalibrated = true;
  } else {
    // Never block boot waiting indefinitely for a missing HX711. Start with a
    // neutral calibration and require an explicit STANDBY tare/calibration.
    Serial.println("No valid load-cell calibration. STANDBY tare required.");
    _hx711.set_scale(1.f);
    _hx711.set_offset(0);
    _loadCellCalibrated = false;
  }
  _currentData.loadCellCalibrated = _loadCellCalibrated;
}

float SensorManager::calculateCvdTemperature(float rOhms) {
  if (isnan(rOhms) || rOhms <= 0.0f) {
    return NAN;
  }
  // Callendar-Van Dusen equation (IEC 751 / DIN EN 60751):
  // R(T) = R0 * (1 + A * T + B * T^2)
  // T = (-A + sqrt(A^2 - 4 * B * (1 - R / R0))) / (2 * B)
  const float R0 = 100.0f;
  const float A = 3.9083e-3f;
  const float B = -5.775e-7f;

  float discriminant = (A * A) - (4.0f * B * (1.0f - (rOhms / R0)));
  if (discriminant < 0.0f) {
    return NAN;
  }
  return (-A + sqrt(discriminant)) / (2.0f * B);
}

void SensorManager::update() {
  _currentData.sensorStatus = 0;

  // Helper with 5-read transient debouncer for MAX31855 thermocouples
  auto readTcDebounced = [&](Adafruit_MAX31855 *tc, const char *name, int idx, uint32_t errBit, uint8_t &exactErrOut) -> float {
    float t = tc->readCelsius();
    float internal = tc->readInternal();
    uint8_t err = tc->readError();

    bool isError = isnan(t) || isnan(internal) || err != 0 || (t == 0.0 && internal == 0.0);

    if (!isError) {
      // Valid reading: clear error counter and save last valid temp
      _errorCount[idx] = 0;
      _lastExactErr[idx] = 0;
      _lastValidTemp[idx] = t;
      _hasValidTemp[idx] = true;
      exactErrOut = 0;
      return t;
    }

    // Error detected
    // Saturate instead of allowing an 8-bit counter to wrap back to zero and
    // accidentally report a persistent fault as healthy after 256 reads.
    if (_errorCount[idx] < SENSOR_FAULT_THRESHOLD) {
      _errorCount[idx]++;
    }
    exactErrOut = (err != 0) ? err : 0x08; // 0x08 = NaN / Comm Fault
    _lastExactErr[idx] = exactErrOut;

    if (_hasValidTemp[idx] && _errorCount[idx] < SENSOR_FAULT_THRESHOLD) {
      // Transient error (1 to 5 reads): hold previous valid temp, do NOT set error bit yet
      return _lastValidTemp[idx];
    } else {
      // A stale value is not safe process feedback.  Mark the channel invalid;
      // the state machine will latch FAULT before the heater update runs.
      _currentData.sensorStatus |= errBit;
      return NAN;
    }
  };

  // Helper with 5-read transient debouncer for MAX31865 RTDs
  auto readRtdDebounced = [&](MAX31865 &rtd, const char *name, int idx, uint32_t errBit, uint8_t &exactErrOut) -> float {
    rtd.sample();
    uint8_t status = rtd.getStatus();
    float r = rtd.getResistance();
    float t = calculateCvdTemperature(r);

    bool isError = (status != 0) || isnan(t) || (r <= 0.0f);

    if (!isError) {
      // Valid reading: clear error counter and save last valid temp
      _errorCount[idx] = 0;
      _lastExactErr[idx] = 0;
      _lastValidTemp[idx] = t;
      _hasValidTemp[idx] = true;
      exactErrOut = 0;
      return t;
    }

    // Error detected
    if (_errorCount[idx] < SENSOR_FAULT_THRESHOLD) {
      _errorCount[idx]++;
    }
    exactErrOut = (status != 0) ? status : 0x08; // 0x08 = Comm Fault / NaN
    _lastExactErr[idx] = exactErrOut;

    if (_hasValidTemp[idx] && _errorCount[idx] < SENSOR_FAULT_THRESHOLD) {
      // Transient error (1 to 5 reads): hold previous valid temp
      return _lastValidTemp[idx];
    } else {
      // Do not feed the last-known temperature into PID after a persistent
      // fault; propagate NAN and let the safety state machine disable heat.
      _currentData.sensorStatus |= errBit;
      return NAN;
    }
  };

  // Read Auxiliary Thermocouples
  _currentData.tempFeedstockReservoir = readTcDebounced(_tcFeedstockReservoir, "FeedRes", 0, ERR_TC_FEEDSTOCK_RESERVOIR, _currentData.errFeedstockReservoir);
  _currentData.tempElectronicsHousing = readTcDebounced(_tcElectronicsHousing, "ElecHousing", 5, ERR_TC_ELECTRONICS_HOUSING, _currentData.errElectronicsHousing);

  // Read Core Process RTDs (PWFusion SEN-30203 Quad MAX31865)
  _currentData.tempFeedstockPreheater = readRtdDebounced(_rtdPreheater, "FeedPre", 1, ERR_RTD_FEEDSTOCK_PREHEATER, _currentData.errFeedstockPreheater);
  _currentData.tempLiquidReactor = readRtdDebounced(_rtdLiquidReactor, "LiqReac", 2, ERR_RTD_LIQUID_REACTOR, _currentData.errLiquidReactor);
  _currentData.tempGasReactorInt = readRtdDebounced(_rtdGasReactorInt, "GasReacInt", 3, ERR_RTD_GAS_REACTOR_INT, _currentData.errGasReactorInt);
  _currentData.tempGasReactorExt = readRtdDebounced(_rtdGasReactorExt, "GasReacExt", 4, ERR_RTD_GAS_REACTOR_EXT, _currentData.errGasReactorExt);

  // Read Analog H2 Sensor (MQ-8)
  int rawH2 = analogRead(PIN_H2_SENSOR);
  float voltageH2 = rawH2 * (5.0f / 1023.0f);
  // Report the measured analog signal honestly in millivolts. Converting an
  // uncalibrated MQ-8 voltage directly to ppm would create false precision.
  _currentData.h2Millivolts = voltageH2 * 1000.0f;

  // Read Load Cell
  if (_hx711.is_ready()) {
    // Read a single raw value scaled by current calibration factor
    float newWeight = _hx711.get_units(1);

    if (isfinite(newWeight)) {
      bool initializeWeight = !_hasValidWeight || isnan(_currentData.weightKg);
      _loadCellErrorCount = 0;
      _hasValidWeight = true;
      _currentData.loadCellReady = true;

      // Apply an Exponential Moving Average (EMA) filter to smooth the noise.
      if (initializeWeight || _currentData.weightKg == 0.0) {
        _currentData.weightKg = newWeight;
      } else {
        _currentData.weightKg =
            (0.2 * newWeight) + (0.8 * _currentData.weightKg);
      }
    } else {
      _currentData.loadCellReady = false;
      if (_loadCellErrorCount < SENSOR_FAULT_THRESHOLD) {
        _loadCellErrorCount++;
      }
    }
  } else {
    _currentData.loadCellReady = false;
    if (_loadCellErrorCount < SENSOR_FAULT_THRESHOLD) {
      _loadCellErrorCount++;
    }
  }

  _currentData.loadCellCalibrated = _loadCellCalibrated;
  if (!_loadCellCalibrated ||
      _loadCellErrorCount >= SENSOR_FAULT_THRESHOLD) {
    _currentData.sensorStatus |= ERR_LOAD_CELL;
    _currentData.weightKg = NAN;
  }

  // Compute aggregate health only after every sensor, including HX711.
  _currentData.sensorsHealthy = (_currentData.sensorStatus == 0);
}

SensorData SensorManager::getLastReadings() { return _currentData; }

bool SensorManager::tareLoadCell() {
  if (!_hx711.is_ready()) {
    return false;
  }
  // tare() internally waits until ready and averages 10 readings
  _hx711.tare(10); 
  _currentData.weightKg = 0.0; // Reset the EMA filter instantly
  _currentData.loadCellReady = true;
  _currentData.loadCellCalibrated = _loadCellCalibrated;
  _loadCellErrorCount = 0;
  _hasValidWeight = _loadCellCalibrated;
  _loadCellTared = true;

  // Tare updates the offset but cannot turn an uncalibrated raw signal into a
  // trusted kg measurement. Persist only when a validated scale already exists.
  if (_loadCellCalibrated && calibrationScaleIsValid(_hx711.get_scale())) {
    writeCalibrationRecord(_hx711.get_scale(), _hx711.get_offset());
  }
  return true;
}

bool SensorManager::calibrateLoadCell(float knownWeight) {
  if (_loadCellTared && isfinite(knownWeight) && knownWeight > 0.0f &&
      _hx711.is_ready()) {
    // get_value(10) internally waits until ready and averages 10 readings minus offset
    long reading = _hx711.get_value(10); 
    
    // Safeguard: If the reading is extremely small, it means the user forgot to put 
    // a weight on the scale before clicking calibrate. This prevents a scale factor 
    // near 0, which would multiply noise into thousands of Kg!
    if (abs(reading) > 500) {
      float scale = (float)reading / knownWeight;
      if (!calibrationScaleIsValid(scale)) {
        return false;
      }
      _hx711.set_scale(scale);
      _currentData.weightKg = knownWeight; // Snap EMA to known weight
      _currentData.loadCellReady = true;
      _currentData.loadCellCalibrated = true;
      _loadCellErrorCount = 0;
      _hasValidWeight = true;
      _loadCellCalibrated = true;

      writeCalibrationRecord(scale, _hx711.get_offset());
      return true;
    }
  }
  return false;
}
