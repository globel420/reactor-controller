#include <Arduino.h>
#include <AccelStepper.h>

// Define TB6600 connections (You can change these based on how you wire it)
#define ENA_PIN 8
#define STEP_PIN 9
#define DIR_PIN 10

// Create AccelStepper instance (1 = driver, STEP_PIN, DIR_PIN)
AccelStepper stepper(1, STEP_PIN, DIR_PIN);

String inputString = "";         // A String to hold incoming data

long targetSpeed = 0;
bool isRunning = false;
unsigned long lastPumpHeartbeat = 0;
const unsigned long PUMP_HEARTBEAT_TIMEOUT = 5000;

void parseCommand(String cmd) {
  cmd.trim();
  if (cmd.length() == 0) return;

  lastPumpHeartbeat = millis();

  if (cmd.startsWith("<START>")) {
    isRunning = true;
    Serial.println("OK:START");
  } 
  else if (cmd.startsWith("<STOP>")) {
    isRunning = false;
    Serial.println("OK:STOP");
  }
  else if (cmd.startsWith("<PING>") || cmd.startsWith("<HEARTBEAT>")) {
    Serial.println("OK:PONG");
  }
  else if (cmd.startsWith("<DIR:")) {
    String dirStr = cmd.substring(5, cmd.indexOf('>'));
    if (dirStr == "FWD") {
      targetSpeed = abs(targetSpeed);
      Serial.println("OK:DIR_FWD");
    } else if (dirStr == "REV") {
      targetSpeed = -abs(targetSpeed);
      Serial.println("OK:DIR_REV");
    }
  }
  else if (cmd.startsWith("<SPEED:")) {
    String speedStr = cmd.substring(7, cmd.indexOf('>'));
    long speedVal = speedStr.toInt();
    
    // Maintain current direction sign
    if (targetSpeed < 0) {
      targetSpeed = -speedVal;
    } else {
      targetSpeed = speedVal;
    }
    Serial.print("OK:SPEED_");
    Serial.println(speedVal);
  }
  else {
    Serial.print("ERR:UNKNOWN_CMD: ");
    Serial.println(cmd);
  }
}

void setup() {
  Serial.begin(9600);
  inputString.reserve(200);

  // Configure Enable Pin
  pinMode(ENA_PIN, OUTPUT);
  digitalWrite(ENA_PIN, LOW); // Usually LOW means enabled on TB6600

  // Configure Stepper parameters
  stepper.setMaxSpeed(4000.0);
  
  lastPumpHeartbeat = millis();
  Serial.println("Pump Controller Ready");
}

void loop() {
  // Read Serial
  while (Serial.available()) {
    char inChar = (char)Serial.read();
    if (inChar == '\n') {
      parseCommand(inputString);
      inputString = "";
    } else {
      inputString += inChar;
    }
  }

  // Safety Watchdog
  if (isRunning && (millis() - lastPumpHeartbeat > PUMP_HEARTBEAT_TIMEOUT)) {
    isRunning = false;
    targetSpeed = 0;
    Serial.println("ERR:WATCHDOG_HEARTBEAT_TIMEOUT");
  }

  // Apply State
  if (isRunning && targetSpeed != 0) {
    stepper.setSpeed(targetSpeed);
    stepper.runSpeed();
  }
}
