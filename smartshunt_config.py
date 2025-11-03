"""
SmartShunt Configuration Reader
Reads configuration settings from Victron SmartShunts via VE.Direct VregLink interface
"""

import dbus
import struct
import logging

# VE.Direct register addresses for SmartShunt configuration
# Only the 0x1000 range is readable on SmartShunts (verified on SmartShunt 500A/50mV)
# All other ranges (0xED9x, 0xEDAx) return error 33025 (not supported/not exposed)

VREG_CAPACITY = 0x1000           # Battery capacity in Ah (16-bit LE, direct value)
VREG_CHARGED_VOLTAGE = 0x1001    # Charged voltage in 0.1V (16-bit LE) - when to sync SoC to 100%
VREG_TAIL_CURRENT = 0x1002       # Tail current in 0.1% (16-bit LE) - current threshold for "charged"
VREG_CHARGED_TIME = 0x1003       # Charged detection time in minutes (16-bit LE)
VREG_CHARGE_EFFICIENCY = 0x1004  # Charge efficiency factor in % (16-bit LE) - Peukert
VREG_PEUKERT = 0x1005            # Peukert exponent * 100 (16-bit LE)
VREG_CURRENT_THRESHOLD = 0x1006  # Current threshold in 0.01A (16-bit LE)
VREG_TIME_TO_GO_PERIOD = 0x1007  # Time-to-go averaging period in minutes (16-bit LE)
VREG_DISCHARGE_FLOOR = 0x1008    # Discharge floor / low SoC alarm in % (16-bit LE)

# Note: SmartShunts do NOT expose:
# - Monitor mode settings (Battery Monitor vs DC Energy Meter)
# - Calibration offsets (voltage, current, temperature)
# - Relay settings
# - Midpoint monitoring settings
# These are firmware-internal or not available via VE.Direct

# Note: SmartShunts do NOT store CVL (Charge Voltage Limit) or other BMS protection voltages
# They only store "Charged voltage" (0x1001) which is used for SoC synchronization
# Battery protection limits (14.6V CVL, 10.8V LVD, etc.) must be configured separately

