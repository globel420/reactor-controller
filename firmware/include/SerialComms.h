#ifndef SERIAL_COMMS_H
#define SERIAL_COMMS_H

#include "HeaterController.h"
#include "SensorManager.h"
#include <Arduino.h>
#include <ArduinoJson.h>

enum CommandType {
  CMD_NONE,
  CMD_SET_TEMP,
  CMD_SET_STATE,
  CMD_HEARTBEAT,
  CMD_TARE_LOADCELL,
  CMD_CALIBRATE_LOADCELL,
  CMD_PUMP_CONTROL
};
enum ControlState {
  STATE_STANDBY,
  STATE_WARMUP,
  STATE_WORKING,
  STATE_ALARM,
  STATE_FAULT
};

struct Command {
  CommandType type;
  uint16_t requestId;
  int zone; // 0=feedstock preheater, 1=liquid reactor, 2=gas reactor
  float value;
  int state;
  int dir;
};

class SerialComms {
public:
  SerialComms();
  void begin();
  Command checkCommand();
  void sendTelemetry(const SensorData &sensors, HeaterController &heaters,
                     ControlState state, unsigned long uptime);
  void sendAck(uint16_t requestId, CommandType type);
  void sendNack(uint16_t requestId, CommandType type, const char *reason);
  void sendError(const String &msg);

private:
  char _readBuffer[256];
  int _bufIndex;
  bool _discardUntilNewline;

  static const char *commandTypeName(CommandType type);
};

#endif
