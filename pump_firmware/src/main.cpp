#include <AccelStepper.h>
#include <Arduino.h>
#include <avr/wdt.h>
#include <string.h>

#ifndef UNO_PIN_ENABLE
#define UNO_PIN_ENABLE 8
#endif
#ifndef UNO_PIN_STEP
#define UNO_PIN_STEP 9
#endif
#ifndef UNO_PIN_DIRECTION
#define UNO_PIN_DIRECTION 10
#endif
#ifndef MCU_HEARTBEAT_TIMEOUT_MS
#define MCU_HEARTBEAT_TIMEOUT_MS 3000
#endif
#ifndef REACTOR_CONFIG_HASH
#define REACTOR_CONFIG_HASH 0UL
#endif

#if MCU_HEARTBEAT_TIMEOUT_MS != 3000
#error "MCU_HEARTBEAT_TIMEOUT_MS must be exactly 3000"
#endif
#if UNO_PIN_ENABLE == UNO_PIN_STEP || UNO_PIN_ENABLE == UNO_PIN_DIRECTION || \
    UNO_PIN_STEP == UNO_PIN_DIRECTION
#error "Uno pump pins must be unique"
#endif

const long MAX_STEP_RATE = 4000;

// TB6600 enable is active-low: HIGH/open is the fail-safe disabled state.
AccelStepper stepper(AccelStepper::DRIVER, UNO_PIN_STEP, UNO_PIN_DIRECTION);

static char inputBuffer[64];
static uint8_t bufferIndex = 0;
static bool discardUntilNewline = false;

long speedMagnitude = 0;
int8_t directionSign = 1;
bool isRunning = false;
bool heartbeatSeen = false;
unsigned long lastPumpHeartbeat = 0;

void printConfigHash() {
  const char hexDigits[] = "0123456789ABCDEF";
  uint32_t value = static_cast<uint32_t>(REACTOR_CONFIG_HASH);
  for (int8_t shift = 28; shift >= 0; shift -= 4) {
    Serial.print(hexDigits[(value >> shift) & 0x0F]);
  }
}

bool heartbeatIsFresh(unsigned long now) {
  return heartbeatSeen &&
         (now - lastPumpHeartbeat <= MCU_HEARTBEAT_TIMEOUT_MS);
}

void disablePump() {
  isRunning = false;
  stepper.setSpeed(0.0f);
  digitalWrite(UNO_PIN_ENABLE, HIGH);
  digitalWrite(UNO_PIN_STEP, LOW);
}

bool parseSpeedCommand(const char *cmd, long &valueOut) {
  const char prefix[] = "<SPEED:";
  const size_t prefixLength = sizeof(prefix) - 1;
  const size_t length = strlen(cmd);
  if (length < prefixLength + 2 || strncmp(cmd, prefix, prefixLength) != 0 ||
      cmd[length - 1] != '>') {
    return false;
  }

  long value = 0;
  for (size_t i = prefixLength; i < length - 1; ++i) {
    char c = cmd[i];
    if (c < '0' || c > '9') {
      return false;
    }
    value = (value * 10L) + (c - '0');
    if (value > MAX_STEP_RATE) {
      return false;
    }
  }
  valueOut = value;
  return true;
}

void parseCommand(const char *cmd) {
  if (strcmp(cmd, "<PING>") == 0 || strcmp(cmd, "<HEARTBEAT>") == 0) {
    heartbeatSeen = true;
    lastPumpHeartbeat = millis();
    Serial.print("OK:PONG CONFIG_HASH=0x");
    printConfigHash();
    Serial.println();
    return;
  }

  if (strcmp(cmd, "<STOP>") == 0) {
    disablePump();
    Serial.println("OK:STOP");
    return;
  }

  if (strcmp(cmd, "<START>") == 0) {
    if (!heartbeatIsFresh(millis())) {
      Serial.println("ERR:START_REQUIRES_HEARTBEAT");
      return;
    }
    if (speedMagnitude <= 0) {
      Serial.println("ERR:START_REQUIRES_SPEED");
      return;
    }
    isRunning = true;
    stepper.setSpeed(directionSign * speedMagnitude);
    digitalWrite(UNO_PIN_ENABLE, LOW);
    Serial.println("OK:START");
    return;
  }

  if (strcmp(cmd, "<DIR:FWD>") == 0) {
    directionSign = 1;
    if (isRunning) {
      stepper.setSpeed(speedMagnitude);
    }
    Serial.println("OK:DIR_FWD");
    return;
  }
  if (strcmp(cmd, "<DIR:REV>") == 0) {
    directionSign = -1;
    if (isRunning) {
      stepper.setSpeed(-speedMagnitude);
    }
    Serial.println("OK:DIR_REV");
    return;
  }

  long requestedSpeed = 0;
  if (parseSpeedCommand(cmd, requestedSpeed)) {
    speedMagnitude = requestedSpeed;
    if (speedMagnitude == 0) {
      disablePump();
    } else if (isRunning) {
      stepper.setSpeed(directionSign * speedMagnitude);
    }
    Serial.print("OK:SPEED_");
    Serial.println(speedMagnitude);
    return;
  }

  Serial.print("ERR:UNKNOWN_OR_INVALID_CMD:");
  Serial.println(cmd);
}

void setup() {
  // Set output latches before changing pin direction so the active-low driver
  // cannot be enabled by software during setup.
  digitalWrite(UNO_PIN_ENABLE, HIGH);
  pinMode(UNO_PIN_ENABLE, OUTPUT);
  digitalWrite(UNO_PIN_STEP, LOW);
  pinMode(UNO_PIN_STEP, OUTPUT);
  digitalWrite(UNO_PIN_DIRECTION, LOW);
  pinMode(UNO_PIN_DIRECTION, OUTPUT);

  Serial.begin(9600);
  stepper.setMaxSpeed(MAX_STEP_RATE);
  disablePump();

  lastPumpHeartbeat = millis();
  Serial.println("Pump Controller Ready - DISABLED");
  wdt_enable(WDTO_2S);
}

void loop() {
  wdt_reset();

  while (Serial.available()) {
    char incoming = (char)Serial.read();
    if (incoming == '\r') {
      continue;
    }
    if (incoming == '\n') {
      if (discardUntilNewline) {
        discardUntilNewline = false;
        bufferIndex = 0;
        Serial.println("ERR:COMMAND_TOO_LONG");
        continue;
      }

      inputBuffer[bufferIndex] = '\0';
      if (bufferIndex > 0) {
        parseCommand(inputBuffer);
      }
      bufferIndex = 0;
    } else if (!discardUntilNewline) {
      if (bufferIndex < sizeof(inputBuffer) - 1) {
        inputBuffer[bufferIndex++] = incoming;
      } else {
        // Discard the complete oversized frame so a valid-looking prefix can
        // never execute by truncation.
        discardUntilNewline = true;
        bufferIndex = 0;
      }
    }
  }

  if (isRunning && !heartbeatIsFresh(millis())) {
    disablePump();
    heartbeatSeen = false;
    Serial.println("ERR:WATCHDOG_HEARTBEAT_TIMEOUT");
  }

  if (isRunning) {
    digitalWrite(UNO_PIN_ENABLE, LOW);
    stepper.runSpeed();
  } else {
    // Defense in depth: every idle pass reasserts the electrical safe state.
    digitalWrite(UNO_PIN_ENABLE, HIGH);
    digitalWrite(UNO_PIN_STEP, LOW);
  }
}
