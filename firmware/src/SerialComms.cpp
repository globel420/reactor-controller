#include "SerialComms.h"
#include <math.h>

namespace {
bool requiresRequestId(const char *commandName) {
  return strcmp(commandName, "SET_TEMP") == 0 ||
         strcmp(commandName, "SET_STATE") == 0 ||
         strcmp(commandName, "TARE_LOADCELL") == 0 ||
         strcmp(commandName, "CALIBRATE_LOADCELL") == 0;
}

bool validRequestId(JsonVariantConst value, uint16_t &requestId) {
  if (!value.is<long>()) {
    return false;
  }
  long parsed = value.as<long>();
  if (parsed < 1 || parsed > 65535L) {
    return false;
  }
  requestId = static_cast<uint16_t>(parsed);
  return true;
}
} // namespace

SerialComms::SerialComms() {
  _bufIndex = 0;
  _discardUntilNewline = false;
}

void SerialComms::begin() {
  // Serial begin handled in main/setup usually, but we can ensure it here if
  // needed relying on global Serial
}

Command SerialComms::checkCommand() {
  Command cmd = {};
  cmd.type = CMD_NONE;

  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      if (_discardUntilNewline) {
        _discardUntilNewline = false;
        _bufIndex = 0;
        sendError("Command Too Long");
        return cmd;
      }

      _readBuffer[_bufIndex] = '\0';
      _bufIndex = 0;

      // Parse JSON
      StaticJsonDocument<256> doc;
      DeserializationError error = deserializeJson(doc, _readBuffer);

      if (!error) {
        const char *typeStr = doc["cmd"];
        if (!typeStr) {
          sendError("Missing 'cmd' field");
          return cmd;
        }
        if (requiresRequestId(typeStr) &&
            !validRequestId(doc["rid"], cmd.requestId)) {
          sendError("Missing or invalid request id");
          return cmd;
        }
        if (strcmp(typeStr, "SET_TEMP") == 0) {
          if (!doc["zone"].is<int>() || !doc["val"].is<float>()) {
            sendNack(cmd.requestId, CMD_SET_TEMP, "INVALID_FIELDS");
            return cmd;
          }
          int zone = doc["zone"].as<int>();
          float value = doc["val"].as<float>();
          if (zone < 0 || zone > 2 || !isfinite(value) || value < 0.0f ||
              value > 300.0f) {
            sendNack(cmd.requestId, CMD_SET_TEMP, "OUT_OF_RANGE");
            return cmd;
          }
          cmd.type = CMD_SET_TEMP;
          cmd.zone = zone;
          cmd.value = value;
        } else if (strcmp(typeStr, "SET_STATE") == 0) {
          if (!doc["state"].is<int>()) {
            sendNack(cmd.requestId, CMD_SET_STATE, "INVALID_FIELDS");
            return cmd;
          }
          int state = doc["state"].as<int>();
          if (state < STATE_STANDBY || state > STATE_FAULT) {
            sendNack(cmd.requestId, CMD_SET_STATE, "OUT_OF_RANGE");
            return cmd;
          }
          cmd.type = CMD_SET_STATE;
          cmd.state = state;

        } else if (strcmp(typeStr, "HEARTBEAT") == 0) {
          cmd.type = CMD_HEARTBEAT;
        } else if (strcmp(typeStr, "TARE_LOADCELL") == 0) {
          cmd.type = CMD_TARE_LOADCELL;
        } else if (strcmp(typeStr, "CALIBRATE_LOADCELL") == 0) {
          if (!doc["val"].is<float>()) {
            sendNack(cmd.requestId, CMD_CALIBRATE_LOADCELL, "INVALID_FIELDS");
            return cmd;
          }
          float value = doc["val"].as<float>();
          if (!isfinite(value) || value <= 0.0f) {
            sendNack(cmd.requestId, CMD_CALIBRATE_LOADCELL, "OUT_OF_RANGE");
            return cmd;
          }
          cmd.type = CMD_CALIBRATE_LOADCELL;
          cmd.value = value;
        } else {
          uint16_t unknownRequestId = 0;
          if (validRequestId(doc["rid"], unknownRequestId)) {
            sendNack(unknownRequestId, CMD_NONE, "UNKNOWN_COMMAND");
          } else {
            sendError("Unknown Command");
          }
        }
        return cmd; // Return immediately on full command
      } else {
        sendError("JSON Parse Error");
      }
    } else {
      if (_discardUntilNewline) {
        continue;
      }
      if (_bufIndex < (int)sizeof(_readBuffer) - 1) {
        _readBuffer[_bufIndex++] = c;
      } else {
        // Discard the entire frame.  Never parse a truncated prefix as a
        // complete, potentially hazardous command.
        _discardUntilNewline = true;
        _bufIndex = 0;
      }
    }
  }
  return cmd;
}

