import os

class Settings:
    # Check environment (defaulting to production if not specified)
    APP_ENV: str = os.getenv("APP_ENV", "production").lower()

    if APP_ENV == "development":
        _default_sensor_port = "socket://sensor_emulator:9999"
        _default_pump_port = "socket://pump_emulator:9998"
    else:
        _default_sensor_port = "/dev/sensor_arduino"
        _default_pump_port = "/dev/pump_arduino"

    SERIAL_PORT: str = os.getenv("SERIAL_PORT", _default_sensor_port)
    SERIAL_BAUD: int = 115200

    PUMP_PORT: str = os.getenv("PUMP_PORT", _default_pump_port)
    PUMP_BAUD: int = 9600

    DATABASE_URL: str = "sqlite:///./reactor_logs.db"
    BUZZER_RELAY_PIN: int = int(os.getenv("BUZZER_RELAY_PIN", "26"))
    
settings = Settings()
