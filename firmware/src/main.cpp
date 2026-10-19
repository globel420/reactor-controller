#include "HeaterController.h"
#include "SensorManager.h"
#include "SerialComms.h"
#include "WeightedAverage.h"
#include "config.h"
#include <Arduino.h>
#include <avr/wdt.h>

// --- Global Objects ---
SensorManager sensors;
HeaterController heaters;
SerialComms comms;
WeightedAverage wAvgGas; // Smooth gas reactor temp

// --- State Management ---
ControlState currentState = STATE_STANDBY;
unsigned long lastLoopTime = 0;
unsigned long lastTelemetryTime = 0;
unsigned long lastHeartbeatTime = 0;
unsigned long startTime = 0;
bool heartbeatSeen = false;
bool safetyFaultLatched = false;
bool haveSensorData = false;
SensorData latestSensorData = {};

const unsigned long HEARTBEAT_TIMEOUT = MCU_HEARTBEAT_TIMEOUT_MS;
const uint32_t HEATER_SENSOR_FAULT_MASK =
    ERR_TC_FEEDSTOCK_RESERVOIR | ERR_RTD_FEEDSTOCK_PREHEATER |
    ERR_RTD_LIQUID_REACTOR | ERR_RTD_GAS_REACTOR_INT |
    ERR_RTD_GAS_REACTOR_EXT | ERR_TC_ELECTRONICS_HOUSING;

// --- Forward Declarations ---
void updateFSM(SensorData &data);
void checkSafety(SensorData &data);
bool heartbeatIsFresh(unsigned long now);
bool safetyInputsAreClear(const SensorData &data);
void latchSafetyFault(const char *reason);

void setup() {
  // Drive SSR control pins LOW before Serial startup delays or sensor init.
  // Physical pull-downs/contactors are still required during bootloader reset.
  heaters.begin();
  heaters.setEnabled(false);

  Serial.begin(SERIAL_BAUD);
  // while (!Serial)
  //   delay(10); // Wait for USB
  delay(1000); // Give time for Serial to stabilize
  // Serial.println("=== BOOT START ===");
  // Serial.println("BOOT");

  sensors.begin();
  // Serial.println("Sensors init done");

  comms.begin();
  // Serial.println("Comms init done");

  startTime = millis();
  lastHeartbeatTime = millis();

  // Initial state
  currentState = STATE_STANDBY;

  wdt_enable(WDTO_2S);
}

void loop() {
  wdt_reset();
  unsigned long now = millis();

  // 1. Process Serial Commands (As fast as possible)
  Command cmd = comms.checkCommand();
  if (cmd.type != CMD_NONE) {
    switch (cmd.type) {
    case CMD_SET_TEMP:
      if (cmd.zone == 0)
        heaters.setSetpoints(cmd.value, heaters.getSetpointLiquid(),
                             heaters.getSetpointGas());
      if (cmd.zone == 1)
        heaters.setSetpoints(heaters.getSetpointPreheater(), cmd.value,
                             heaters.getSetpointGas());
      if (cmd.zone == 2)
        heaters.setSetpoints(heaters.getSetpointPreheater(),
                             heaters.getSetpointLiquid(), cmd.value);
      comms.sendAck(cmd.requestId, cmd.type);
      break;
    case CMD_SET_STATE:
      if (cmd.state == STATE_STANDBY) {
        heaters.setEnabled(false);
        if (!safetyFaultLatched ||
            (heartbeatIsFresh(now) && haveSensorData &&
             safetyInputsAreClear(latestSensorData))) {
          safetyFaultLatched = false;
          currentState = STATE_STANDBY;
          comms.sendAck(cmd.requestId, cmd.type);
        } else {
          currentState = STATE_FAULT;
          comms.sendNack(cmd.requestId, cmd.type,
                         "SAFETY_FAULT_RESET_REJECTED");
        }
      } else if (cmd.state == STATE_WARMUP || cmd.state == STATE_WORKING) {
        if (safetyFaultLatched || !heartbeatIsFresh(now) || !haveSensorData ||
            !safetyInputsAreClear(latestSensorData)) {
          currentState = STATE_FAULT;
          heaters.setEnabled(false);
          comms.sendNack(cmd.requestId, cmd.type,
                         "ACTIVE_STATE_REJECTED_UNSAFE");
        } else {
          currentState = (ControlState)cmd.state;
          comms.sendAck(cmd.requestId, cmd.type);
        }
      } else {
        // Remote ALARM/FAULT requests are always fail-safe and latched.
        safetyFaultLatched = true;
        currentState = (ControlState)cmd.state;
        heaters.setEnabled(false);
        comms.sendAck(cmd.requestId, cmd.type);
      }
      break;

    case CMD_HEARTBEAT:
      heartbeatSeen = true;
      lastHeartbeatTime = now;
      break;
    case CMD_TARE_LOADCELL:
      if (currentState != STATE_STANDBY) {
        comms.sendNack(cmd.requestId, cmd.type, "REQUIRES_STANDBY");
      } else {
        heaters.setEnabled(false);
      }
      if (currentState == STATE_STANDBY && sensors.tareLoadCell()) {
        comms.sendAck(cmd.requestId, cmd.type);
      } else if (currentState == STATE_STANDBY) {
        comms.sendNack(cmd.requestId, cmd.type, "LOAD_CELL_NOT_READY");
      }
      break;
    case CMD_CALIBRATE_LOADCELL:
      if (currentState != STATE_STANDBY) {
        comms.sendNack(cmd.requestId, cmd.type, "REQUIRES_STANDBY");
      } else {
        heaters.setEnabled(false);
      }
      if (currentState == STATE_STANDBY && !sensors.isLoadCellTared()) {
        comms.sendNack(cmd.requestId, cmd.type, "TARE_REQUIRED");
      } else if (currentState == STATE_STANDBY &&
          sensors.calibrateLoadCell(cmd.value)) {
        comms.sendAck(cmd.requestId, cmd.type);
      } else if (currentState == STATE_STANDBY) {
        comms.sendNack(cmd.requestId, cmd.type,
                       "CALIBRATION_READING_INVALID");
      }
      break;
    case CMD_PUMP_CONTROL:
      // Removed from Arduino, now handled by Raspberry Pi
      comms.sendNack(cmd.requestId, cmd.type, "UNSUPPORTED_COMMAND");
      break;
    case CMD_NONE:
      break;
    }
  }

  // 2. Fixed Interval Control Loop (10Hz)
  if (now - lastLoopTime >= LOOP_INTERVAL_MS) {
    lastLoopTime = now;

    // A. Read Sensors
    sensors.update();
    SensorData data = sensors.getLastReadings();
    latestSensorData = data;
    haveSensorData = true;

    // B. Check Safety (Hard Limits)
    checkSafety(data);

    // C. Update FSM (Logic for each state)
    updateFSM(data);

    // Calc Weighted PVs (70% Internal / 30% External) for Gas Reactor
    float instantGas = NAN;
    if (!isnan(data.tempGasReactorInt) && !isnan(data.tempGasReactorExt)) {
      instantGas = (data.tempGasReactorInt * 0.7) + (data.tempGasReactorExt * 0.3);
    } else if (!isnan(data.tempGasReactorExt)) {
      instantGas = data.tempGasReactorExt;
    } else if (!isnan(data.tempGasReactorInt)) {
      instantGas = data.tempGasReactorInt;
    }

    if (!isnan(instantGas)) {
      wAvgGas.add(instantGas);
    }

    float pvGas = wAvgGas.getAverage();
    if ((pvGas == 0.0f || isnan(pvGas)) && !isnan(instantGas)) {
      pvGas = instantGas;
    }

    // D. Update Heaters (PID calculation with external temp safety check)
    heaters.update(data.tempFeedstockPreheater, data.tempLiquidReactor, pvGas,
                   data.tempGasReactorExt);

    // E. Telemetry (1Hz)
    if (now - lastTelemetryTime >= 1000) {
      lastTelemetryTime = now;
      comms.sendTelemetry(data, heaters, currentState,
                          (now - startTime) / 1000);
    }
  }
}

