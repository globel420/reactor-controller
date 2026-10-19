#include "HeaterController.h"

HeaterController::HeaterController() {
  // Initialize Variables
  _spPreheater = 0;
  _inPreheater = 0;
  _outPreheater = 0;
  _spLiquid = 0;
  _inLiquid = 0;
  _outLiquid = 0;
  _spGas = 0;
  _inGas = 0;
  _outGas = 0;

  _enabled = false;
  _windowStartTime = millis();

  // Initialize PIDs
  _pidPreheater = new PID(&_inPreheater, &_outPreheater, &_spPreheater, _kp, _ki, _kd, DIRECT);
  _pidLiquid = new PID(&_inLiquid, &_outLiquid, &_spLiquid, _kp, _ki, _kd, DIRECT);
  _pidGas = new PID(&_inGas, &_outGas, &_spGas, _kp, _ki, _kd, DIRECT);
}

void HeaterController::begin() {
  // Load the safe LOW value into each output latch before enabling its driver.
  digitalWrite(PIN_HEATER_FEEDSTOCK_PREHEATER, LOW);
  digitalWrite(PIN_HEATER_LIQUID_REACTOR, LOW);
  digitalWrite(PIN_HEATER_GAS_REACTOR, LOW);
  pinMode(PIN_HEATER_FEEDSTOCK_PREHEATER, OUTPUT);
  pinMode(PIN_HEATER_LIQUID_REACTOR, OUTPUT);
  pinMode(PIN_HEATER_GAS_REACTOR, OUTPUT);

  digitalWrite(PIN_HEATER_FEEDSTOCK_PREHEATER, LOW);
  digitalWrite(PIN_HEATER_LIQUID_REACTOR, LOW);
  digitalWrite(PIN_HEATER_GAS_REACTOR, LOW);

  // Limit output to 0-WINDOW_SIZE (time proportional)
  _pidPreheater->SetOutputLimits(0, WINDOW_SIZE);
  _pidLiquid->SetOutputLimits(0, WINDOW_SIZE);
  _pidGas->SetOutputLimits(0, WINDOW_SIZE);

  // Remain in manual mode until the state machine deliberately enables heat.
  _pidPreheater->SetMode(MANUAL);
  _pidLiquid->SetMode(MANUAL);
  _pidGas->SetMode(MANUAL);
}

void HeaterController::setSetpoints(float spPreheater, float spLiquid, float spGas) {
  _spPreheater = spPreheater;
  _spLiquid = spLiquid;
  _spGas = spGas;
}

void HeaterController::setEnabled(bool enabled) {
  // updateFSM() calls this every control cycle.  Resetting the PWM window on
  // every repeated "true" call would keep elapsed time near zero and turn any
  // positive PID output into effectively continuous heater power.
  if (enabled == _enabled) {
    if (!enabled) {
      digitalWrite(PIN_HEATER_FEEDSTOCK_PREHEATER, LOW);
      digitalWrite(PIN_HEATER_LIQUID_REACTOR, LOW);
      digitalWrite(PIN_HEATER_GAS_REACTOR, LOW);
    }
    return;
  }

  _enabled = enabled;
  if (!enabled) {
    // Force outputs off immediately
    digitalWrite(PIN_HEATER_FEEDSTOCK_PREHEATER, LOW);
    digitalWrite(PIN_HEATER_LIQUID_REACTOR, LOW);
    digitalWrite(PIN_HEATER_GAS_REACTOR, LOW);

    _pidPreheater->SetMode(MANUAL);
    _pidLiquid->SetMode(MANUAL);
    _pidGas->SetMode(MANUAL);
    _outPreheater = 0;
    _outLiquid = 0;
    _outGas = 0;
  } else {
    // Start one new window only on the disabled -> enabled transition.
    _windowStartTime = millis();
    _pidPreheater->SetMode(AUTOMATIC);
    _pidLiquid->SetMode(AUTOMATIC);
    _pidGas->SetMode(AUTOMATIC);
  }
}

void HeaterController::update(float tempPreheater, float tempLiquid, float tempGas, float tempGasExt) {
  if (!_enabled) {
    digitalWrite(PIN_HEATER_FEEDSTOCK_PREHEATER, LOW);
    digitalWrite(PIN_HEATER_LIQUID_REACTOR, LOW);
    digitalWrite(PIN_HEATER_GAS_REACTOR, LOW);
    return;
  }

  // Time Proportional Window Reset (Fix stale window drift)
  unsigned long now = millis();
  if (now - _windowStartTime >= WINDOW_SIZE || now < _windowStartTime) {
    _windowStartTime = now;
  }

  // Helper for thermal control with strict over-temp cutoff and anti-windup
  auto processZone = [&](float temp, double sp, PID *pid, double &outVal, int pin) {
    if (isnan(temp) || sp <= 0.0) {
      outVal = 0.0;
      digitalWrite(pin, LOW);
      return;
    }

    if (temp >= sp) {
      // STRICT OVER-TEMP CUTOFF: Shutdown heater immediately when at or above setpoint
      outVal = 0.0;
      digitalWrite(pin, LOW);
      // Clear integral windup
      pid->SetMode(MANUAL);
      pid->SetMode(AUTOMATIC);
    } else {
      // Standard PID Control when below setpoint
      pid->Compute();
      applyTimeProportional(pin, outVal);
    }
  };

  _inPreheater = tempPreheater;
  _inLiquid = tempLiquid;
  _inGas = tempGas;

  processZone(tempPreheater, _spPreheater, _pidPreheater, _outPreheater, PIN_HEATER_FEEDSTOCK_PREHEATER);
  processZone(tempLiquid, _spLiquid, _pidLiquid, _outLiquid, PIN_HEATER_LIQUID_REACTOR);

  // Gas Reactor External Over-temp Hard Safety Limit (110% of setpoint)
  if (!isnan(tempGasExt) && _spGas > 0.0 && tempGasExt > (_spGas * 1.10f)) {
    _outGas = 0.0;
    digitalWrite(PIN_HEATER_GAS_REACTOR, LOW);
    _pidGas->SetMode(MANUAL);
    _pidGas->SetMode(AUTOMATIC);
  } else {
    processZone(tempGas, _spGas, _pidGas, _outGas, PIN_HEATER_GAS_REACTOR);
  }
}

void HeaterController::applyTimeProportional(int pin, double output) {
  unsigned long now = millis();
  unsigned long elapsed = now - _windowStartTime;
  if (output > 0 && elapsed < (unsigned long)output) {
    digitalWrite(pin, HIGH);
  } else {
    digitalWrite(pin, LOW);
  }
}
