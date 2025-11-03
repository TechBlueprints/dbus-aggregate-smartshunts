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
    
    def __init__(self, config, servicename="com.victronenergy.battery.aggregate_shunts"):
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
        
        logging.info("### Initializing VeDbusService")
        self._dbusservice = VeDbusService(servicename, self._dbusConn, register=False)
        
        # Create management objects
        self._dbusservice.add_path("/Mgmt/ProcessName", __file__)
        self._dbusservice.add_path("/Mgmt/ProcessVersion", "Python " + platform.python_version())
        self._dbusservice.add_path("/Mgmt/Connection", "Virtual SmartShunt Aggregator")
        
        # Create mandatory objects
        self._dbusservice.add_path("/DeviceInstance", 100)
        
        # Always use SmartShunt ProductId (monitor mode only - no charge control)
        # For BMS functionality, use dbus-smartshunt-to-bms project instead
        product_id = 0xA389  # SmartShunt
        default_name = "SmartShunt Aggregate"
        
        # Use custom name if provided, otherwise use default
        device_name = config['DEVICE_NAME'] if config['DEVICE_NAME'] else default_name
        
        self._dbusservice.add_path("/ProductId", product_id,
            gettextcallback=lambda a, x: f"0x{x:X}" if x and isinstance(x, int) else "")
        self._dbusservice.add_path("/ProductName", device_name)
        
        # Mirror firmware version from first physical shunt
        self._dbusservice.add_path("/FirmwareVersion", config.get('FIRMWARE_VERSION', VERSION))
        # Hardware version: physical SmartShunts don't have one (empty), so we shouldn't either
        self._dbusservice.add_path("/HardwareVersion", [],
            gettextcallback=lambda a, x: "")
        self._dbusservice.add_path("/Connected", 1)
        self._dbusservice.add_path("/Serial", "AGGREGATE01")
        self._dbusservice.add_path("/CustomName", device_name)
        
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
        self._dbusservice.add_path("/Devices/0/CustomName", device_name)
        self._dbusservice.add_path("/Devices/0/DeviceInstance", 100)  # Our device instance
        # Use firmware version from first detected shunt (integer format)
        self._dbusservice.add_path("/Devices/0/FirmwareVersion", 
            config.get('FIRMWARE_VERSION_INT'),
            gettextcallback=lambda a, x: f"v{(x >> 8) & 0xFF}.{x & 0xFF:x}" if x and isinstance(x, int) else "")
        self._dbusservice.add_path("/Devices/0/ProductId", product_id,
            gettextcallback=lambda a, x: f"0x{x:X}" if x and isinstance(x, int) else "")
        self._dbusservice.add_path("/Devices/0/ProductName", "Virtual SmartShunt Aggregate")
        self._dbusservice.add_path("/Devices/0/ServiceName", "com.victronenergy.battery.aggregate_shunts")
        self._dbusservice.add_path("/Devices/0/VregLink", [],
            gettextcallback=lambda a, x: "")
        
        # Create /VEDirect/* paths to aggregate communication errors from all shunts
        self._dbusservice.add_path("/VEDirect/HexChecksumErrors", None)
        self._dbusservice.add_path("/VEDirect/HexInvalidCharacterErrors", None)
        self._dbusservice.add_path("/VEDirect/HexUnfinishedErrors", None)
        self._dbusservice.add_path("/VEDirect/TextChecksumErrors", None)
        self._dbusservice.add_path("/VEDirect/TextParseError", None)
        self._dbusservice.add_path("/VEDirect/TextUnfinishedErrors", None)
        
        # Register service
        logging.info("### Registering VeDbusService")
        self._dbusservice.register()
        
        # Start searching for SmartShunts
        GLib.timeout_add_seconds(self.config['UPDATE_INTERVAL_FIND_DEVICES'], self._find_smartshunts)
    
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
        self._dbusmon = DbusMonitor(monitorlist, valueChangedCallback=self._on_value_changed,
                                     deviceAddedCallback=None, deviceRemovedCallback=None)
    
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
    
    def _find_smartshunts(self):
        """Search for SmartShunt services on D-Bus"""
        logging.info(f"Searching for SmartShunts: Trial #{self._searchTrials}")
        
        found_shunts = []
        
        try:
            for service in self._dbusConn.list_names():
                if "com.victronenergy.battery" in service:
                    product_name = self._dbusmon.get_value(service, "/ProductName")
                    
                    # Check if this is a SmartShunt
                    if product_name and "SmartShunt" in product_name:
                        device_instance = self._dbusmon.get_value(service, "/DeviceInstance")
                        custom_name = self._dbusmon.get_value(service, "/CustomName")
                        
                        # Check if this SmartShunt should be excluded
                        if not self._should_exclude_shunt(device_instance, custom_name):
                            found_shunts.append({
                                'service': service,
                                'instance': device_instance,
                                'name': custom_name or f"Shunt {device_instance}",
                                'product': product_name
                            })
                            logging.info(f"|- Found: {custom_name} [{device_instance}] - {product_name}")
                        else:
                            logging.info(f"|- Excluded: {custom_name} [{device_instance}] - {product_name}")
        
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
    
    def _should_exclude_shunt(self, device_instance, custom_name):
        """Check if a SmartShunt should be excluded based on config"""
        exclude_list = self.config.get('EXCLUDE_SHUNTS')
        
        if exclude_list is None or exclude_list == []:
            # No exclusions - include all
            return False
        
        # Check if instance ID or name matches exclusion list
        for identifier in exclude_list:
            if isinstance(identifier, int) and identifier == device_instance:
                return True
            if isinstance(identifier, str) and identifier == custom_name:
                return True
        
        return False
    
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
            
            # Get thresholds from config
            COLD_DANGER = self.config['TEMP_COLD_DANGER']
            HOT_DANGER = self.config['TEMP_HOT_DANGER']
            
            # Check if any temp is in danger zone
            cold_danger = min_temp < COLD_DANGER
            hot_danger = max_temp > HOT_DANGER
            
            if cold_danger and hot_danger:
                # Both dangers present - which is closer to critical?
                # Critical points: 0°C for cold, 45°C for hot
                cold_severity = abs(min_temp - 0)
                hot_severity = abs(max_temp - 45)
                # Use whichever is closer to critical
                reported_temperature = min_temp if cold_severity < hot_severity else max_temp
                logging.info(f"Temperature danger detected - Cold: {min_temp:.1f}°C, Hot: {max_temp:.1f}°C, Reporting: {reported_temperature:.1f}°C")
            elif cold_danger:
                # Cold is dangerous - report lowest temp
                reported_temperature = min_temp
                logging.info(f"Cold temperature warning - Reporting lowest: {min_temp:.1f}°C (avg: {avg_temperature:.1f}°C)")
            elif hot_danger:
                # Heat is dangerous - report highest temp
                reported_temperature = max_temp
                logging.info(f"High temperature warning - Reporting highest: {max_temp:.1f}°C (avg: {avg_temperature:.1f}°C)")
            else:
                # All temps in safe range - use average
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
            
            # Log individual shunt values
            for shunt in self._shunts:
                name = shunt['name']
                service = shunt['service']
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
        # Get list of SmartShunt services (excluding any in EXCLUDE_SHUNTS)
        shunt_services = []
        for service_name in bus.list_names():
            if service_name.startswith('com.victronenergy.battery.') and service_name not in settings.EXCLUDE_SHUNTS:
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
        
        # Read firmware/hardware version from first shunt to mirror it
        first_shunt_firmware = None
        first_shunt_firmware_int = None
        first_shunt_hardware = None
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
                # If you have DC Energy Meters, manually exclude them via EXCLUDE_SHUNTS in config
                
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
        logging.error("  3. SmartShunts are not in the EXCLUDE_SHUNTS list")
        sys.exit(1)
    
    # Create config dict
    config = {
        'DEVICE_NAME': settings.DEVICE_NAME,
        'EXCLUDE_SHUNTS': settings.EXCLUDE_SHUNTS,
        'TOTAL_CAPACITY': total_capacity,  # Auto-detected
        'FIRMWARE_VERSION': first_shunt_firmware if 'first_shunt_firmware' in locals() and first_shunt_firmware else VERSION,
        'FIRMWARE_VERSION_INT': first_shunt_firmware_int if 'first_shunt_firmware_int' in locals() and first_shunt_firmware_int else None,
        'HARDWARE_VERSION': first_shunt_hardware if 'first_shunt_hardware' in locals() and first_shunt_hardware else VERSION,
        'MIN_CHARGED_VOLTAGE': min_charged_voltage if 'min_charged_voltage' in locals() else None,
        'TEMP_COLD_DANGER': settings.TEMP_COLD_DANGER,
        'TEMP_HOT_DANGER': settings.TEMP_HOT_DANGER,
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
    
    # Create service
    DbusAggregateSmartShunts(config)
    
    # Run main loop
    logging.info("Connected to D-Bus, starting main loop")
    mainloop = GLib.MainLoop()
    mainloop.run()


if __name__ == "__main__":
    main()

