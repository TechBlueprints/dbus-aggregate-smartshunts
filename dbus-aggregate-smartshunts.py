#!/usr/bin/env python3

"""
Service to aggregate multiple Victron SmartShunts into a single virtual battery monitor.

Designed for parallel battery banks where each battery has its own SmartShunt.
Combines current, voltage, and SoC readings to present a unified battery to the system.

Author: Based on dbus-aggregate-batteries by Dr-Gigavolt
License: MIT
"""

from gi.repository import GLib
import logging
import sys
import os
import platform
import dbus
import time as tt
from datetime import datetime as dt

# Add ext folder to sys.path
sys.path.insert(1, os.path.join(os.path.dirname(__file__), "ext", "velib_python"))

from vedbus import VeDbusService
from dbusmonitor import DbusMonitor
from settingsdevice import SettingsDevice

VERSION = "1.0.0"


class SystemBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SYSTEM)


class SessionBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SESSION)


def get_bus():
    return SessionBus() if "DBUS_SESSION_BUS_ADDRESS" in os.environ else SystemBus()


class DbusAggregateSmartShunts:
    
    def __init__(self, config, servicename="com.victronenergy.battery.aggregateshunts"):
        self.config = config
        self._shunts = []
        self._dbusConn = get_bus()
        self._searchTrials = 1
        self._readTrials = 1
        
        # Track if we're in the middle of an update to prevent recursion
        self._updating = False
        
        # Exponential backoff for device discovery
        self._device_search_interval = config['UPDATE_INTERVAL_FIND_DEVICES']  # Current interval
        self._initial_search_interval = config['UPDATE_INTERVAL_FIND_DEVICES']  # Store initial
        self._max_search_interval = config['MAX_UPDATE_INTERVAL_FIND_DEVICES']  # Max interval
        self._devices_stable_since = None  # Track when devices became stable
        self._last_device_count = 0  # Track device count changes
        
        # Flag to track if we've already logged TTG divergence warning (only log once per session)
        self._ttg_divergence_logged = False
        
        # Switch management for discovered shunts
        self.discovery_enabled = True  # Default to enabled
        self.shunt_switches = {}  # Maps service_name -> {'relay_id': int, 'enabled': bool}
        self.next_relay_id = 1  # Start at 1 (relay_0 is discovery switch)
        
        logging.info("### Initializing VeDbusService")
        self._dbusservice = VeDbusService(servicename, self._dbusConn, register=False)
        
        # Create management objects
        self._dbusservice.add_path("/Mgmt/ProcessName", __file__)
        self._dbusservice.add_path("/Mgmt/ProcessVersion", "Python " + platform.python_version())
        self._dbusservice.add_path("/Mgmt/Connection", "Virtual SmartShunt Aggregator")
        
        # Find an available device instance (check what's already in use)
        device_instance = self._find_available_device_instance()
        logging.info(f"### Using device instance: {device_instance}")
        
        # Create mandatory objects
        self._dbusservice.add_path("/DeviceInstance", device_instance)
        
        # Use ProductId and ProductName from first physical shunt (for VRM compatibility)
        # This ensures VRM recognizes the aggregate as the same type of device
        product_id = config.get('PRODUCT_ID', 0xA389)  # Default to SmartShunt if not found
        product_name = config.get('PRODUCT_NAME', 'SmartShunt 500A/50mV')  # Default to common SmartShunt model
        
        # CustomName can be overridden in config, but ProductName should match physical shunt
        custom_name = config['DEVICE_NAME'] if config['DEVICE_NAME'] else "SmartShunt Aggregate"
        
        self._dbusservice.add_path("/ProductId", product_id,
            gettextcallback=lambda a, x: f"0x{x:X}" if x and isinstance(x, int) else "")
        self._dbusservice.add_path("/ProductName", product_name)
        
        # Mirror firmware version from first physical shunt (must be integer like physical shunts)
        self._dbusservice.add_path("/FirmwareVersion", config['FIRMWARE_VERSION_INT'])
        # Hardware version: physical SmartShunts don't have one (empty), so we shouldn't either
        self._dbusservice.add_path("/HardwareVersion", [],
            gettextcallback=lambda a, x: "")
        self._dbusservice.add_path("/Connected", 1)
        self._dbusservice.add_path("/Serial", "AGGREGATE01")
        self._dbusservice.add_path("/CustomName", custom_name)
        
        # Create DC paths
        self._dbusservice.add_path("/Dc/0/Voltage", None, writeable=True,
                                    gettextcallback=lambda a, x: "{:.2f}V".format(x) if x is not None else "")
        self._dbusservice.add_path("/Dc/0/Current", None, writeable=True,
                                    gettextcallback=lambda a, x: "{:.2f}A".format(x) if x is not None else "")
        self._dbusservice.add_path("/Dc/0/Power", None, writeable=True,
                                    gettextcallback=lambda a, x: "{:.0f}W".format(x) if x is not None else "")
        self._dbusservice.add_path("/Dc/0/Temperature", None, writeable=True,
                                    gettextcallback=lambda a, x: "{:.0f}C".format(x) if x is not None else "")
        
        # Create capacity paths
        self._dbusservice.add_path("/Soc", None, writeable=True,
                                    gettextcallback=lambda a, x: "{:.1f}%".format(x) if x is not None else "0%")
        # Note: /Capacity and /InstalledCapacity are NOT included - these are BMS-specific paths
        # Physical SmartShunts don't expose these paths
        # For BMS functionality with these paths, use dbus-smartshunt-to-bms project
        
        self._dbusservice.add_path("/ConsumedAmphours", None,
                                    gettextcallback=lambda a, x: "{:.1f}Ah".format(x) if x is not None else "")
        self._dbusservice.add_path("/TimeToGo", None, writeable=True,
                                    gettextcallback=lambda a, x: "{:.0f}s".format(x) if x is not None and x != [] else "")
        
        # Pure SmartShunt monitoring only - no charge control paths
        # For BMS functionality (CVL/CCL/DCL, AllowToCharge/Discharge), use dbus-smartshunt-to-bms project
        logging.info("Monitor mode only - no charge control (pure SmartShunt aggregation)")
        
        # Alarms (pass through from physical shunts)
        self._dbusservice.add_path("/Alarms/Alarm", None, writeable=True)
        self._dbusservice.add_path("/Alarms/LowVoltage", None, writeable=True)
        self._dbusservice.add_path("/Alarms/HighVoltage", None, writeable=True)
        self._dbusservice.add_path("/Alarms/LowSoc", None, writeable=True)
        self._dbusservice.add_path("/Alarms/HighTemperature", None, writeable=True)
        self._dbusservice.add_path("/Alarms/LowTemperature", None, writeable=True)
        self._dbusservice.add_path("/Alarms/MidVoltage", 0)
        self._dbusservice.add_path("/Alarms/LowStarterVoltage", 0)
        self._dbusservice.add_path("/Alarms/HighStarterVoltage", 0)
        
        # History data (aggregated from physical shunts)
        self._dbusservice.add_path("/History/ChargeCycles", None, writeable=True)
        self._dbusservice.add_path("/History/TotalAhDrawn", None, writeable=True,
                                    gettextcallback=lambda a, x: "{:.1f}Ah".format(x) if x is not None else "")
        self._dbusservice.add_path("/History/MinimumVoltage", None, writeable=True,
                                    gettextcallback=lambda a, x: "{:.2f}V".format(x) if x is not None else "")
        self._dbusservice.add_path("/History/MaximumVoltage", None, writeable=True,
                                    gettextcallback=lambda a, x: "{:.2f}V".format(x) if x is not None else "")
        self._dbusservice.add_path("/History/TimeSinceLastFullCharge", None, writeable=True,
                                    gettextcallback=lambda a, x: "{:.0f}s".format(x) if x is not None else "")
        self._dbusservice.add_path("/History/AutomaticSyncs", None, writeable=True)
        self._dbusservice.add_path("/History/LowVoltageAlarms", None, writeable=True)
        self._dbusservice.add_path("/History/HighVoltageAlarms", None, writeable=True)
        self._dbusservice.add_path("/History/LastDischarge", None, writeable=True,
                                    gettextcallback=lambda a, x: "{:.1f}Ah".format(x) if x is not None else "")
        self._dbusservice.add_path("/History/AverageDischarge", None, writeable=True,
                                    gettextcallback=lambda a, x: "{:.1f}Ah".format(x) if x is not None else "")
        self._dbusservice.add_path("/History/ChargedEnergy", None, writeable=True,
                                    gettextcallback=lambda a, x: "{:.2f}kWh".format(x) if x is not None else "")
        self._dbusservice.add_path("/History/DischargedEnergy", None, writeable=True,
                                    gettextcallback=lambda a, x: "{:.2f}kWh".format(x) if x is not None else "")
        self._dbusservice.add_path("/History/FullDischarges", None, writeable=True)
        self._dbusservice.add_path("/History/DeepestDischarge", None, writeable=True,
                                    gettextcallback=lambda a, x: "{:.1f}Ah".format(x) if x is not None else "")
        def _empty_or_value_str(a, x):
            """Return empty string for None or empty arrays/lists, otherwise stringify"""
            logging.debug(f"_empty_or_value_str called with: type={type(x)}, value={x}, len={len(x) if hasattr(x, '__len__') else 'N/A'}")
            if x is None:
                return ""
            try:
                if len(x) == 0:
                    return ""
            except (TypeError, AttributeError):
                pass
            return str(x)
        
        def _empty_or_value_v(a, x):
            """Return empty string for None or empty arrays/lists, otherwise format as voltage"""
            logging.debug(f"_empty_or_value_v called with: type={type(x)}, value={x}")
            if x is None:
                return ""
            try:
                if len(x) == 0:
                    return ""
            except (TypeError, AttributeError):
                pass
            return "{:.2f}V".format(x)
        
        self._dbusservice.add_path("/History/LowStarterVoltageAlarms", [], writeable=True,
                                    gettextcallback=_empty_or_value_str)
        self._dbusservice.add_path("/History/HighStarterVoltageAlarms", [], writeable=True,
                                    gettextcallback=_empty_or_value_str)
        self._dbusservice.add_path("/History/MinimumStarterVoltage", [], writeable=True,
                                    gettextcallback=_empty_or_value_v)
        self._dbusservice.add_path("/History/MaximumStarterVoltage", [], writeable=True,
                                    gettextcallback=_empty_or_value_v)
        
        # Settings flags
        self._dbusservice.add_path("/Settings/HasTemperature", 1)
        self._dbusservice.add_path("/Settings/HasStarterVoltage", 0)
        self._dbusservice.add_path("/Settings/HasMidVoltage", 0)
        self._dbusservice.add_path("/Settings/RelayMode", [],
                                    gettextcallback=lambda a, x: "")
        
        # Group ID (not used for aggregate)
        self._dbusservice.add_path("/GroupId", [],
                                    gettextcallback=lambda a, x: "")
        
        # Relay state (not used for aggregate, but for compatibility)
        self._dbusservice.add_path("/Relay/0/State", [],
                                    gettextcallback=lambda a, x: "")
        
        # Additional DC paths for compatibility
        self._dbusservice.add_path("/Dc/0/MidVoltage", [],
                                    gettextcallback=lambda a, x: "")
        self._dbusservice.add_path("/Dc/0/MidVoltageDeviation", [],
                                    gettextcallback=lambda a, x: "")
        self._dbusservice.add_path("/Dc/1/Voltage", [],
                                    gettextcallback=lambda a, x: "")
        
        # Initialize D-Bus monitor
        logging.info("### Starting D-Bus monitor")
        self._init_dbusmonitor()
        
        # Note: /InstalledCapacity is NOT set - this is a BMS-specific path
        # Physical SmartShunts don't expose this path
        # For BMS functionality, use dbus-smartshunt-to-bms project
        
        # Track created device paths for cleanup
        self._device_paths = {}  # {instance: [list of paths]}
        
        # Create /Devices/0/* paths for the aggregate itself (like physical shunts do)
        self._dbusservice.add_path("/Devices/0/CustomName", custom_name)
        self._dbusservice.add_path("/Devices/0/DeviceInstance", device_instance)  # Use same instance as main device
        # Use firmware version from first detected shunt (integer format)
        self._dbusservice.add_path("/Devices/0/FirmwareVersion", 
            config.get('FIRMWARE_VERSION_INT'),
            gettextcallback=lambda a, x: f"v{(x >> 8) & 0xFF}.{x & 0xFF:x}" if x and isinstance(x, int) else "")
        self._dbusservice.add_path("/Devices/0/ProductId", product_id,
            gettextcallback=lambda a, x: f"0x{x:X}" if x and isinstance(x, int) else "")
        self._dbusservice.add_path("/Devices/0/ProductName", f"{product_name} (Aggregate)")
        self._dbusservice.add_path("/Devices/0/ServiceName", "com.victronenergy.battery.aggregateshunts")
        self._dbusservice.add_path("/Devices/0/VregLink", [],
            gettextcallback=lambda a, x: "")
        # Flag to identify this as a virtual aggregate (so dbus-smartshunt-to-bms can exclude it)
        self._dbusservice.add_path("/Devices/0/Virtual", 1)
        
        # Create /VEDirect/* paths to aggregate communication errors from all shunts
        self._dbusservice.add_path("/VEDirect/HexChecksumErrors", None)
        self._dbusservice.add_path("/VEDirect/HexInvalidCharacterErrors", None)
        self._dbusservice.add_path("/VEDirect/HexUnfinishedErrors", None)
        self._dbusservice.add_path("/VEDirect/TextChecksumErrors", None)
        self._dbusservice.add_path("/VEDirect/TextParseError", None)
        self._dbusservice.add_path("/VEDirect/TextUnfinishedErrors", None)
        
        # Store device_instance for later registration
        self._device_instance = device_instance
        
        # Add master discovery switch (relay_discovery)
        self._dbusservice.add_path('/SwitchableOutput/relay_discovery/Name', '* SmartShunt Discovery')
        self._dbusservice.add_path('/SwitchableOutput/relay_discovery/Type', 1)  # Toggle switch
        self._dbusservice.add_path('/SwitchableOutput/relay_discovery/State', 1, 
                                   writeable=True, onchangecallback=self._on_discovery_changed)
        self._dbusservice.add_path('/SwitchableOutput/relay_discovery/Status', 0x00)
        self._dbusservice.add_path('/SwitchableOutput/relay_discovery/Current', 0)
        self._dbusservice.add_path('/SwitchableOutput/relay_discovery/Settings/CustomName', '', writeable=True)
        self._dbusservice.add_path('/SwitchableOutput/relay_discovery/Settings/Type', 1, writeable=True)
        self._dbusservice.add_path('/SwitchableOutput/relay_discovery/Settings/ValidTypes', 2)
        self._dbusservice.add_path('/SwitchableOutput/relay_discovery/Settings/Function', 2, writeable=True)
        self._dbusservice.add_path('/SwitchableOutput/relay_discovery/Settings/ValidFunctions', 4)
        self._dbusservice.add_path('/SwitchableOutput/relay_discovery/Settings/Group', '', writeable=True)
        self._dbusservice.add_path('/SwitchableOutput/relay_discovery/Settings/ShowUIControl', 1, writeable=True)
        self._dbusservice.add_path('/SwitchableOutput/relay_discovery/Settings/PowerOnState', 1)
        
        # Add temperature threshold switches (using reserved relay IDs)
        # Defaults: 50°F (10°C) for cold limit, 105°F (40.5°C) for hot limit
        # Note: GUI slider is hardcoded 1-100, so we map slider values to actual temperature range:
        # - Both sliders: 1-100 maps to -50°C to 100°C (150°C range, ~1.5°C per slider step)
        DEFAULT_TEMP_LOW = 10.0   # 50°F / 10°C
        DEFAULT_TEMP_HIGH = 40.5  # 105°F / 40.5°C
        
        # Temperature range constants (both sliders use same range)
        TEMP_MIN = -50.0   # Minimum temperature threshold
        TEMP_MAX = 100.0   # Maximum temperature threshold
        
        # Store temperature range constants as instance variables
        self._temp_min = TEMP_MIN
        self._temp_max = TEMP_MAX
        
        # Convert default temperatures to slider positions (1-100 range)
        initial_low_slider = self._temp_to_slider(DEFAULT_TEMP_LOW)
        initial_high_slider = self._temp_to_slider(DEFAULT_TEMP_HIGH)
        
        # Low temp threshold (relay_temp_low)
        # Note: Using Type 2 (Dimmable/PWM) because GUI only displays Types 0, 1, and 2 in switches pane
        # GUI slider is hardcoded 1-100, so DimmingMin/Max are informational only
        # Initial display name with both C and F
        initial_low_f = DEFAULT_TEMP_LOW * 9/5 + 32
        self._default_temp_low = DEFAULT_TEMP_LOW
        self._default_temp_low_slider = initial_low_slider
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_low/Name', f'Cold Limit: {DEFAULT_TEMP_LOW:.0f}°C / {initial_low_f:.0f}°F')
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_low/Type', 2)  # Dimmable
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_low/State', 1, writeable=True, 
                                   onchangecallback=self._on_temp_low_state_changed)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_low/Status', 0x09, writeable=True)  # On status (no badge)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_low/Current', 0)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_low/Dimming', initial_low_slider, 
                                   writeable=True, onchangecallback=self._on_temp_low_changed)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_low/Measurement', DEFAULT_TEMP_LOW, writeable=True)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_low/Settings/CustomName', '', writeable=True)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_low/Settings/Type', 2, writeable=False)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_low/Settings/ValidTypes', 4)  # Only dimmable (bit 2)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_low/Settings/Function', 2, writeable=True)  # Manual
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_low/Settings/ValidFunctions', 4)  # Bit 2 = Manual only
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_low/Settings/Group', '', writeable=True)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_low/Settings/PowerOnState', 1)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_low/Settings/DimmingMin', 0.0)  # 0°C min
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_low/Settings/DimmingMax', 50.0)   # 50°C max
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_low/Settings/StepSize', 0.5)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_low/Settings/Decimals', 1)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_low/Settings/ShowUIControl', 1, writeable=True)
        
        # High temp threshold (relay_temp_high)
        # Note: Using Type 2 (Dimmable/PWM) because GUI only displays Types 0, 1, and 2 in switches pane
        # GUI slider is hardcoded 1-100, so DimmingMin/Max are informational only
        # Initial display name with both C and F
        initial_high_f = DEFAULT_TEMP_HIGH * 9/5 + 32
        self._default_temp_high = DEFAULT_TEMP_HIGH
        self._default_temp_high_slider = initial_high_slider
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_high/Name', f'Hot Limit: {DEFAULT_TEMP_HIGH:.0f}°C / {initial_high_f:.0f}°F')
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_high/Type', 2)  # Dimmable
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_high/State', 1, writeable=True,
                                   onchangecallback=self._on_temp_high_state_changed)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_high/Status', 0x09, writeable=True)  # On status (no badge)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_high/Current', 0)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_high/Dimming', initial_high_slider, 
                                   writeable=True, onchangecallback=self._on_temp_high_changed)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_high/Measurement', DEFAULT_TEMP_HIGH, writeable=True)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_high/Settings/CustomName', '', writeable=True)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_high/Settings/Type', 2, writeable=False)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_high/Settings/ValidTypes', 4)  # Only dimmable (bit 2)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_high/Settings/Function', 2, writeable=True)  # Manual
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_high/Settings/ValidFunctions', 4)  # Bit 2 = Manual only
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_high/Settings/Group', '', writeable=True)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_high/Settings/PowerOnState', 1)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_high/Settings/DimmingMin', 0.0)   # 0°C min
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_high/Settings/DimmingMax', 50.0)   # 50°C max
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_high/Settings/StepSize', 0.5)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_high/Settings/Decimals', 1)
        self._dbusservice.add_path('/SwitchableOutput/relay_temp_high/Settings/ShowUIControl', 1, writeable=True)
        
        # Store defaults for fallback use in aggregation
        self._default_temp_low = DEFAULT_TEMP_LOW
        self._default_temp_high = DEFAULT_TEMP_HIGH
        
        # Don't register yet - wait until main loop is ready
        # This prevents D-Bus timeout issues when DbusMonitor tries to call GetItems
        # before the main loop is running
        # Note: SmartShunt discovery timer is started in register() after settings are loaded
    
    def register(self):
        """Register the D-Bus service and device settings.
        
        Should be called after __init__ but before starting the main loop.
        This ensures the service is ready to handle D-Bus method calls.
        """
        logging.info("### Registering VeDbusService")
        self._dbusservice.register()
        
        # Register device in settings (for GUI device list)
        # This also restores discovery_enabled state from saved settings
        self._register_device_settings(self._device_instance)
        
        # Start searching for SmartShunts AFTER settings are loaded
        # This ensures discovery_enabled state is correct before first search
        GLib.timeout_add_seconds(self.config['UPDATE_INTERVAL_FIND_DEVICES'], self._find_smartshunts)
        
        logging.info("Service registered and ready")
    
    def _find_available_device_instance(self):
        """Find an available device instance number that's not already in use"""
        # Get all battery services and their device instances
        used_instances = set()
        try:
            for service in self._dbusConn.list_names():
                if "com.victronenergy.battery" in service:
                    try:
                        obj = self._dbusConn.get_object(service, '/DeviceInstance')
                        iface = dbus.Interface(obj, 'com.victronenergy.BusItem')
                        instance = iface.GetValue()
                        if instance is not None:
                            used_instances.add(int(instance))
                    except:
                        pass  # Service might not have DeviceInstance yet
        except Exception as e:
            logging.warning(f"Error checking used device instances: {e}")
        
        # Start from 100 and find first available
        for candidate in range(100, 300):
            if candidate not in used_instances:
                return candidate
        
        # Fallback to 100 if somehow all are taken (unlikely)
        logging.warning("All device instances 100-299 appear to be in use, using 100 anyway")
        return 100
    
    def _register_device_settings(self, device_instance):
        """Register device in com.victronenergy.settings for GUI device list"""
        try:
            # Create unique identifier for settings path (using serial number)
            unique_id = "aggregateshunts_AGGREGATE01"
            settings_path = f"/Settings/Devices/{unique_id}"
            
            # Create ClassAndVrmInstance setting
            class_and_vrm_instance = f"battery:{device_instance}"
            
            # Use SettingsDevice to register the device
            # This makes it appear in the GUI device list
            settings = {
                "ClassAndVrmInstance": [
                    f"{settings_path}/ClassAndVrmInstance",
                    class_and_vrm_instance,
                    0,
                    0,
                ],
                "DiscoveryEnabled": [
                    f"{settings_path}/DiscoveryEnabled",
                    1,  # Default: ON
                    0,
                    1,
                ],
                "TempLowSlider": [
                    f"{settings_path}/TempLowSlider",
                    self._default_temp_low_slider,  # Default slider position
                    1,  # Min
                    100,  # Max
                ],
                "TempHighSlider": [
                    f"{settings_path}/TempHighSlider",
                    self._default_temp_high_slider,  # Default slider position
                    1,  # Min
                    100,  # Max
                ],
            }
            
            # Initialize SettingsDevice (will create the settings if they don't exist)
            self._settings = SettingsDevice(
                self._dbusConn,
                settings,
                eventCallback=None,  # No callback needed for now
                timeout=10
            )
            
            logging.info(f"Registered device settings: {settings_path}/ClassAndVrmInstance = {class_and_vrm_instance}")
            
            # Restore discovery state from saved settings
            discovery_state = self._settings['DiscoveryEnabled']
            self._dbusservice['/SwitchableOutput/relay_discovery/State'] = discovery_state
            self.discovery_enabled = bool(discovery_state)
            if discovery_state:
                logging.info("Discovery enabled from saved settings")
            else:
                logging.info("Discovery disabled from saved settings")
                # Hide temperature threshold switches when discovery is disabled
                self._dbusservice['/SwitchableOutput/relay_temp_low/Settings/ShowUIControl'] = 0
                self._dbusservice['/SwitchableOutput/relay_temp_high/Settings/ShowUIControl'] = 0
            
            # Restore temperature slider values from saved settings
            saved_low_slider = self._settings['TempLowSlider']
            saved_high_slider = self._settings['TempHighSlider']
            
            if saved_low_slider != self._default_temp_low_slider:
                self._dbusservice['/SwitchableOutput/relay_temp_low/Dimming'] = saved_low_slider
                actual_temp = self._slider_to_temp(saved_low_slider)
                temp_f = actual_temp * 9/5 + 32
                self._dbusservice['/SwitchableOutput/relay_temp_low/Name'] = f'Cold Limit: {actual_temp:.0f}°C / {temp_f:.0f}°F'
                self._dbusservice['/SwitchableOutput/relay_temp_low/Measurement'] = actual_temp
                logging.info(f"Restored cold limit: {actual_temp:.1f}°C from slider {saved_low_slider}")
            
            if saved_high_slider != self._default_temp_high_slider:
                self._dbusservice['/SwitchableOutput/relay_temp_high/Dimming'] = saved_high_slider
                actual_temp = self._slider_to_temp(saved_high_slider)
                temp_f = actual_temp * 9/5 + 32
                self._dbusservice['/SwitchableOutput/relay_temp_high/Name'] = f'Hot Limit: {actual_temp:.0f}°C / {temp_f:.0f}°F'
                self._dbusservice['/SwitchableOutput/relay_temp_high/Measurement'] = actual_temp
                logging.info(f"Restored hot limit: {actual_temp:.1f}°C from slider {saved_high_slider}")
            
        except Exception as e:
            logging.error(f"Failed to register device settings: {e}")
            # Don't fail the whole service if settings registration fails
    
    def _init_dbusmonitor(self):
        """Initialize D-Bus monitor for SmartShunt services with reactive updates"""
        dummy = {"code": None, "whenToLog": "configChange", "accessLevel": None}
        
        monitorlist = {
            "com.victronenergy.battery": {
                "/ProductName": dummy,
                "/ProductId": dummy,
                "/CustomName": dummy,
                "/Serial": dummy,
                "/DeviceInstance": dummy,
                "/FirmwareVersion": dummy,
                "/HardwareVersion": dummy,
                "/Dc/0/Voltage": dummy,
                "/Dc/0/Current": dummy,
                "/Dc/0/Power": dummy,
                "/Dc/0/Temperature": dummy,
                "/Soc": dummy,
                "/ConsumedAmphours": dummy,
                "/TimeToGo": dummy,
                "/Connected": dummy,
                "/Alarms/Alarm": dummy,
                "/Alarms/LowVoltage": dummy,
                "/Alarms/HighVoltage": dummy,
                "/Alarms/LowSoc": dummy,
                "/Alarms/HighTemperature": dummy,
                "/Alarms/LowTemperature": dummy,
                # History data
                "/History/ChargeCycles": dummy,
                "/History/TotalAhDrawn": dummy,
                "/History/MinimumVoltage": dummy,
                "/History/MaximumVoltage": dummy,
                "/History/TimeSinceLastFullCharge": dummy,
                "/History/AutomaticSyncs": dummy,
                "/History/LowVoltageAlarms": dummy,
                "/History/HighVoltageAlarms": dummy,
                "/History/LastDischarge": dummy,
                "/History/AverageDischarge": dummy,
                "/History/ChargedEnergy": dummy,
                "/History/DischargedEnergy": dummy,
                "/History/FullDischarges": dummy,
                "/History/DeepestDischarge": dummy,
                "/History/MinimumStarterVoltage": dummy,
                "/History/MaximumStarterVoltage": dummy,
                # Relay
                "/Relay/0/State": dummy,
                # VE.Direct communication error counters
                "/VEDirect/HexChecksumErrors": dummy,
                "/VEDirect/HexInvalidCharacterErrors": dummy,
                "/VEDirect/HexUnfinishedErrors": dummy,
                "/VEDirect/TextChecksumErrors": dummy,
                "/VEDirect/TextParseError": dummy,
                "/VEDirect/TextUnfinishedErrors": dummy,
            }
        }
        
        # Set up reactive updates - callback fires whenever any monitored value changes
        # IMPORTANT: Exclude our own service from monitoring to prevent "GetItems failed" errors
        self._dbusmon = DbusMonitor(monitorlist, valueChangedCallback=self._on_value_changed,
                                     deviceAddedCallback=None, deviceRemovedCallback=None,
                                     ignoreServices=['com.victronenergy.battery.aggregateshunts'])
    
    def _on_value_changed(self, dbusServiceName, dbusPath, options, changes, deviceInstance):
        """
        Called whenever any monitored D-Bus value changes.
        This enables reactive updates instead of polling.
        """
        # Only trigger updates for our tracked SmartShunts, not our own service
        if "aggregate_shunts" in dbusServiceName:
            return
        
        # Only update for relevant paths (voltage, current, power, SoC, etc.)
        if dbusPath in ["/Dc/0/Voltage", "/Dc/0/Current", "/Dc/0/Power", "/Soc", 
                        "/ConsumedAmphours", "/TimeToGo", "/Dc/0/Temperature"]:
            # Schedule an update if we have shunts configured
            if self._shunts and not self._updating:
                # Use GLib.idle_add to avoid blocking the D-Bus callback
                GLib.idle_add(self._update)
    
    def _on_temp_low_state_changed(self, path: str, value):
        """Handle low temp threshold on/off state - reset to default when turned off"""
        new_state = bool(int(value) if isinstance(value, str) else value)
        logging.info(f"Low temp threshold state changed to {'On' if new_state else 'Off'}")
        
        if not new_state:
            # When turned off, reset to default
            try:
                self._dbusservice['/SwitchableOutput/relay_temp_low/Dimming'] = self._default_temp_low_slider
                self._dbusservice['/SwitchableOutput/relay_temp_low/Measurement'] = self._default_temp_low
                default_f = self._default_temp_low * 9/5 + 32
                self._dbusservice['/SwitchableOutput/relay_temp_low/Name'] = f'Cold Limit: {self._default_temp_low:.0f}°C / {default_f:.0f}°F'
                # Turn it back on automatically after resetting
                self._dbusservice['/SwitchableOutput/relay_temp_low/State'] = 1
            except Exception as e:
                logging.error(f"Failed to reset low temp to default: {e}")
        return True
    
    def _on_temp_high_state_changed(self, path: str, value):
        """Handle high temp threshold on/off state - reset to default when turned off"""
        new_state = bool(int(value) if isinstance(value, str) else value)
        logging.info(f"High temp threshold state changed to {'On' if new_state else 'Off'}")
        
        if not new_state:
            # When turned off, reset to default
            try:
                self._dbusservice['/SwitchableOutput/relay_temp_high/Dimming'] = self._default_temp_high_slider
                self._dbusservice['/SwitchableOutput/relay_temp_high/Measurement'] = self._default_temp_high
                default_f = self._default_temp_high * 9/5 + 32
                self._dbusservice['/SwitchableOutput/relay_temp_high/Name'] = f'Hot Limit: {self._default_temp_high:.0f}°C / {default_f:.0f}°F'
                # Turn it back on automatically after resetting
                self._dbusservice['/SwitchableOutput/relay_temp_high/State'] = 1
            except Exception as e:
                logging.error(f"Failed to reset high temp to default: {e}")
        return True
    
    def _temp_to_slider(self, temp: float) -> float:
        """Convert temperature (-50 to 100°C) to slider value (1-100)"""
        # Linear mapping: -50°C -> 1, 100°C -> 100
        return 1.0 + ((temp - self._temp_min) / (self._temp_max - self._temp_min)) * 99.0
    
    def _slider_to_temp(self, slider: float) -> float:
        """Convert slider value (1-100) to temperature (-50 to 100°C)"""
        # Linear mapping: 1 -> -50°C, 100 -> 100°C
        return self._temp_min + ((slider - 1.0) / 99.0) * (self._temp_max - self._temp_min)
    
    def _on_temp_low_changed(self, path: str, value):
        """Handle low temperature threshold changes - value is slider position (1-100)"""
        slider_value = float(value) if value is not None else 40.4  # Default to 10°C
        # Convert slider value to actual temperature
        actual_temp = self._slider_to_temp(slider_value)
        temp_f = actual_temp * 9/5 + 32
        logging.info(f"Low temp threshold slider changed to {slider_value:.1f} -> {actual_temp:.1f}°C / {temp_f:.1f}°F")
        # Update paths to display the value in the UI
        try:
            self._dbusservice['/SwitchableOutput/relay_temp_low/Measurement'] = actual_temp
            # Update Name to show the temperature in both C and F
            self._dbusservice['/SwitchableOutput/relay_temp_low/Name'] = f'Cold Limit: {actual_temp:.0f}°C / {temp_f:.0f}°F'
        except Exception as e:
            logging.error(f"Failed to update low temp measurement: {e}")
        # Save to persistent settings
        if hasattr(self, '_settings') and self._settings:
            self._settings['TempLowSlider'] = int(slider_value)
        return True
    
    def _on_temp_high_changed(self, path: str, value):
        """Handle high temperature threshold changes - value is slider position (1-100)"""
        slider_value = float(value) if value is not None else 57.35  # Default to 35°C
        # Convert slider value to actual temperature
        actual_temp = self._slider_to_temp(slider_value)
        temp_f = actual_temp * 9/5 + 32
        logging.info(f"High temp threshold slider changed to {slider_value:.1f} -> {actual_temp:.1f}°C / {temp_f:.1f}°F")
        # Update paths to display the value in the UI
        try:
            self._dbusservice['/SwitchableOutput/relay_temp_high/Measurement'] = actual_temp
            # Update Name to show the temperature in both C and F
            self._dbusservice['/SwitchableOutput/relay_temp_high/Name'] = f'Hot Limit: {actual_temp:.0f}°C / {temp_f:.0f}°F'
        except Exception as e:
            logging.error(f"Failed to update high temp measurement: {e}")
        # Save to persistent settings
        if hasattr(self, '_settings') and self._settings:
            self._settings['TempHighSlider'] = int(slider_value)
        return True
    
    def _on_discovery_changed(self, path: str, value):
        """Handle discovery switch state changes - show/hide all shunt switches"""
        new_enabled = bool(int(value) if isinstance(value, str) else value)
        
        logging.info(f"Discovery switch changed: new_enabled={new_enabled}, old={self.discovery_enabled}")
        
        # Save to persistent settings
        if hasattr(self, '_settings') and self._settings:
            self._settings['DiscoveryEnabled'] = 1 if new_enabled else 0
        
        if self.discovery_enabled != new_enabled:
            self.discovery_enabled = new_enabled
            
            # Update ShowUIControl for all shunt switches
            show_value = 1 if new_enabled else 0
            for service_name, switch_info in self.shunt_switches.items():
                relay_id = switch_info.get('relay_id')
                if relay_id:
                    output_path = f'/SwitchableOutput/relay_{relay_id}/Settings/ShowUIControl'
                    try:
                        self._dbusservice[output_path] = show_value
                        logging.debug(f"Set {output_path} = {show_value}")
                    except Exception as e:
                        logging.error(f"Failed to set {output_path}: {e}")
            
            # Also hide/show the temperature threshold switches
            try:
                self._dbusservice['/SwitchableOutput/relay_temp_low/Settings/ShowUIControl'] = show_value
                self._dbusservice['/SwitchableOutput/relay_temp_high/Settings/ShowUIControl'] = show_value
                logging.debug(f"Set temperature threshold switches ShowUIControl = {show_value}")
            except Exception as e:
                logging.error(f"Failed to set temperature threshold switches visibility: {e}")
            
            # Note: Discovery switch (relay_0) is NEVER hidden - users need it to re-enable discovery
            
            logging.info(f"SmartShunt Discovery {'enabled' if new_enabled else 'disabled'} - all switches {'visible' if new_enabled else 'hidden'}")
        
        return True
    
    def _on_shunt_switch_changed(self, service_name: str, path: str, value):
        """Handle individual shunt switch state changes - enable/disable from aggregation"""
        new_enabled = bool(int(value) if isinstance(value, str) else value)
        
        if service_name in self.shunt_switches:
            old_enabled = self.shunt_switches[service_name].get('enabled', True)
            
            if old_enabled != new_enabled:
                self.shunt_switches[service_name]['enabled'] = new_enabled
                # Save to settings
                self._set_shunt_enabled_setting(service_name, new_enabled)
                logging.info(f"Shunt switch changed: {service_name} -> {'enabled' if new_enabled else 'disabled'}")
                
                # Trigger aggregation update
                if not self._updating:
                    self._update_values()
        
        return True
    
    def _get_shunt_setting_key(self, service_name: str) -> str:
        """Convert service name to a valid settings key"""
        # e.g., com.victronenergy.battery.ttyS6 -> battery_ttyS6
        parts = service_name.split('.')
        if len(parts) >= 4:
            return f"{parts[2]}_{parts[3]}"
        return service_name.replace('.', '_')
    
    def _get_shunt_enabled_setting(self, service_name: str) -> bool:
        """Get shunt enabled state from settings"""
        try:
            key = self._get_shunt_setting_key(service_name)
            settings_path = f"/Settings/Devices/aggregateshunts/Shunt_{key}"
            settings_obj = self._dbusConn.get_object('com.victronenergy.settings', settings_path)
            settings_iface = dbus.Interface(settings_obj, 'com.victronenergy.BusItem')
            value = settings_iface.GetValue()
            return bool(value)
        except:
            # Setting doesn't exist yet - default to enabled
            return None
    
    def _set_shunt_enabled_setting(self, service_name: str, enabled: bool):
        """Save shunt enabled state to settings"""
        try:
            key = self._get_shunt_setting_key(service_name)
            settings_path = f"/Settings/Devices/aggregateshunts/Shunt_{key}"
            
            settings_obj = self._dbusConn.get_object('com.victronenergy.settings', '/Settings')
            settings_iface = dbus.Interface(settings_obj, 'com.victronenergy.Settings')
            # AddSetting(group, name, default, type, min, max)
            settings_iface.AddSetting(
                'Devices/aggregateshunts',
                f'Shunt_{key}',
                1,  # Default: enabled
                'i',  # integer
                0,
                1
            )
            
            # Now set the actual value
            shunt_obj = self._dbusConn.get_object('com.victronenergy.settings', settings_path)
            shunt_iface = dbus.Interface(shunt_obj, 'com.victronenergy.BusItem')
            shunt_iface.SetValue(1 if enabled else 0)
            
            logging.debug(f"Saved shunt {service_name} enabled={enabled} to settings")
        except Exception as e:
            logging.error(f"Failed to save shunt setting: {e}")
    
    def _create_shunt_switch(self, service_name: str, custom_name: str):
        """Create a switch for a discovered SmartShunt
        
        Uses context manager to emit ItemsChanged signal so GUI picks up new switches.
        """
        # Check if switch already exists
        if service_name in self.shunt_switches:
            return
        
        # Assign relay_id
        relay_id = self.next_relay_id
        self.next_relay_id += 1
        
        # Check settings for persisted enabled state
        persisted_enabled = self._get_shunt_enabled_setting(service_name)
        enabled = persisted_enabled if persisted_enabled is not None else True
        
        # Store switch info
        self.shunt_switches[service_name] = {
            'relay_id': relay_id,
            'enabled': enabled,
            'custom_name': custom_name
        }
        
        # Save to settings (creates setting if needed)
        self._set_shunt_enabled_setting(service_name, enabled)
        
        output_path = f'/SwitchableOutput/relay_{relay_id}'
        show_ui = 1 if self.discovery_enabled else 0
        
        # Create switch paths using context manager to emit ItemsChanged signal
        with self._dbusservice as ctx:
            ctx.add_path(f'{output_path}/Name', custom_name)
            ctx.add_path(f'{output_path}/Type', 1)  # Toggle switch
            ctx.add_path(f'{output_path}/State', 1 if enabled else 0, 
                         writeable=True, onchangecallback=lambda p, v: self._on_shunt_switch_changed(service_name, p, v))
            ctx.add_path(f'{output_path}/Status', 0x00)
            ctx.add_path(f'{output_path}/Current', 0)
            
            # Settings - match relay_0 structure
            ctx.add_path(f'{output_path}/Settings/CustomName', '', writeable=True)
            ctx.add_path(f'{output_path}/Settings/Type', 1, writeable=True)
            ctx.add_path(f'{output_path}/Settings/ValidTypes', 2)
            ctx.add_path(f'{output_path}/Settings/Function', 2, writeable=True)
            ctx.add_path(f'{output_path}/Settings/ValidFunctions', 4)
            ctx.add_path(f'{output_path}/Settings/Group', '', writeable=True)
            ctx.add_path(f'{output_path}/Settings/ShowUIControl', show_ui, writeable=True)
            ctx.add_path(f'{output_path}/Settings/PowerOnState', 1 if enabled else 0)
        
        logging.info(f"Created switch for {custom_name} ({service_name}) at {output_path}, enabled={enabled}")
    
    def _find_smartshunts(self):
        """Search for SmartShunt services on D-Bus"""
        # Skip discovery if disabled
        if not self.discovery_enabled:
            logging.debug("Discovery disabled, skipping SmartShunt search")
            GLib.timeout_add_seconds(int(self._device_search_interval), self._find_smartshunts)
            return False
        
        logging.info(f"Searching for SmartShunts: Trial #{self._searchTrials}")
        
        found_shunts = []
        
        try:
            for service in self._dbusConn.list_names():
                if "com.victronenergy.battery" in service:
                    product_name = self._dbusmon.get_value(service, "/ProductName")
                    
                    # Check if this is a SmartShunt (but not a virtual aggregate)
                    if product_name and "SmartShunt" in product_name:
                        # Skip if this is a virtual/aggregate device (to avoid aggregating ourselves)
                        is_virtual = self._dbusmon.get_value(service, "/Devices/0/Virtual")
                        if is_virtual == 1:
                            logging.debug(f"Skipping virtual device: {service}")
                            continue
                        
                        device_instance = self._dbusmon.get_value(service, "/DeviceInstance")
                        custom_name = self._dbusmon.get_value(service, "/CustomName")
                        
                        # Add all SmartShunts (filtering via switches instead of config)
                        found_shunts.append({
                            'service': service,
                            'instance': device_instance,
                            'name': custom_name or f"Shunt {device_instance}",
                            'product': product_name
                        })
                        logging.info(f"|- Found: {custom_name} [{device_instance}] - {product_name}")
        
        except Exception as e:
            logging.error(f"Error searching for SmartShunts: {e}")
        
        # Check if device count changed (new device appeared or disappeared)
        if self._shunts and len(found_shunts) != len(self._shunts):
            logging.warning(f"Device count changed: {len(self._shunts)} -> {len(found_shunts)}")
            logging.warning(f"Resetting search interval to {self._initial_search_interval}s")
            self._device_search_interval = self._initial_search_interval
            self._devices_stable_since = None
        
        # Check if we found any SmartShunts
        if len(found_shunts) > 0:
            # Check if this is initial discovery or device count changed
            if not self._shunts or len(found_shunts) != self._last_device_count:
                self._shunts = found_shunts
                self._last_device_count = len(found_shunts)
                logging.info(f"✓ Found {len(found_shunts)} SmartShunt(s) to aggregate")
                
                # Create switches for newly discovered shunts
                for shunt in found_shunts:
                    service_name = shunt['service']
                    if service_name not in self.shunt_switches:
                        self._create_shunt_switch(service_name, shunt['name'])
                
                # Update /Devices/* paths to show info about aggregated SmartShunts
                self._update_device_paths(found_shunts)
                
                # Start device stability timer
                self._devices_stable_since = tt.time()
                
                # Do an initial update immediately
                self._update()
                
                # Set up periodic logging (every LOG_PERIOD seconds)
                if self.config['LOG_PERIOD'] > 0:
                    GLib.timeout_add_seconds(self.config['LOG_PERIOD'], self._periodic_log)
            else:
                # Devices haven't changed - check if stable for 30 seconds
                if self._devices_stable_since and (tt.time() - self._devices_stable_since) >= 30:
                    # Apply exponential backoff
                    old_interval = self._device_search_interval
                    self._device_search_interval = min(
                        self._device_search_interval * 2,
                        self._max_search_interval
                    )
                    
                    if self._device_search_interval != old_interval:
                        logging.info(f"Devices stable for 30s, increasing search interval: {old_interval}s -> {self._device_search_interval}s")
                    
                    # Reset stability timer for next backoff cycle
                    self._devices_stable_since = tt.time()
            
            # Schedule next search with current interval
            GLib.timeout_add_seconds(int(self._device_search_interval), self._find_smartshunts)
            return False  # Stop the current timer (we scheduled a new one)
        
        elif self._searchTrials < self.config['SEARCH_TRIALS']:
            self._searchTrials += 1
            return True  # Continue searching at initial rate
        
        else:
            logging.error(f"No SmartShunts found after {self.config['SEARCH_TRIALS']} trials")
            logging.error(f"Check that SmartShunts are connected and not all excluded")
            tt.sleep(self.config['TIME_BEFORE_RESTART'])
            sys.exit(1)
    
    def _update_device_paths(self, shunts):
        """
        Update /Devices/{instance}/* paths to show info about aggregated SmartShunts.
        Uses device instance as the key so paths are stable (e.g., /Devices/278/*, /Devices/277/*).
        """
        # Get current device instances
        current_instances = set(shunt['instance'] for shunt in shunts)
        existing_instances = set(self._device_paths.keys())
        
        # Remove paths for devices that are no longer present
        for instance in existing_instances - current_instances:
            logging.info(f"Removing /Devices/{instance}/* paths (device no longer present)")
            for path in self._device_paths[instance]:
                try:
                    # Note: VeDbusService doesn't have a remove_path method, 
                    # so we'll just set them to empty/None
                    self._dbusservice[path] = [] if "VregLink" in path else None
                except:
                    pass
            del self._device_paths[instance]
        
        # Add or update paths for current devices
        for shunt in shunts:
            instance = shunt['instance']
            service = shunt['service']
            
            # Create paths for this device if not already present
            if instance not in self._device_paths:
                logging.info(f"Creating /Devices/{instance}/* paths for {shunt['name']}")
                
                base_path = f"/Devices/{instance}"
                paths = [
                    f"{base_path}/CustomName",
                    f"{base_path}/DeviceInstance",
                    f"{base_path}/FirmwareVersion",
                    f"{base_path}/ProductId",
                    f"{base_path}/ProductName",
                    f"{base_path}/ServiceName",
                    f"{base_path}/VregLink",
                ]
                
                # Add paths to service with appropriate text formatting
                self._dbusservice.add_path(f"{base_path}/CustomName", None)
                self._dbusservice.add_path(f"{base_path}/DeviceInstance", None)
                # Firmware version: format integer as v{major}.{minor} (hex)
                self._dbusservice.add_path(f"{base_path}/FirmwareVersion", None,
                    gettextcallback=lambda a, x: f"v{(x >> 8) & 0xFF}.{x & 0xFF:x}" if x and isinstance(x, int) else "")
                # Product ID: format as hex
                self._dbusservice.add_path(f"{base_path}/ProductId", None,
                    gettextcallback=lambda a, x: f"0x{x:X}" if x and isinstance(x, int) else "")
                self._dbusservice.add_path(f"{base_path}/ProductName", None)
                self._dbusservice.add_path(f"{base_path}/ServiceName", None)
                self._dbusservice.add_path(f"{base_path}/VregLink", [],
                    gettextcallback=lambda a, x: "")
                
                self._device_paths[instance] = paths
            
            # Update values from physical shunt
            base_path = f"/Devices/{instance}"
            try:
                fw_version = self._dbusmon.get_value(service, "/FirmwareVersion")
                product_id = self._dbusmon.get_value(service, "/ProductId")
                
                self._dbusservice[f"{base_path}/CustomName"] = self._dbusmon.get_value(service, "/CustomName")
                self._dbusservice[f"{base_path}/DeviceInstance"] = self._dbusmon.get_value(service, "/DeviceInstance")
                self._dbusservice[f"{base_path}/FirmwareVersion"] = fw_version
                self._dbusservice[f"{base_path}/ProductId"] = product_id
                self._dbusservice[f"{base_path}/ProductName"] = self._dbusmon.get_value(service, "/ProductName")
                self._dbusservice[f"{base_path}/ServiceName"] = service
                self._dbusservice[f"{base_path}/VregLink"] = []  # Not applicable for aggregate
                
                # Debug: log if we got empty values
                if not fw_version:
                    logging.debug(f"FirmwareVersion for {instance} is empty: {fw_version}")
                if not product_id:
                    logging.debug(f"ProductId for {instance} is empty: {product_id}")
            except Exception as e:
                logging.warning(f"Error updating /Devices/{instance} paths: {e}")
    
    def _update(self):
        """Main update function - aggregate SmartShunt data and update D-Bus"""
        
        # Prevent recursive updates
        if self._updating:
            return False
        
        self._updating = True
        
        # Aggregate values
        total_voltage = 0
        voltage_readings = []  # Collect all voltages for smart algorithm
        total_current = 0
        total_power = 0
        total_temperature = 0
        temperature_readings = []  # Collect all temps for smart algorithm
        total_consumed_ah = 0
        soc_readings = []
        time_to_go_readings = []
        
        # Alarm lists (use MAX - if any shunt alarms, we alarm)
        alarm_general_list = []
        alarm_low_voltage_list = []
        alarm_high_voltage_list = []
        alarm_low_soc_list = []
        alarm_high_temp_list = []
        alarm_low_temp_list = []
        
        # History data lists (aggregate from all shunts)
        history_charge_cycles = []
        history_total_ah_drawn = []
        history_min_voltage = []
        history_max_voltage = []
        history_time_since_full = []
        history_auto_syncs = []
        history_lv_alarms = []
        history_hv_alarms = []
        history_last_discharge = []
        history_avg_discharge = []
        history_charged_energy = []
        history_discharged_energy = []
        history_full_discharges = []
        history_deepest_discharge = []
        history_min_starter_voltage = []
        history_max_starter_voltage = []
        
        temp_count = 0
        
        try:
            for shunt in self._shunts:
                service = shunt['service']
                
                # Skip if this shunt's switch is disabled
                if service in self.shunt_switches:
                    if not self.shunt_switches[service].get('enabled', True):
                        logging.debug(f"Skipping disabled shunt: {service}")
                        continue
                
                # Read values
                voltage = self._dbusmon.get_value(service, "/Dc/0/Voltage")
                current = self._dbusmon.get_value(service, "/Dc/0/Current")
                power = self._dbusmon.get_value(service, "/Dc/0/Power")
                soc = self._dbusmon.get_value(service, "/Soc")
                consumed_ah = self._dbusmon.get_value(service, "/ConsumedAmphours")
                temp = self._dbusmon.get_value(service, "/Dc/0/Temperature")
                ttg = self._dbusmon.get_value(service, "/TimeToGo")
                
                # Aggregate
                if voltage is not None:
                    voltage_readings.append(voltage)
                    total_voltage += voltage
                if current is not None:
                    total_current += current
                if power is not None:
                    total_power += power
                if soc is not None:
                    soc_readings.append(soc)
                if consumed_ah is not None:
                    total_consumed_ah += consumed_ah
                if temp is not None:
                    temperature_readings.append(temp)
                    total_temperature += temp
                    temp_count += 1
                if ttg is not None and ttg > 0:
                    time_to_go_readings.append(ttg)
                
                # Read alarms from physical shunts
                alarm_gen = self._dbusmon.get_value(service, "/Alarms/Alarm")
                alarm_lv = self._dbusmon.get_value(service, "/Alarms/LowVoltage")
                alarm_hv = self._dbusmon.get_value(service, "/Alarms/HighVoltage")
                alarm_ls = self._dbusmon.get_value(service, "/Alarms/LowSoc")
                alarm_ht = self._dbusmon.get_value(service, "/Alarms/HighTemperature")
                alarm_lt = self._dbusmon.get_value(service, "/Alarms/LowTemperature")
                
                if alarm_gen is not None:
                    alarm_general_list.append(alarm_gen)
                if alarm_lv is not None:
                    alarm_low_voltage_list.append(alarm_lv)
                if alarm_hv is not None:
                    alarm_high_voltage_list.append(alarm_hv)
                if alarm_ls is not None:
                    alarm_low_soc_list.append(alarm_ls)
                if alarm_ht is not None:
                    alarm_high_temp_list.append(alarm_ht)
                if alarm_lt is not None:
                    alarm_low_temp_list.append(alarm_lt)
                
                # Read history data
                hist_charge_cycles = self._dbusmon.get_value(service, "/History/ChargeCycles")
                if hist_charge_cycles is not None:
                    history_charge_cycles.append(hist_charge_cycles)
                
                hist_total_ah = self._dbusmon.get_value(service, "/History/TotalAhDrawn")
                if hist_total_ah is not None:
                    history_total_ah_drawn.append(hist_total_ah)
                
                hist_min_v = self._dbusmon.get_value(service, "/History/MinimumVoltage")
                if hist_min_v is not None:
                    history_min_voltage.append(hist_min_v)
                
                hist_max_v = self._dbusmon.get_value(service, "/History/MaximumVoltage")
                if hist_max_v is not None:
                    history_max_voltage.append(hist_max_v)
                
                hist_time = self._dbusmon.get_value(service, "/History/TimeSinceLastFullCharge")
                if hist_time is not None:
                    history_time_since_full.append(hist_time)
                
                hist_syncs = self._dbusmon.get_value(service, "/History/AutomaticSyncs")
                if hist_syncs is not None:
                    history_auto_syncs.append(hist_syncs)
                
                hist_lv_alarms = self._dbusmon.get_value(service, "/History/LowVoltageAlarms")
                if hist_lv_alarms is not None:
                    history_lv_alarms.append(hist_lv_alarms)
                
                hist_hv_alarms = self._dbusmon.get_value(service, "/History/HighVoltageAlarms")
                if hist_hv_alarms is not None:
                    history_hv_alarms.append(hist_hv_alarms)
                
                hist_last_discharge = self._dbusmon.get_value(service, "/History/LastDischarge")
                if hist_last_discharge is not None:
                    history_last_discharge.append(hist_last_discharge)
                
                hist_avg_discharge = self._dbusmon.get_value(service, "/History/AverageDischarge")
                if hist_avg_discharge is not None:
                    history_avg_discharge.append(hist_avg_discharge)
                
                hist_charged_energy = self._dbusmon.get_value(service, "/History/ChargedEnergy")
                if hist_charged_energy is not None:
                    history_charged_energy.append(hist_charged_energy)
                
                hist_discharged_energy = self._dbusmon.get_value(service, "/History/DischargedEnergy")
                if hist_discharged_energy is not None:
                    history_discharged_energy.append(hist_discharged_energy)
                
                hist_full_discharges = self._dbusmon.get_value(service, "/History/FullDischarges")
                if hist_full_discharges is not None:
                    history_full_discharges.append(hist_full_discharges)
                
                hist_deepest = self._dbusmon.get_value(service, "/History/DeepestDischarge")
                if hist_deepest is not None:
                    history_deepest_discharge.append(hist_deepest)
                
                # Starter voltage (auxiliary monitoring - may not be present on all shunts)
                hist_min_starter = self._dbusmon.get_value(service, "/History/MinimumStarterVoltage")
                if hist_min_starter is not None and hist_min_starter > 0:  # Only include if actually has value
                    history_min_starter_voltage.append(hist_min_starter)
                    logging.debug(f"{service}: MinimumStarterVoltage = {hist_min_starter}V")
                
                hist_max_starter = self._dbusmon.get_value(service, "/History/MaximumStarterVoltage")
                if hist_max_starter is not None and hist_max_starter > 0:  # Only include if actually has value
                    history_max_starter_voltage.append(hist_max_starter)
                    logging.debug(f"{service}: MaximumStarterVoltage = {hist_max_starter}V")
        
        except Exception as e:
            logging.error(f"Error reading SmartShunt data: {e}")
            self._readTrials += 1
            if self._readTrials > self.config['READ_TRIALS']:
                logging.error("Too many read errors. Restarting...")
                self._updating = False
                tt.sleep(self.config['TIME_BEFORE_RESTART'])
                sys.exit(1)
            self._updating = False
            return False
        
        # Reset read trial counter on success
        self._readTrials = 1
        
        # Calculate averages and combined values
        num_shunts = len(self._shunts)
        avg_soc = sum(soc_readings) / len(soc_readings) if soc_readings else 50.0
        
        # Smart voltage selection - prioritize dangerous voltages for battery protection
        # If any SmartShunt is alarming on voltage, report the worst-case voltage
        if voltage_readings:
            min_voltage = min(voltage_readings)
            max_voltage = max(voltage_readings)
            avg_voltage = sum(voltage_readings) / len(voltage_readings)
            
            # Check if any SmartShunt is triggering voltage alarms
            low_voltage_alarm_active = any(alarm_low_voltage_list)
            high_voltage_alarm_active = any(alarm_high_voltage_list)
            
            if low_voltage_alarm_active and high_voltage_alarm_active:
                # Both voltage alarms present - report the most severe
                # Use the voltage that's furthest from nominal (13V for 12V system)
                nominal_voltage = 13.0
                low_deviation = abs(min_voltage - nominal_voltage)
                high_deviation = abs(max_voltage - nominal_voltage)
                reported_voltage = min_voltage if low_deviation > high_deviation else max_voltage
                logging.warning(f"Both voltage alarms active - Low: {min_voltage:.2f}V, High: {max_voltage:.2f}V, Reporting: {reported_voltage:.2f}V")
            elif low_voltage_alarm_active:
                # Low voltage alarm - report minimum (weakest battery)
                reported_voltage = min_voltage
                logging.warning(f"Low voltage alarm active - Reporting minimum: {min_voltage:.2f}V (avg: {avg_voltage:.2f}V)")
            elif high_voltage_alarm_active:
                # High voltage alarm - report maximum (strongest battery)
                reported_voltage = max_voltage
                logging.warning(f"High voltage alarm active - Reporting maximum: {max_voltage:.2f}V (avg: {avg_voltage:.2f}V)")
            else:
                # No alarms - all voltages in safe range, use average
                reported_voltage = avg_voltage
        else:
            reported_voltage = 0
        
        # Smart temperature selection - prioritize dangerous temperatures for cell protection
        # LiFePO4 safe charging range: 0°C to 45°C (32°F to 113°F)
        # LiFePO4 safe discharging range: -20°C to 60°C (-4°F to 140°F)
        # Use charging limits as they're more restrictive
        if temperature_readings:
            min_temp = min(temperature_readings)
            max_temp = max(temperature_readings)
            avg_temperature = sum(temperature_readings) / len(temperature_readings)
            
            # Get thresholds from D-Bus switches (with fallback to stored defaults)
            # Note: We read from Measurement, not Dimming, because Measurement contains the actual
            # temperature value after conversion from the slider position
            try:
                LOW_TEMP = self._dbusservice['/SwitchableOutput/relay_temp_low/Measurement']
                if LOW_TEMP is None:
                    LOW_TEMP = self._default_temp_low
            except:
                LOW_TEMP = self._default_temp_low
            
            try:
                HIGH_TEMP = self._dbusservice['/SwitchableOutput/relay_temp_high/Measurement']
                if HIGH_TEMP is None:
                    HIGH_TEMP = self._default_temp_high
            except:
                HIGH_TEMP = self._default_temp_high
            
            # Check if any temp is outside thresholds
            low_temp_alert = min_temp < LOW_TEMP
            high_temp_alert = max_temp > HIGH_TEMP
            
            if low_temp_alert and high_temp_alert:
                # Both alerts present - which is closer to critical?
                # Critical points: 0°C for low, 45°C for high
                low_severity = abs(min_temp - 0)
                high_severity = abs(max_temp - 45)
                # Use whichever is closer to critical
                reported_temperature = min_temp if low_severity < high_severity else max_temp
                logging.info(f"Temperature alert detected - Low: {min_temp:.1f}°C, High: {max_temp:.1f}°C, Reporting: {reported_temperature:.1f}°C")
            elif low_temp_alert:
                # Low temp - report lowest temp
                reported_temperature = min_temp
                logging.info(f"Low temperature alert - Reporting lowest: {min_temp:.1f}°C (avg: {avg_temperature:.1f}°C)")
            elif high_temp_alert:
                # High temp - report highest temp
                reported_temperature = max_temp
                logging.info(f"High temperature alert - Reporting highest: {max_temp:.1f}°C (avg: {avg_temperature:.1f}°C)")
            else:
                # All temps in normal range - use average
                reported_temperature = avg_temperature
        else:
            reported_temperature = None
        
        # Use capacity-weighted average SoC from SmartShunts
        # SmartShunts have dedicated hardware and individual calibration - always more accurate
        soc = avg_soc
        consumed_ah = total_consumed_ah
        capacity = self.config['TOTAL_CAPACITY'] * soc / 100
        
        # Calculate TimeToGo
        # We use our own calculation based on combined current and remaining capacity
        # This is more accurate than averaging individual SmartShunt TTGs because:
        # - Batteries in parallel discharge at different rates (due to impedance differences)
        # - Individual TTGs will diverge as one battery depletes faster
        # - Combined system TTG should be based on total remaining capacity and total current draw
        if total_current < 0:  # Discharging
            # TTG in seconds = (Remaining Capacity in Ah) / (Discharge Current in A) * 3600
            time_to_go = -3600 * capacity / total_current if total_current != 0 else None
            
            # Optional: Compare with SmartShunt TTG estimates for logging
            if time_to_go and time_to_go_readings and not self._ttg_divergence_logged:
                min_shunt_ttg = min(time_to_go_readings)
                max_shunt_ttg = max(time_to_go_readings)
                
                # Log once if there's significant divergence (indicates unbalanced discharge)
                if max_shunt_ttg > 0 and (max_shunt_ttg - min_shunt_ttg) / max_shunt_ttg > 0.2:  # >20% difference
                    logging.warning(f"SmartShunt TTG divergence detected: min={min_shunt_ttg/3600:.1f}h, max={max_shunt_ttg/3600:.1f}h, aggregate={time_to_go/3600:.1f}h")
                    logging.warning("This indicates batteries are discharging at different rates - may indicate impedance mismatch or different battery health")
                    self._ttg_divergence_logged = True
        else:
            time_to_go = None
        
        # Aggregate alarms - use MAX (if ANY shunt alarms, we alarm)
        alarm_general = max(alarm_general_list) if alarm_general_list else 0
        alarm_low_voltage = max(alarm_low_voltage_list) if alarm_low_voltage_list else 0
        alarm_high_voltage = max(alarm_high_voltage_list) if alarm_high_voltage_list else 0
        alarm_low_soc = max(alarm_low_soc_list) if alarm_low_soc_list else 0
        alarm_high_temp = max(alarm_high_temp_list) if alarm_high_temp_list else 0
        alarm_low_temp = max(alarm_low_temp_list) if alarm_low_temp_list else 0
        
        # Aggregate history data
        # Cycles: batteries in parallel cycle together, use MAX
        agg_charge_cycles = max(history_charge_cycles) if history_charge_cycles else 0
        agg_full_discharges = max(history_full_discharges) if history_full_discharges else 0
        
        # Total Ah/Energy: SUM (combined from both batteries)
        agg_total_ah_drawn = sum(history_total_ah_drawn) if history_total_ah_drawn else 0
        agg_charged_energy = sum(history_charged_energy) if history_charged_energy else 0
        agg_discharged_energy = sum(history_discharged_energy) if history_discharged_energy else 0
        agg_last_discharge = sum(history_last_discharge) if history_last_discharge else 0
        
        # Voltages: MIN for minimum, MAX for maximum
        agg_min_voltage = min(history_min_voltage) if history_min_voltage else None
        agg_max_voltage = max(history_max_voltage) if history_max_voltage else None
        
        # Time: MAX (most conservative - longest time since full charge)
        agg_time_since_full = max(history_time_since_full) if history_time_since_full else None
        
        # Syncs: MAX (most syncs from either battery)
        agg_auto_syncs = max(history_auto_syncs) if history_auto_syncs else 0
        
        # Alarms: SUM (total alarm events across both batteries)
        agg_lv_alarms = sum(history_lv_alarms) if history_lv_alarms else 0
        agg_hv_alarms = sum(history_hv_alarms) if history_hv_alarms else 0
        
        # Discharge: MAX for deepest (worst case), AVG for average
        agg_deepest_discharge = min(history_deepest_discharge) if history_deepest_discharge else 0  # Most negative = deepest
        agg_avg_discharge = sum(history_avg_discharge) / len(history_avg_discharge) if history_avg_discharge else 0
        
        # Starter voltages: MIN for minimum, MAX for maximum (auxiliary battery monitoring)
        # These may be empty if no SmartShunts have starter voltage input connected
        agg_min_starter_voltage = min(history_min_starter_voltage) if history_min_starter_voltage else []
        agg_max_starter_voltage = max(history_max_starter_voltage) if history_max_starter_voltage else []
        
        # Aggregate VE.Direct communication errors (SUM across all shunts)
        vedirect_hex_checksum = []
        vedirect_hex_invalid_char = []
        vedirect_hex_unfinished = []
        vedirect_text_checksum = []
        vedirect_text_parse = []
        vedirect_text_unfinished = []
        
        for shunt in self._shunts:
            service = shunt['service']
            
            # Skip if this shunt's switch is disabled
            if service in self.shunt_switches:
                if not self.shunt_switches[service].get('enabled', True):
                    continue
            
            vedirect_hex_checksum.append(self._dbusmon.get_value(service, "/VEDirect/HexChecksumErrors") or 0)
            vedirect_hex_invalid_char.append(self._dbusmon.get_value(service, "/VEDirect/HexInvalidCharacterErrors") or 0)
            vedirect_hex_unfinished.append(self._dbusmon.get_value(service, "/VEDirect/HexUnfinishedErrors") or 0)
            vedirect_text_checksum.append(self._dbusmon.get_value(service, "/VEDirect/TextChecksumErrors") or 0)
            vedirect_text_parse.append(self._dbusmon.get_value(service, "/VEDirect/TextParseError") or 0)
            vedirect_text_unfinished.append(self._dbusmon.get_value(service, "/VEDirect/TextUnfinishedErrors") or 0)
        
        agg_vedirect_hex_checksum = sum(vedirect_hex_checksum)
        agg_vedirect_hex_invalid_char = sum(vedirect_hex_invalid_char)
        agg_vedirect_hex_unfinished = sum(vedirect_hex_unfinished)
        agg_vedirect_text_checksum = sum(vedirect_text_checksum)
        agg_vedirect_text_parse = sum(vedirect_text_parse)
        agg_vedirect_text_unfinished = sum(vedirect_text_unfinished)
        
        # Update D-Bus
        with self._dbusservice as bus:
            bus["/Dc/0/Voltage"] = reported_voltage
            bus["/Dc/0/Current"] = total_current
            bus["/Dc/0/Power"] = total_power
            bus["/Dc/0/Temperature"] = reported_temperature
            
            bus["/Soc"] = soc
            # Note: /Capacity and /InstalledCapacity are NOT published - these are BMS-specific paths
            # Physical SmartShunts don't have these paths. For BMS functionality, use dbus-smartshunt-to-bms project.
            bus["/ConsumedAmphours"] = consumed_ah
            # TimeToGo: use [] (empty array) when None to match physical SmartShunt behavior
            bus["/TimeToGo"] = time_to_go if time_to_go is not None else []
            
            bus["/Alarms/Alarm"] = alarm_general
            bus["/Alarms/LowVoltage"] = alarm_low_voltage
            bus["/Alarms/HighVoltage"] = alarm_high_voltage
            bus["/Alarms/LowSoc"] = alarm_low_soc
            bus["/Alarms/HighTemperature"] = alarm_high_temp
            bus["/Alarms/LowTemperature"] = alarm_low_temp
            
            # Update history data
            bus["/History/ChargeCycles"] = agg_charge_cycles
            bus["/History/TotalAhDrawn"] = agg_total_ah_drawn
            bus["/History/MinimumVoltage"] = agg_min_voltage
            bus["/History/MaximumVoltage"] = agg_max_voltage
            bus["/History/TimeSinceLastFullCharge"] = agg_time_since_full
            bus["/History/AutomaticSyncs"] = agg_auto_syncs
            bus["/History/LowVoltageAlarms"] = agg_lv_alarms
            bus["/History/HighVoltageAlarms"] = agg_hv_alarms
            bus["/History/LastDischarge"] = agg_last_discharge
            bus["/History/AverageDischarge"] = agg_avg_discharge
            bus["/History/ChargedEnergy"] = agg_charged_energy
            bus["/History/DischargedEnergy"] = agg_discharged_energy
            bus["/History/FullDischarges"] = agg_full_discharges
            bus["/History/DeepestDischarge"] = agg_deepest_discharge
            bus["/History/MinimumStarterVoltage"] = agg_min_starter_voltage
            bus["/History/MaximumStarterVoltage"] = agg_max_starter_voltage
            
            # Update VE.Direct communication error counters (aggregated from all shunts)
            bus["/VEDirect/HexChecksumErrors"] = agg_vedirect_hex_checksum
            bus["/VEDirect/HexInvalidCharacterErrors"] = agg_vedirect_hex_invalid_char
            bus["/VEDirect/HexUnfinishedErrors"] = agg_vedirect_hex_unfinished
            bus["/VEDirect/TextChecksumErrors"] = agg_vedirect_text_checksum
            bus["/VEDirect/TextParseError"] = agg_vedirect_text_parse
            bus["/VEDirect/TextUnfinishedErrors"] = agg_vedirect_text_unfinished
            
            # No charge control - this is pure monitoring (SmartShunt behavior)
            # For BMS functionality (CVL/CCL/DCL, AllowToCharge/Discharge), use dbus-smartshunt-to-bms project
        
        # Reset updating flag
        self._updating = False
        
        return False  # Don't repeat (we're now reactive, not polling)
    
    def _periodic_log(self):
        """Periodic logging of status - called every LOG_PERIOD seconds"""
        try:
            # Read current values
            voltage = self._dbusservice["/Dc/0/Voltage"]
            current = self._dbusservice["/Dc/0/Current"]
            soc = self._dbusservice["/Soc"]
            
            logging.info(f"Status: {voltage:.2f}V, {current:.1f}A, {soc:.1f}% SoC")
            
            # Log individual shunt values (only enabled ones)
            for shunt in self._shunts:
                name = shunt['name']
                service = shunt['service']
                
                # Skip if this shunt's switch is disabled
                if service in self.shunt_switches:
                    if not self.shunt_switches[service].get('enabled', True):
                        continue
                
                v = self._dbusmon.get_value(service, "/Dc/0/Voltage")
                i = self._dbusmon.get_value(service, "/Dc/0/Current")
                s = self._dbusmon.get_value(service, "/Soc")
                if v is not None and i is not None and s is not None:
                    logging.info(f"  |- {name}: {v:.2f}V, {i:.1f}A, {s:.1f}%")
        
        except Exception as e:
            logging.error(f"Error in periodic logging: {e}")
        
        return True  # Keep repeating


