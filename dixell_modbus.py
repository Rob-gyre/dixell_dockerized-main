"""Modbus communication for the Dixell XR77U."""

import json
import time
import math
from pathlib import Path

import logging
import minimalmodbus
from minimalmodbus import ModbusException


class DixellXR77U:
    def __init__(self, profile_path, serial_config=None):
        profile_path = Path(profile_path)

        with profile_path.open("r", encoding="utf-8-sig") as file:
            self.profile = json.load(file)

        if self.profile.get("protocol") != "modbus_rtu":
            raise ValueError("The profile must use Modbus RTU.")

        self.parameters = self.profile["parameters"]

        self.connection = self.profile["connection"].copy()
        self.connection.update(serial_config or {})

        self.instrument = None

    def connect(self):
        config = self.connection
        slave = int(config["slave"])

        if not 1 <= slave <= 247:
            raise ValueError("Modbus address must be between 1 and 247.")

        instrument = minimalmodbus.Instrument(
            config["port"],
            slave,
            mode=minimalmodbus.MODE_RTU,
        )

        try:
            instrument.serial.baudrate = int(config["baudrate"])
            instrument.serial.bytesize = 8
            instrument.serial.parity = config["parity"]
            instrument.serial.stopbits = int(config["stopbits"])
            instrument.serial.timeout = float(
                config.get("timeout", 0.6)
            )
            instrument.serial.write_timeout = 2.0

            instrument.clear_buffers_before_each_transaction = True
        except Exception:
            instrument.serial.close()
            raise

        self.instrument = instrument

    def close(self):
        if self.instrument is not None:
            self.instrument.serial.close()
            self.instrument = None

    def read_raw(self, name, attempts=3):
        if self.instrument is None:
            raise RuntimeError("Controller is not connected.")

        parameter = self.parameters[name]
        address = int(parameter["address"])
        register_type = parameter["register_type"]

        if register_type not in (
            "holding", "input", "coil", "discrete"
        ):
            raise ValueError(
                f"{name}: unsupported register type {register_type}"
            )

        for attempt in range(attempts):
            try:
                if register_type in ("coil", "discrete"):
                    function = 1 if register_type == "coil" else 2

                    return self.instrument.read_bit(
                        address,
                        functioncode=function,
                    )

                function = 3 if register_type == "holding" else 4

                return self.instrument.read_register(
                    address,
                    number_of_decimals=0,
                    functioncode=function,
                    signed=False,
                )

            except (OSError, ValueError, ModbusException):
                if attempt == attempts - 1:
                    raise

                time.sleep(0.1)

        raise RuntimeError("No read attempts were made.")

    def decode(self, name, raw):
        parameter = self.parameters[name]

        if parameter.get("units") == "bool":
            if raw == parameter.get("on_value", 1):
                return True

            if raw == parameter.get("off_value", 0):
                return False

            raise ValueError(
                f"{name}: unexpected Boolean value {raw}"
            )

        value = raw

        if parameter.get("signed", False) and raw >= 32768:
            value = raw - 65536

        scale = float(parameter.get("scale", 1))

        if scale <= 0:
            raise ValueError(f"{name}: scale must be positive.")

        operation = parameter.get("scale_operation", "divide")

        if operation == "divide":
            return value / scale

        if operation == "multiply":
            return value * scale

        raise ValueError(
            f"{name}: unknown scale operation {operation}"
        )

    def read_all(self):
        readings = {}
        raw_values = {}
        errors = {}

        for name, parameter in self.parameters.items():
            if parameter.get("address") is None:
                continue

            try:
                raw = self.read_raw(name)
                raw_values[name] = raw
                readings[name] = self.decode(name, raw)

            except (OSError, ValueError, ModbusException) as error:
                errors[name] = str(error)

        # Check live probe presence and the profile's temperature limits.
        for name, presence_name in (
            ("Pb1", None),
            ("Pb2", "P2P"),
            ("Pb3", "P3P"),
        ):
            if name not in readings:
                continue

            if (
                presence_name is not None
                and readings.get(presence_name) != 1
            ):
                readings.pop(name)
                errors[name] = (
                    f"Probe presence not confirmed by {presence_name}"
                )
                continue

            value = readings[name]
            parameter = self.parameters[name]
            minimum = parameter.get("minimum")
            maximum = parameter.get("maximum")

            if (
                not math.isfinite(value)
                or (minimum is not None and value < minimum)
                or (maximum is not None and value > maximum)
            ):
                readings.pop(name)
                errors[name] = "Temperature outside profile limits"

        return readings, raw_values, errors


    def write_parameter(self, name, value):
        if self.instrument is None:
            raise RuntimeError("Controller is not connected.")

        parameter = self.parameters[name]

        if parameter["register_type"] != "holding":
            raise ValueError(f"{name}: not a holding register")

        if (
            parameter.get("read_only", False)
            or parameter.get("access") == "read_only"
            or parameter.get("high_risk", False)
        ):
            raise ValueError(f"{name}: writing is not permitted")

        value = float(value)
        scale = float(parameter.get("scale", 1))

        if not math.isfinite(value):
            raise ValueError(f"{name}: value must be finite")

        if not math.isfinite(scale) or scale <= 0:
            raise ValueError(f"{name}: invalid scale")

        minimum = parameter.get("minimum")
        maximum = parameter.get("maximum")

        if minimum is not None and value < float(minimum):
            raise ValueError(f"{name}: below minimum {minimum}")

        if maximum is not None and value > float(maximum):
            raise ValueError(f"{name}: above maximum {maximum}")

        operation = parameter.get("scale_operation", "divide")

        if operation == "divide":
            encoded = value * scale
        elif operation == "multiply":
            encoded = value / scale
        else:
            raise ValueError(f"{name}: invalid scale operation")

        raw = round(encoded)

        if abs(encoded - raw) > 0.000001:
            raise ValueError(
                f"{name}: value cannot be represented exactly"
            )

        if parameter.get("signed", False):
            lower, upper = -32768, 32767
        else:
            lower, upper = 0, 65535

        if not lower <= raw <= upper:
            raise ValueError(f"{name}: value exceeds register range")

        choices = parameter.get("enum")
        if choices and str(raw) not in choices:
            raise ValueError(
                f"{name}: invalid choice {value}; "
                f"allowed values: {', '.join(choices)}"
            )
        
        raw = raw & 0xFFFF

        try:
            if self.read_raw(name) == raw:
                return True

            self.instrument.write_register(
                int(parameter["address"]),
                raw,
                number_of_decimals=0,
                functioncode=6,
                signed=False,
            )

            time.sleep(0.1)

            return self.read_raw(name) == raw

        except (OSError, ModbusException) as error:
            logging.getLogger(__name__).warning(
                "Dixell write or read-back failed for %s: %s",
                name,
                error,
            )
            return False