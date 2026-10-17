#include "HeaterController.h"
#include "SensorManager.h"
#include "SerialComms.h"
#include "WeightedAverage.h"
#include "config.h"
#include <Arduino.h>

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

const unsigned long HEARTBEAT_TIMEOUT = 5000; // 5 Seconds safety timeout

// --- Forward Declarations ---
void updateFSM(SensorData &data);
void checkSafety(SensorData &data);

void setup() {
  Serial.begin(SERIAL_BAUD);
  // while (!Serial)
  //   delay(10); // Wait for USB
  delay(1000); // Give time for Serial to stabilize
  // Serial.println("=== BOOT START ===");
  // Serial.println("BOOT");

  sensors.begin();
  // Serial.println("Sensors init done");

  heaters.begin();
  // Serial.println("Heaters init done");

  comms.begin();
  // Serial.println("Comms init done");

  startTime = millis();
  lastHeartbeatTime = millis();

  // Initial state
  currentState = STATE_STANDBY;
}

void loop() {
  unsigned long now = millis();

  // 1. Process Serial Commands (As fast as possible)
  Command cmd = comms.checkCommand();
  if (cmd.type != CMD_NONE) {
    lastHeartbeatTime = now; // Reset watchdog

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
      break;
    case CMD_SET_STATE:
      currentState = (ControlState)cmd.state;
      break;

    case CMD_HEARTBEAT:
      break;
    case CMD_TARE_LOADCELL:
      sensors.tareLoadCell();
      break;
    case CMD_CALIBRATE_LOADCELL:
      sensors.calibrateLoadCell(cmd.value);
      break;
    case CMD_PUMP_CONTROL:
      // Removed from Arduino, now handled by Raspberry Pi
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

void checkSafety(SensorData &data) {
  unsigned long now = millis();
  if (now - lastHeartbeatTime > HEARTBEAT_TIMEOUT) {
    if (currentState != STATE_STANDBY) {
      currentState = STATE_STANDBY;
      heaters.setEnabled(false);
      comms.sendError("WATCHDOG_HEARTBEAT_TIMEOUT");
    }
  }

  if ((!isnan(data.tempFeedstockPreheater) && data.tempFeedstockPreheater > MAX_TEMP_C_PREHEATER) ||
      (!isnan(data.tempLiquidReactor) && data.tempLiquidReactor > MAX_TEMP_C_LIQUID_REACTOR) ||
      (!isnan(data.tempGasReactorInt) && data.tempGasReactorInt > MAX_TEMP_C_GAS_REACTOR) ||
      (!isnan(data.tempGasReactorExt) && data.tempGasReactorExt > MAX_TEMP_C_GAS_REACTOR) ||
      (!isnan(data.tempElectronicsHousing) && data.tempElectronicsHousing > MAX_TEMP_C_HOUSING)) {
    if (currentState != STATE_STANDBY) {
      currentState = STATE_STANDBY;
      heaters.setEnabled(false);
      comms.sendError("THERMAL_SAFETY_LIMIT_EXCEEDED");
    }
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
  }
}