def main():
    # Import config
    import settings
    
    # Set up logging
    logging.basicConfig(level=settings.LOGGING_LEVEL)
    
    logging.info("")
    logging.info("*** Starting dbus-aggregate-smartshunts ***")
    logging.info(f"Version: {VERSION}")
    
    # Initialize D-Bus main loop
    from dbus.mainloop.glib import DBusGMainLoop
    DBusGMainLoop(set_as_default=True)
    
    # Auto-detect total capacity from SmartShunt configuration registers
    logging.info("Reading total capacity from SmartShunt configuration...")
    
    min_charged_voltage = None  # Will be set when reading config
    
    try:
        # Read from SmartShunt configuration registers (0x1000)
        from smartshunt_config import SmartShuntConfig
        import dbus
        
        bus = dbus.SystemBus()
        # Get list of SmartShunt services
        shunt_services = []
        for service_name in bus.list_names():
            if service_name.startswith('com.victronenergy.battery.'):
                # Check if it's a SmartShunt (ProductId 0xA389)
                try:
                    obj = bus.get_object(service_name, '/ProductId')
                    iface = dbus.Interface(obj, 'com.victronenergy.BusItem')
                    product_id = iface.GetValue()
                    if product_id == 0xA389:  # SmartShunt
                        # Get product name for logging
                        try:
                            obj = bus.get_object(service_name, '/ProductName')
                            product_name = str(obj.Get('com.victronenergy.BusItem', 'Value', dbus_interface='org.freedesktop.DBus.Properties'))
                            logging.info(f"|- Found: {product_name}")
                        except:
                            logging.info(f"|- Found: {service_name}")
                        shunt_services.append(service_name)
                except:
                    pass
        
        if not shunt_services:
            logging.error("No SmartShunts found!")
            raise ValueError("No SmartShunts available to aggregate")
        
        # Read firmware/hardware/product info from first shunt to mirror it
        first_shunt_firmware = None
        first_shunt_firmware_int = None
        first_shunt_hardware = None
        first_shunt_product_name = None
        first_shunt_product_id = None
        
        try:
            obj = bus.get_object(shunt_services[0], '/FirmwareVersion')
            iface = dbus.Interface(obj, 'com.victronenergy.BusItem')
            fw_value = iface.GetValue()
            # Store the raw integer value for /Devices/0/FirmwareVersion
            first_shunt_firmware_int = fw_value
            # Convert to text format if it's an integer (e.g., 1049 decimal = 0x419 hex = v4.19)
            if isinstance(fw_value, (int, dbus.Int32)):
                # Victron format: 0xMMNN where MM is major (hex), NN is minor (hex)
                major = (fw_value >> 8) & 0xFF  # High byte
                minor = fw_value & 0xFF  # Low byte
                first_shunt_firmware = f"v{major}.{minor:x}"  # Format minor as hex without 0x prefix
            else:
                first_shunt_firmware = str(fw_value)
            logging.info(f"|- First shunt firmware: {first_shunt_firmware} (from value: {fw_value})")
        except Exception as e:
            logging.warning(f"|- Could not read firmware version: {e}")
        
        try:
            obj = bus.get_object(shunt_services[0], '/HardwareVersion')
            iface = dbus.Interface(obj, 'com.victronenergy.BusItem')
            hw_value = iface.GetValue()
            if hw_value and hw_value != []:
                first_shunt_hardware = str(hw_value)
                logging.info(f"|- First shunt hardware: {first_shunt_hardware}")
        except Exception as e:
            logging.debug(f"|- Hardware version not available: {e}")
        
        try:
            obj = bus.get_object(shunt_services[0], '/ProductName')
            iface = dbus.Interface(obj, 'com.victronenergy.BusItem')
            first_shunt_product_name = iface.GetValue()
            logging.info(f"|- First shunt product name: {first_shunt_product_name}")
        except Exception as e:
            logging.warning(f"|- Could not read product name: {e}")
        
        try:
            obj = bus.get_object(shunt_services[0], '/ProductId')
            iface = dbus.Interface(obj, 'com.victronenergy.BusItem')
            first_shunt_product_id = iface.GetValue()
            logging.info(f"|- First shunt product ID: 0x{first_shunt_product_id:X}")
        except Exception as e:
            logging.warning(f"|- Could not read product ID: {e}")
        
        # Read configuration from all shunts
        logging.info(f"Reading configuration from {len(shunt_services)} SmartShunt(s)...")
        
        configs = []
        
        # Read config from all shunts
        for i, service in enumerate(shunt_services):
            logging.info(f"  Reading shunt {i+1}/{len(shunt_services)}: {service}")
            config_reader = SmartShuntConfig(service)
            if config_reader.read_all(bus):
                # Log all settings for this shunt
                config_reader.log_all_settings()
                
                # Note: Monitor mode (Battery Monitor vs DC Energy Meter) is NOT readable via VE.Direct
                # We assume all SmartShunts on this system are in Battery Monitor mode
                # If you have DC Energy Meters, disable them via the UI switches after discovery
                
                configs.append(config_reader)
                logging.info(f"    ✓ Added to aggregate")
            else:
                logging.error(f"    ✗ Failed to read configuration from {service}")
        
        if not configs:
            logging.error("\nNo SmartShunts available to aggregate!")
            logging.error("Please check that SmartShunts are connected and not excluded in config")
            raise ValueError("No valid SmartShunts to aggregate")
        
        # Calculate total capacity from SmartShunt configuration registers
        capacities = []
        for config_reader in configs:
            if config_reader.capacity is not None:
                capacities.append(config_reader.capacity)
                logging.info(f"{config_reader.service_name}: {config_reader.capacity}Ah (from config register 0x1000)")
            else:
                logging.error(f"{config_reader.service_name}: Could not read capacity from config register!")
                raise ValueError("Failed to read capacity from SmartShunt")
        
        total_capacity = sum(capacities)
        if total_capacity > 0:
            logging.info(f"✓ Total capacity: {total_capacity}Ah from {len(capacities)} SmartShunt(s)")
            logging.info(f"  (read from SmartShunt configuration registers)")
        else:
            logging.error("Failed to read capacity from any SmartShunt!")
            raise ValueError("No capacity information available")
        
        # Validate SmartShunt configuration
        logging.info(f"SmartShunt configuration (validating {len(configs)} shunt(s)):")
        
        # Check consistency across all shunts
        # CRITICAL: These settings MUST match for accurate aggregation
        critical_settings = [
            ('charged_voltage', 'Charged voltage', 'V', 2),
        ]
        
        # RECOMMENDED: These should match for best accuracy, but not critical
        recommended_settings = [
            ('tail_current', 'Tail current', '%', 1),
            ('charge_efficiency', 'Charge efficiency', '%', 0),
            ('peukert_exponent', 'Peukert exponent', '', 2),
            ('current_threshold', 'Current threshold', 'A', 2),
            ('discharge_floor', 'Discharge floor', '%', 0),
        ]
        
        # Check CRITICAL settings first
        logging.info("\n  === CRITICAL Settings (must match) ===")
        
        for attr, name, unit, decimals in critical_settings:
            values = [getattr(c, attr) for c in configs if getattr(c, attr) is not None]
            
            if values:
                min_val = min(values)
                max_val = max(values)
                avg_val = sum(values) / len(values)
                
                # Store minimum charged voltage for charge control
                if attr == 'charged_voltage' and min_val is not None:
                    min_charged_voltage = min_val
                
                # Check if all values are the same (within tolerance for floats)
                tolerance = 0.01 if decimals > 0 else 0.5
                all_same = (max_val - min_val) <= tolerance
                
                if all_same:
                    if decimals > 0:
                        logging.info(f"  ✓ {name}: {avg_val:.{decimals}f}{unit} (consistent)")
                    else:
                        logging.info(f"  ✓ {name}: {int(avg_val)}{unit} (consistent)")
                else:
                    # CRITICAL MISMATCH - use minimum for safety
                    if decimals > 0:
                        logging.warning(f"  ⚠️  {name}: MISMATCH! Range: {min_val:.{decimals}f}{unit} to {max_val:.{decimals}f}{unit}")
                    else:
                        logging.warning(f"  ⚠️  {name}: MISMATCH! Range: {int(min_val)}{unit} to {int(max_val)}{unit}")
                    logging.warning(f"     All SmartShunts should have the same {name.lower()}!")
                    for i, c in enumerate(configs):
                        val = getattr(c, attr)
                        if val is not None:
                            if decimals > 0:
                                logging.warning(f"     Shunt {i+1}: {val:.{decimals}f}{unit}")
                            else:
                                logging.warning(f"     Shunt {i+1}: {int(val)}{unit}")
                    
                    # Use the MINIMUM (most conservative) value for safety
                    if decimals > 0:
                        logging.warning(f"  → Using MINIMUM value: {min_val:.{decimals}f}{unit} (prevents overcharge)")
                    else:
                        logging.warning(f"  → Using MINIMUM value: {int(min_val)}{unit} (prevents overcharge)")
                    logging.warning(f"     SAFETY: This ensures no battery gets overcharged")
        
        # Check RECOMMENDED settings
        logging.info("\n  === Recommended Settings (should match for best accuracy) ===")
        for attr, name, unit, decimals in recommended_settings:
            values = [getattr(c, attr) for c in configs if getattr(c, attr) is not None]
            
            if values:
                min_val = min(values)
                max_val = max(values)
                avg_val = sum(values) / len(values)
                
                # Check if all values are the same (within tolerance for floats)
                tolerance = 0.01 if decimals > 0 else 0.5
                all_same = (max_val - min_val) <= tolerance
                
                if all_same:
                    if decimals > 0:
                        logging.info(f"  ✓ {name}: {avg_val:.{decimals}f}{unit} (consistent)")
                    else:
                        logging.info(f"  ✓ {name}: {int(avg_val)}{unit} (consistent)")
                else:
                    # Recommended mismatch - log as INFO/WARNING but not critical
                    if decimals > 0:
                        logging.info(f"  ℹ {name}: Varies - Range: {min_val:.{decimals}f}{unit} to {max_val:.{decimals}f}{unit}")
                    else:
                        logging.info(f"  ℹ {name}: Varies - Range: {int(min_val)}{unit} to {int(max_val)}{unit}")
                    logging.info(f"     (Not critical, but matching values recommended for best accuracy)")
        
        logging.info("  Note: These are SmartShunt settings (for SoC calculation/sync)")
        logging.info("  Note: Battery protection limits (CVL/CCL/DCL) are separate config settings")
                
    except Exception as e:
        logging.error(f"Error reading from SmartShunt registers: {e}")
        import traceback
        logging.error(traceback.format_exc())
        logging.error("\nFailed to read capacity from SmartShunt configuration!")
        logging.error("Please ensure:")
        logging.error("  1. SmartShunts are connected and powered on")
        logging.error("  2. Capacity is configured in VictronConnect for each SmartShunt")
        sys.exit(1)
    
    # Create config dict
    config = {
        'DEVICE_NAME': settings.DEVICE_NAME,
        'TOTAL_CAPACITY': total_capacity,  # Auto-detected
        'FIRMWARE_VERSION': first_shunt_firmware if 'first_shunt_firmware' in locals() and first_shunt_firmware else VERSION,
        'FIRMWARE_VERSION_INT': first_shunt_firmware_int if 'first_shunt_firmware_int' in locals() and first_shunt_firmware_int else None,
        'HARDWARE_VERSION': first_shunt_hardware if 'first_shunt_hardware' in locals() and first_shunt_hardware else VERSION,
        'PRODUCT_NAME': first_shunt_product_name if 'first_shunt_product_name' in locals() and first_shunt_product_name else None,
        'PRODUCT_ID': first_shunt_product_id if 'first_shunt_product_id' in locals() and first_shunt_product_id else 0xA389,  # Default to SmartShunt
        'MIN_CHARGED_VOLTAGE': min_charged_voltage if 'min_charged_voltage' in locals() else None,
        'UPDATE_INTERVAL_FIND_DEVICES': settings.UPDATE_INTERVAL_FIND_DEVICES,
        'MAX_UPDATE_INTERVAL_FIND_DEVICES': settings.MAX_UPDATE_INTERVAL_FIND_DEVICES,
        'SEARCH_TRIALS': settings.SEARCH_TRIALS,
        'READ_TRIALS': settings.READ_TRIALS,
        'TIME_BEFORE_RESTART': settings.TIME_BEFORE_RESTART,
        'LOG_PERIOD': settings.LOG_PERIOD,
    }
    
    logging.info("========== Settings ==========")
    logging.info(f"|- Mode: Monitor only (pure SmartShunt aggregation)")
    logging.info(f"|- Total Capacity: {config['TOTAL_CAPACITY']}Ah (from SmartShunt configuration)")
    logging.info(f"|- Charge control: DISABLED")
    logging.info(f"|  For BMS functionality (CVL/CCL/DCL), use dbus-smartshunt-to-bms project")
    
    # Create service (but don't register on D-Bus yet)
    service = DbusAggregateSmartShunts(config)
    
    # Wait for smartshunts to appear before registering
    # This prevents "GetItems failed" errors from systemcalc trying to query us
    # before we're ready to respond
    logging.info("Waiting for SmartShunts to appear on D-Bus before registering...")
    
    # Give dbusmonitor a moment to populate its values after the initial scan
    tt.sleep(1)
    
    # Do an initial search synchronously
    service._find_smartshunts()
    
    # If still no shunts found, wait up to 30 seconds
    max_wait = 30  # Maximum 30 seconds
    wait_count = 1  # We already waited 1 second above
    while len(service._shunts) == 0 and wait_count < max_wait:
        tt.sleep(1)
        wait_count += 1
        if wait_count % 5 == 0:
            logging.info(f"Still waiting for SmartShunts... ({wait_count}s)")
            # Try searching again
            service._find_smartshunts()
    
    if len(service._shunts) == 0:
        logging.error("No SmartShunts found after 30 seconds! Service will not register.")
        sys.exit(1)
    
    logging.info(f"Found {len(service._shunts)} SmartShunt(s), proceeding with registration")
    
    # Perform initial aggregation to ensure we have valid data (voltage != None)
    # This is important because dbus-systemcalc-py only includes batteries in
    # /AvailableBatteries if they have a valid voltage. If we register before
    # aggregating, we'll be "invalid" and won't appear in the GUI battery list.
    logging.info("Performing initial aggregation before registering service...")
    service._update()
    
    # Start main loop first
    logging.info("Starting main loop...")
    mainloop = GLib.MainLoop()
    
    # Register service on D-Bus once the main loop is running
    # This ensures the service can respond to D-Bus method calls immediately
    # Schedule registration to happen on the next idle cycle
    def do_registration():
        logging.info("Main loop running, now registering service on D-Bus...")
        service.register()
        logging.info("Service registered and ready")
        return False  # Don't repeat
    
    GLib.idle_add(do_registration)
    
    mainloop.run()


if __name__ == "__main__":
    main()