class SmartShuntConfig:
    """Represents configuration settings for a SmartShunt"""
    
    def __init__(self, service_name):
        """
        Initialize SmartShunt configuration reader
        
        Args:
            service_name: D-Bus service name (e.g. 'com.victronenergy.battery.ttyS5')
        """
        self.service_name = service_name
        
        # Only 0x1000 range registers are readable on SmartShunts
        self.capacity = None
        self.charged_voltage = None
        self.tail_current = None
        self.charged_time = None
        self.charge_efficiency = None
        self.peukert_exponent = None
        self.current_threshold = None
        self.time_to_go_period = None
        self.discharge_floor = None
    
    def read_vreg(self, bus, vreg):
        """
        Read a vreg register from the SmartShunt
        
        Args:
            bus: D-Bus system bus
            vreg: Register address (16-bit)
            
        Returns:
            bytes: Raw data or None if error
        """
        try:
            path = "/Devices/0/VregLink"
            vreglink = bus.get_object(self.service_name, path)
            iface = dbus.Interface(vreglink, "com.victronenergy.VregLink")
            error, data = iface.GetVreg(vreg)
            if error == 0:
                return bytes(data)
            else:
                logging.debug(f"Vreg {vreg:04x} returned error {error}")
                return None
        except Exception as e:
            logging.error(f"Error reading vreg {vreg:04x} from {self.service_name}: {e}")
            return None
    
    def read_all(self, bus):
        """
        Read all configuration settings from the SmartShunt
        
        Args:
            bus: D-Bus system bus
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Battery capacity (Ah) - direct value, no scaling
            data = self.read_vreg(bus, VREG_CAPACITY)
            if data and len(data) >= 2:
                self.capacity = struct.unpack('<H', data[:2])[0]  # Direct Ah value
            
            # Charged voltage (V) - 0.1V scaling
            data = self.read_vreg(bus, VREG_CHARGED_VOLTAGE)
            if data and len(data) >= 2:
                self.charged_voltage = struct.unpack('<H', data[:2])[0] / 10.0  # Convert to V
            
            # Tail current (%)
            data = self.read_vreg(bus, VREG_TAIL_CURRENT)
            if data and len(data) >= 2:
                self.tail_current = struct.unpack('<H', data[:2])[0] / 10.0  # Convert to %
            
            # Charged detection time (minutes)
            data = self.read_vreg(bus, VREG_CHARGED_TIME)
            if data and len(data) >= 2:
                self.charged_time = struct.unpack('<H', data[:2])[0]  # minutes
            
            # Charge efficiency factor (%)
            data = self.read_vreg(bus, VREG_CHARGE_EFFICIENCY)
            if data and len(data) >= 2:
                self.charge_efficiency = struct.unpack('<H', data[:2])[0]  # %
            
            # Peukert exponent
            data = self.read_vreg(bus, VREG_PEUKERT)
            if data and len(data) >= 2:
                self.peukert_exponent = struct.unpack('<H', data[:2])[0] / 100.0  # Convert to actual value
            
            # Current threshold (A)
            data = self.read_vreg(bus, VREG_CURRENT_THRESHOLD)
            if data and len(data) >= 2:
                self.current_threshold = struct.unpack('<H', data[:2])[0] / 100.0  # Convert to A
            
            # Time-to-go averaging period (minutes)
            data = self.read_vreg(bus, VREG_TIME_TO_GO_PERIOD)
            if data and len(data) >= 2:
                self.time_to_go_period = struct.unpack('<H', data[:2])[0]  # minutes
            
            # Discharge floor / low SoC alarm (%)
            data = self.read_vreg(bus, VREG_DISCHARGE_FLOOR)
            if data and len(data) >= 2:
                self.discharge_floor = struct.unpack('<H', data[:2])[0]  # %
            
            return True
        
        except Exception as e:
            logging.error(f"Error reading SmartShunt config from {self.service_name}: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return False
    
    def log_all_settings(self):
        """Log all readable configuration settings from SmartShunt"""
        logging.info(f"=== SmartShunt Configuration: {self.service_name} ===")
        
        # All readable settings from 0x1000 range
        if self.charged_voltage is not None:
            logging.info(f"  Charged Voltage: {self.charged_voltage:.2f} V")
        if self.tail_current is not None:
            logging.info(f"  Tail Current: {self.tail_current:.1f}%")
        if self.charged_time is not None:
            logging.info(f"  Charged Detection Time: {self.charged_time} min")
        if self.charge_efficiency is not None:
            logging.info(f"  Charge Efficiency: {self.charge_efficiency}%")
        if self.peukert_exponent is not None:
            logging.info(f"  Peukert Exponent: {self.peukert_exponent:.2f}")
        if self.current_threshold is not None:
            logging.info(f"  Current Threshold: {self.current_threshold:.2f} A")
        if self.time_to_go_period is not None:
            logging.info(f"  Time-to-Go Period: {self.time_to_go_period} min")
        if self.discharge_floor is not None:
            logging.info(f"  Discharge Floor: {self.discharge_floor}%")
        
        logging.info(f"=== End Configuration ===")

    
    def __str__(self):
        """String representation of configuration"""
        lines = [f"SmartShunt Config ({self.service_name}):"]
        if self.capacity is not None:
            lines.append(f"  Capacity: {self.capacity} Ah")
        if self.charged_voltage is not None:
            lines.append(f"  Charged Voltage: {self.charged_voltage:.2f} V")
        if self.tail_current is not None:
            lines.append(f"  Tail Current: {self.tail_current:.1f}%")
        if self.charged_time is not None:
            lines.append(f"  Charged Detection Time: {self.charged_time} min")
        if self.charge_efficiency is not None:
            lines.append(f"  Charge Efficiency: {self.charge_efficiency}%")
        if self.peukert_exponent is not None:
            lines.append(f"  Peukert Exponent: {self.peukert_exponent:.2f}")
        if self.current_threshold is not None:
            lines.append(f"  Current Threshold: {self.current_threshold:.2f} A")
        if self.time_to_go_period is not None:
            lines.append(f"  Time-to-Go Period: {self.time_to_go_period} min")
        if self.discharge_floor is not None:
            lines.append(f"  Discharge Floor: {self.discharge_floor}%")
        return "\n".join(lines)


def get_capacity_from_soc(bus, service):
    """
    Calculate capacity from SoC and ConsumedAmphours (reverse calculation)
    This is more reliable than reading vregs for SmartShunts
    
    Args:
        bus: D-Bus system bus
        service: Service name
        
    Returns:
        float: Capacity in Ah, or None if error
    """
    try:
        # Get SoC
        obj_soc = bus.get_object(service, "/Soc")
        iface_soc = dbus.Interface(obj_soc, "com.victronenergy.BusItem")
        soc = float(iface_soc.GetValue())
        
        # Get ConsumedAmphours
        obj_consumed = bus.get_object(service, "/ConsumedAmphours")
        iface_consumed = dbus.Interface(obj_consumed, "com.victronenergy.BusItem")
        consumed = float(iface_consumed.GetValue())
        
        # Calculate: Capacity = |Consumed| / (1 - SoC/100)
        # Only valid if SoC is between 10% and 90% for accuracy
        if 10 < soc < 90:
            capacity = abs(consumed) / (1.0 - soc/100.0)
            return round(capacity)
        else:
            logging.warning(f"{service}: SoC {soc}% outside reliable range (10-90%) for capacity calculation")
            return None
            
    except Exception as e:
        logging.error(f"Error calculating capacity from {service}: {e}")
        return None


def get_total_capacity(bus, service_list):
    """
    Get total capacity from all SmartShunts using SoC/ConsumedAh calculation
    
    Args:
        bus: D-Bus system bus
        service_list: List of service names
        
    Returns:
        int: Total capacity in Ah, or None if error
    """
    total_capacity = 0
    capacities = []
    
    for service in service_list:
        capacity = get_capacity_from_soc(bus, service)
        if capacity:
            capacities.append(capacity)
            total_capacity += capacity
            logging.info(f"{service}: {capacity} Ah (calculated from SoC)")
        else:
            logging.warning(f"Could not calculate capacity from {service}")
            return None
    
    if len(capacities) >= 2:
        # If we have multiple shunts, they should have similar capacities
        # (assuming same battery type). Log if there's a large discrepancy
        avg_cap = sum(capacities) / len(capacities)
        for i, cap in enumerate(capacities):
            deviation = abs(cap - avg_cap) / avg_cap * 100
            if deviation > 20:
                logging.warning(f"SmartShunt {i} capacity {cap}Ah deviates {deviation:.0f}% from average")
    
    return total_capacity if total_capacity > 0 else None