const char *SerialComms::commandTypeName(CommandType type) {
  switch (type) {
  case CMD_SET_TEMP:
    return "SET_TEMP";
  case CMD_SET_STATE:
    return "SET_STATE";
  case CMD_HEARTBEAT:
    return "HEARTBEAT";
  case CMD_TARE_LOADCELL:
    return "TARE_LOADCELL";
  case CMD_CALIBRATE_LOADCELL:
    return "CALIBRATE_LOADCELL";
  case CMD_PUMP_CONTROL:
    return "PUMP_CONTROL";
  case CMD_NONE:
  default:
    return "UNKNOWN";
  }
}

void SerialComms::sendAck(uint16_t requestId, CommandType type) {
  StaticJsonDocument<128> doc;
  doc["ack"] = true;
  doc["rid"] = requestId;
  doc["cmd"] = commandTypeName(type);
  serializeJson(doc, Serial);
  Serial.println();
}

void SerialComms::sendNack(uint16_t requestId, CommandType type,
                           const char *reason) {
  StaticJsonDocument<192> doc;
  doc["ack"] = false;
  doc["rid"] = requestId;
  doc["cmd"] = commandTypeName(type);
  doc["error"] = reason;
  serializeJson(doc, Serial);
  Serial.println();
}

void SerialComms::sendTelemetry(const SensorData &sensors,
                                HeaterController &heaters,
                                ControlState state, unsigned long uptime) {
  StaticJsonDocument<640> doc;

  doc["uptime"] = uptime;
  doc["state"] = state;
  doc["config_hash"] = static_cast<uint32_t>(REACTOR_CONFIG_HASH);

  // Sensors
  JsonObject s = doc.createNestedObject("sensors");
  s["status"] = sensors.sensorStatus;
  if (sensors.loadCellReady && sensors.loadCellCalibrated &&
      !isnan(sensors.weightKg)) {
    s["weight"] = sensors.weightKg;
  } else {
    s["weight"] = nullptr;
  }
  s["load_cell_ready"] = sensors.loadCellReady;
  s["load_cell_calibrated"] = sensors.loadCellCalibrated;

  auto setTc = [&](const char *key, float val) {
    if (isnan(val)) {
      s[key] = nullptr;
    } else {
      s[key] = val;
    }
  };

  setTc("t_feed_res", sensors.tempFeedstockReservoir);
  setTc("t_feed_pre", sensors.tempFeedstockPreheater);
  setTc("t_liq_reac", sensors.tempLiquidReactor);
  setTc("t_gas_reac_int", sensors.tempGasReactorInt);
  setTc("t_gas_reac_ext", sensors.tempGasReactorExt);
  setTc("t_elec_housing", sensors.tempElectronicsHousing);
  s["h2"] = sensors.h2Millivolts;

  JsonObject errs = s.createNestedObject("tc_errors");
  errs["t_feed_res"] = sensors.errFeedstockReservoir;
  errs["t_feed_pre"] = sensors.errFeedstockPreheater;
  errs["t_liq_reac"] = sensors.errLiquidReactor;
  errs["t_gas_reac_int"] = sensors.errGasReactorInt;
  errs["t_gas_reac_ext"] = sensors.errGasReactorExt;
  errs["t_elec_housing"] = sensors.errElectronicsHousing;

  // Heaters
  JsonObject h = doc.createNestedObject("heaters");
  auto outputPercent = [](float windowMs) -> float {
    if (!isfinite(windowMs) || windowMs <= 0.0f) {
      return 0.0f;
    }
    if (windowMs >= WINDOW_SIZE) {
      return 100.0f;
    }
    return (windowMs * 100.0f) / WINDOW_SIZE;
  };
  h["feed_pre"] = outputPercent(heaters.getOutputPreheater());
  h["liq_reac"] = outputPercent(heaters.getOutputLiquid());
  h["gas_reac"] = outputPercent(heaters.getOutputGas());

  // Setpoints
  JsonObject sp = doc.createNestedObject("sp");
  sp["feed_pre"] = heaters.getSetpointPreheater();
  sp["liq_reac"] = heaters.getSetpointLiquid();
  sp["gas_reac"] = heaters.getSetpointGas();

  serializeJson(doc, Serial);
  Serial.println();
}

void SerialComms::sendError(const String &msg) {
  StaticJsonDocument<128> doc;
  doc["error"] = msg;
  serializeJson(doc, Serial);
  Serial.println();
}