bool heartbeatIsFresh(unsigned long now) {
  return heartbeatSeen && (now - lastHeartbeatTime <= HEARTBEAT_TIMEOUT);
}

bool safetyInputsAreClear(const SensorData &data) {
  if ((data.sensorStatus & HEATER_SENSOR_FAULT_MASK) != 0) {
    return false;
  }
  return !isnan(data.tempFeedstockPreheater) &&
         data.tempFeedstockPreheater < MAX_TEMP_C_PREHEATER &&
         !isnan(data.tempLiquidReactor) &&
         data.tempLiquidReactor < MAX_TEMP_C_LIQUID_REACTOR &&
         !isnan(data.tempGasReactorInt) &&
         data.tempGasReactorInt < MAX_TEMP_C_GAS_REACTOR &&
         !isnan(data.tempGasReactorExt) &&
         data.tempGasReactorExt < MAX_TEMP_C_GAS_REACTOR &&
         !isnan(data.tempElectronicsHousing) &&
         data.tempElectronicsHousing < MAX_TEMP_C_HOUSING;
}

void latchSafetyFault(const char *reason) {
  heaters.setEnabled(false);
  currentState = STATE_FAULT;
  if (!safetyFaultLatched) {
    safetyFaultLatched = true;
    comms.sendError(reason);
  }
}

void checkSafety(SensorData &data) {
  unsigned long now = millis();
  if ((currentState == STATE_WARMUP || currentState == STATE_WORKING) &&
      !heartbeatIsFresh(now) && now - lastHeartbeatTime > HEARTBEAT_TIMEOUT) {
    latchSafetyFault("WATCHDOG_HEARTBEAT_TIMEOUT");
  }

  if ((data.sensorStatus & HEATER_SENSOR_FAULT_MASK) != 0) {
    latchSafetyFault("PERSISTENT_SENSOR_FAULT");
  }

  if ((!isnan(data.tempFeedstockPreheater) && data.tempFeedstockPreheater >= MAX_TEMP_C_PREHEATER) ||
      (!isnan(data.tempLiquidReactor) && data.tempLiquidReactor >= MAX_TEMP_C_LIQUID_REACTOR) ||
      (!isnan(data.tempGasReactorInt) && data.tempGasReactorInt >= MAX_TEMP_C_GAS_REACTOR) ||
      (!isnan(data.tempGasReactorExt) && data.tempGasReactorExt >= MAX_TEMP_C_GAS_REACTOR) ||
      (!isnan(data.tempElectronicsHousing) && data.tempElectronicsHousing >= MAX_TEMP_C_HOUSING)) {
    latchSafetyFault("THERMAL_SAFETY_LIMIT_EXCEEDED");
  }
}

void updateFSM(SensorData &data) {
  switch (currentState) {
  case STATE_STANDBY:
    heaters.setEnabled(false);
    break;

  case STATE_WARMUP:
    heaters.setEnabled(true);
    break;

  case STATE_WORKING:
    heaters.setEnabled(true);
    break;

  case STATE_ALARM:
    heaters.setEnabled(false);
    break;

  case STATE_FAULT:
    heaters.setEnabled(false);
    break;

  default:
    latchSafetyFault("INVALID_CONTROL_STATE");
    break;
  }
}
