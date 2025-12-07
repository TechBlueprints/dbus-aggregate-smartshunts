#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Settings loader for dbus-aggregate-smartshunts
Reads config.default.ini and config.ini (user overrides)
"""

import configparser
import logging
import sys
from pathlib import Path
from time import sleep

PATH_CONFIG_DEFAULT = "config.default.ini"
PATH_CONFIG_USER = "config.ini"

config = configparser.ConfigParser()
path = Path(__file__).parents[0]
default_config_file_path = str(path.joinpath(PATH_CONFIG_DEFAULT).absolute())
custom_config_file_path = str(path.joinpath(PATH_CONFIG_USER).absolute())

try:
    # Read default config first, then override with user config
    config.read([default_config_file_path, custom_config_file_path])
    
    # Ensure the [DEFAULT] section exists
    if "DEFAULT" not in config:
        logging.error(f'The config file is missing the [DEFAULT] section.')
        logging.error("Make sure the first line of the file is exactly: [DEFAULT]")
        sleep(60)
        sys.exit(1)

except configparser.MissingSectionHeaderError as error_message:
    logging.error(f'Error reading config files')
    logging.error("Make sure the first line is exactly: [DEFAULT]")
    logging.error(f"{error_message}\n")
    sleep(60)
    sys.exit(1)

# Map logging levels
LOGGING_LEVELS = {
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}

# Get logging level
if "LOGGING" not in config["DEFAULT"] or config["DEFAULT"]["LOGGING"].upper() not in LOGGING_LEVELS:
    logging.warning(f'Invalid "LOGGING" option. Using default level "INFO".')
    LOGGING_LEVEL = logging.INFO
else:
    LOGGING_LEVEL = LOGGING_LEVELS.get(config["DEFAULT"].get("LOGGING").upper())

# Set logging level
logging.basicConfig(level=LOGGING_LEVEL)

# List to store config errors
errors_in_config = []


# --------- Helper Functions ---------
def get_bool_from_config(group: str, option: str, default: bool = False) -> bool:
    """Get a boolean value from the config file."""
    return config[group].get(option, str(default)).lower() == "true"


def get_float_from_config(group: str, option: str, default_value: float = 0.0) -> float:
    """Get a float value from the config file."""
    value = config[group].get(option, default_value)
    if value == "":
        return default_value
    try:
        return float(value)
    except ValueError:
        errors_in_config.append(f"Invalid value '{value}' for option '{option}' in group '{group}'.")
        return default_value


def get_int_from_config(group: str, option: str, default_value: int = 0) -> int:
    """Get an integer value from the config file."""
    value = config[group].get(option, default_value)
    if value == "":
        return default_value
    try:
        return int(value)
    except ValueError:
        errors_in_config.append(f"Invalid value '{value}' for option '{option}' in group '{group}'.")
        return default_value


def get_list_from_config(group: str, option: str) -> list:
    """
    Get a comma-separated list from config.
    Can handle integers or strings.
    Returns empty list if empty or not specified.
    """
    try:
        value = config[group].get(option, "").strip()
        if not value:
            return []
        
        # Split by comma
        items = [item.strip() for item in value.split(",")]
        
        # Try to convert to int if possible, otherwise keep as string
        result = []
        for item in items:
            if item:  # Skip empty items
                item_stripped = item.strip('"\'')  # Remove quotes if present
                try:
                    result.append(int(item_stripped))
                except ValueError:
                    result.append(item_stripped)
        
        return result
    
    except Exception as e:
        logging.error(f"Error parsing list option '{option}': {e}")
        return []


# --------- Load Configuration Values ---------

# Device Configuration
DEVICE_NAME = config["DEFAULT"].get("DEVICE_NAME", "").strip()
if not DEVICE_NAME:
    DEVICE_NAME = "SmartShunts"

# Battery Specifications
# Capacity is always read from SmartShunt configuration registers (no config needed)

# Device Naming
DEVICE_NAME = config["DEFAULT"].get("DEVICE_NAME", "").strip()

# Note: BMS functionality (DEVICE_MODE, MAX_CHARGE_VOLTAGE, MAX_CHARGE_CURRENT, MAX_DISCHARGE_CURRENT) 
# has been removed from this project. This is now pure monitoring only.
# For BMS functionality, use the dbus-smartshunt-to-bms project instead.

# Temperature thresholds are now managed via UI switches, not config file

# Update Intervals
UPDATE_INTERVAL_FIND_DEVICES = get_int_from_config("DEFAULT", "UPDATE_INTERVAL_FIND_DEVICES", 10)  # Start at 10s, not 1s
MAX_UPDATE_INTERVAL_FIND_DEVICES = get_int_from_config("DEFAULT", "MAX_UPDATE_INTERVAL_FIND_DEVICES", 1800)

# Error Handling
SEARCH_TRIALS = get_int_from_config("DEFAULT", "SEARCH_TRIALS", 10)
READ_TRIALS = get_int_from_config("DEFAULT", "READ_TRIALS", 10)
TIME_BEFORE_RESTART = get_int_from_config("DEFAULT", "TIME_BEFORE_RESTART", 15)

# Logging
LOG_PERIOD = get_int_from_config("DEFAULT", "LOG_PERIOD", 300)

# Print errors and exit if there are any
if errors_in_config:
    logging.error("Errors in config file:")
    for error in errors_in_config:
        logging.error(f"|- {error}")
    logging.error("")
    logging.error("Please fix the errors in config.ini and restart the program.")
    sleep(60)
    sys.exit(1)

