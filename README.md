# dbus-aggregate-smartshunts

A Victron Venus OS service that aggregates multiple SmartShunts into a single virtual SmartShunt monitor.

> **Note:** This project is derived from [dbus-aggregate-batteries](https://github.com/Dr-Gigavolt/dbus-aggregate-batteries) by Anton Labanc PhD and adapted for SmartShunt aggregation.

## Purpose

When you have multiple batteries in parallel, each with their own SmartShunt, the Cerbo GX shows them separately. This service combines them into a single virtual SmartShunt, providing unified monitoring of your complete battery bank.

**Key Benefits:**
- 🎯 **Combined monitoring** - See your entire battery bank as one device
- 📊 **Accurate SoC** - Capacity-weighted state of charge across all batteries
- ⚡ **Reactive updates** - Instant response when any SmartShunt reports changes
- 🔧 **Zero configuration** - Auto-detects SmartShunts and calculates capacity
- 🛡️ **Smart protection** - Intelligent voltage and temperature reporting prioritizes battery safety
- 🔍 **Full transparency** - Exposes which physical SmartShunts are being aggregated
- 📊 **Complete history** - Aggregates charge cycles, energy throughput, and all history data

## Features

- ✅ **Auto-discovers all SmartShunts** on the system
- ✅ **Auto-detects total capacity** from SmartShunt configurations
- ✅ **Reactive updates** (no polling delay - updates immediately when any shunt changes)
- ✅ **Combines current** (sum of all shunts)
- ✅ **Smart voltage reporting** (prioritizes safety - reports minimum on low voltage alarm, maximum on high voltage alarm, average otherwise)
- ✅ **Smart temperature reporting** (prioritizes danger - reports coldest when near freezing, hottest when overheating, average otherwise)
- ✅ **Capacity-weighted SoC** calculation
- ✅ **Passes through all alarms** from physical shunts
- ✅ **Aggregates history data** (charge cycles, energy throughput, min/max voltages, etc.)
- ✅ **Time-to-Go calculation** based on total remaining capacity
- ✅ **Starter voltage monitoring** aggregation
- ✅ **VE.Direct error counters** aggregated from all shunts
- ✅ **Completely stateless** - all data derived from physical SmartShunts
- ✅ **Exponential backoff** for device discovery (reduces D-Bus traffic)

## Installation

### Prerequisites

- Victron Cerbo GX or Venus GX running Venus OS
- 2+ SmartShunts connected and visible on D-Bus
- SSH access to your Venus device

### Recommended: One-Line Remote Install

```bash
ssh root@<cerbo-ip> "curl -fsSL https://raw.githubusercontent.com/TechBlueprints/dbus-aggregate-smartshunts/main/install.sh | bash"
```

This will:
- Install `git` if needed
- Clone or update the repository
- Install and start the service
- Survive reboots automatically

### Manual Installation

If you prefer to install manually:

1. **SSH to your Venus device:**
   ```bash
   ssh root@cerbo
   ```

2. **Clone the repository:**
   ```bash
   cd /data/apps
   git clone https://github.com/TechBlueprints/dbus-aggregate-smartshunts.git
   cd dbus-aggregate-smartshunts
   ```

3. **Run the service installer:**
   ```bash
   bash install-service.sh
   ```

That's it! The service will:
- Auto-discover all SmartShunts
- Auto-detect total capacity
- Start aggregating immediately
- Persist across reboots

### Optional Configuration

**No config file is required!** The service runs with sensible defaults:
- Device name: "SmartShunts" (editable in UI)
- Temperature thresholds: Configurable via UI switches (see below)
- SmartShunt selection: Managed via UI switches (see below)

**Advanced users only:** If you need to customize operational settings (logging, polling intervals, error handling), you can create a `config.ini` file:

1. **Create your config:**
   ```bash
   cp config.default.ini config.ini
   nano config.ini
   ```

2. **Optional settings:**
   ```ini
   [DEFAULT]
   
   # Device name (also editable in UI)
   DEVICE_NAME = SmartShunts
   
   # Logging level for troubleshooting
   LOGGING = INFO  # Options: ERROR, WARNING, INFO, DEBUG
   
   # Advanced operational settings (rarely need changing)
   UPDATE_INTERVAL_FIND_DEVICES = 1
   MAX_UPDATE_INTERVAL_FIND_DEVICES = 1800
   LOG_PERIOD = 300
   ```

3. **Restart the service:**
   ```bash
   ./restart.sh
   ```

### Finding SmartShunt Information

**List all battery services:**
```bash
dbus -y | grep battery
```

**Check a specific SmartShunt:**
```bash
dbus -y com.victronenergy.battery.ttyS5 /DeviceInstance GetValue
dbus -y com.victronenergy.battery.ttyS5 /CustomName GetValue
dbus -y com.victronenergy.battery.ttyS5 /Soc GetValue
```

## How It Works

1. **Discovery**: Finds all SmartShunts on D-Bus (runs every second initially, then backs off exponentially)
2. **UI Switches**: Each discovered SmartShunt gets a toggle switch in the Venus OS UI (Settings -> Switches)
3. **Reactive Monitoring**: Watches for value changes on all enabled SmartShunts
4. **Instant Aggregation**: When any value changes:
   - **Current**: Sums all enabled shunt currents (parallel batteries = currents add)
   - **Voltage**: Smart selection based on alarm states (reports most critical voltage)
   - **SoC**: Capacity-weighted average (accounts for different battery sizes)
   - **Temperature**: Smart selection (reports coldest when near freezing, hottest when overheating, average otherwise)
   - **Alarms**: Logical OR (if any shunt alarms, aggregate alarms)
   - **History**: Aggregates charge cycles, energy throughput, min/max values
5. **Publishing**: Updates virtual SmartShunt service immediately

## Configuration Reference

See `config.default.ini` for comprehensive documentation of all settings.

**All settings have defaults - config file is optional!**

The config file is only needed for advanced operational settings like logging levels, polling intervals, and error handling timeouts. All functional settings (device name, temperature thresholds, SmartShunt selection) are managed via the UI.

## Managing SmartShunts via UI Switches

All SmartShunt discovery and control is now managed via the Venus OS UI:

1. **Discovery Switch**: Navigate to **Settings -> Switches** and find "* SmartShunt Discovery"
   - **ON** (default): Service scans for new SmartShunts and creates switches for them
   - **OFF**: Stops scanning, hides all switches (but continues aggregating enabled shunts)

2. **Individual Shunt Switches**: Each discovered SmartShunt gets its own toggle switch
   - **ON** (default): Shunt is included in the aggregate
   - **OFF**: Shunt is excluded from the aggregate

3. **Temperature Threshold Switches**: Two dimmable slider controls for smart temperature reporting
   - **Cold Limit**: Default 50°F (10°C) - adjustable from -58°F to 212°F (-50°C to 100°C)
     - Below this temperature, the aggregate reports the coldest battery temperature
     - Reset to default by toggling the switch off and back on
   - **Hot Limit**: Default 105°F (40.5°C) - adjustable from -58°F to 212°F (-50°C to 100°C)
     - Above this temperature, the aggregate reports the hottest battery temperature
     - Reset to default by toggling the switch off and back on
   - Between thresholds, the aggregate reports the average temperature
   - The switch label shows the current setting in both Celsius and Fahrenheit

4. **Hiding Switches**: When you're done configuring, turn off "SmartShunt Discovery" to hide all switches from the main UI. They remain accessible in the device settings if you need to change them later.

**Example Use Cases:**
- Exclude a DC loads shunt from your battery aggregate
- Temporarily disable a shunt for testing
- Separate house batteries from starter battery monitoring
- Adjust temperature thresholds for LiFePO4 (wider range) vs Lead-Acid (narrower range)
- Set temperature limits based on battery chemistry charge/discharge windows

## Managing the Service

**View logs:**
```bash
/data/apps/dbus-aggregate-smartshunts/get-logs.sh
```

**Restart after config changes:**
```bash
/data/apps/dbus-aggregate-smartshunts/restart.sh
```

**Disable service:**
```bash
/data/apps/dbus-aggregate-smartshunts/disable.sh
```

**Re-enable service:**
```bash
/data/apps/dbus-aggregate-smartshunts/enable.sh
```

**Uninstall:**
```bash
/data/apps/dbus-aggregate-smartshunts/uninstall.sh
```

## Monitoring

**Check the virtual battery service:**
```bash
dbus -y com.victronenergy.battery.aggregate_shunts / GetItems
```

**View real-time status:**
```bash
watch -n 1 'dbus -y com.victronenergy.battery.aggregate_shunts /Dc/0/Voltage GetValue && \
            dbus -y com.victronenergy.battery.aggregate_shunts /Dc/0/Current GetValue && \
            dbus -y com.victronenergy.battery.aggregate_shunts /Soc GetValue'
```

**Check logs in real-time:**
```bash
tail -f /data/apps/dbus-aggregate-smartshunts/service/log/current | tai64nlocal
```

## Example Setups

### Example 1: Basic Monitoring (Most Common)

**System:**
- 2× 300Ah LiFePO4 batteries in parallel
- 2× Victron SmartShunt 500A/50mV
- Built-in BMS in each battery

**Configuration:**
```ini
# No config.ini needed! 
# Service auto-detects both shunts and calculates 600Ah capacity
```

**Result:**
- Virtual SmartShunt shows 600Ah total capacity
- Current is sum of both shunts
- SoC is capacity-weighted average
- All alarms passed through from physical shunts

### Example 2: Three Shunts with Selective Aggregation

**System:**
- 3× SmartShunts, but one monitors a house battery (not part of the bank)

**Configuration:**
1. All three shunts are discovered automatically
2. Navigate to **Settings -> Switches**
3. Toggle OFF the "House Battery" switch
4. Toggle OFF "SmartShunt Discovery" to hide switches

**Result:**
- Only aggregates the two bank shunts
- House battery remains separate

## Troubleshooting

### Service won't start

**Check logs:**
```bash
tail -n 50 /data/apps/dbus-aggregate-smartshunts/service/log/current | tai64nlocal
```

**Common issues:**
- **Python errors**: Check syntax if you edited the code
- **Permission errors**: Ensure scripts are executable (`chmod +x *.sh`)

### SmartShunts not found

**Verify SmartShunts are visible:**
```bash
dbus -y | grep com.victronenergy.battery
```

**Check each one:**
```bash
dbus -y com.victronenergy.battery.ttyS5 /ProductName GetValue
```

Should see "SmartShunt" in the product name.

### Capacity auto-detection not working

**Requirements for auto-detection:**
- SoC must be between 10-90% (most accurate at 30-70%)
- All SmartShunts must have capacity configured in VictronConnect
- ConsumedAmphours must be available

### SoC seems incorrect

**Check individual shunts:**
```bash
dbus -y com.victronenergy.battery.ttyS5 /Soc GetValue
dbus -y com.victronenergy.battery.ttyS6 /Soc GetValue
```

If individual shunts are wrong, calibrate them in VictronConnect:
- Sync to 100% when batteries are full
- Ensure capacity is configured correctly

## Technical Details

**D-Bus Service:** `com.victronenergy.battery.aggregate_shunts`

**Product ID:** `0xA389` (41865) - SmartShunt

**Key D-Bus Paths:**
- `/Dc/0/Voltage` - Voltage (V)
- `/Dc/0/Current` - Current (A, positive = charging)
- `/Dc/0/Power` - Power (W)
- `/Dc/0/Temperature` - Temperature (°C)
- `/Soc` - State of charge (%)
- `/Capacity` - Remaining capacity (Ah)
- `/InstalledCapacity` - Total capacity (Ah)
- `/ConsumedAmphours` - Energy consumed (Ah)
- `/TimeToGo` - Time remaining (seconds)
- `/History/*` - Aggregated history data
- `/Alarms/*` - Passed through from physical shunts

## Credits

This project is **derived from** [dbus-aggregate-batteries](https://github.com/Dr-Gigavolt/dbus-aggregate-batteries) by **Dr-Gigavolt (Anton Labanc PhD)**.

### Original Work by Anton Labanc PhD

The foundational architecture and many core components come from the original dbus-aggregate-batteries project:
- D-Bus service architecture and monitoring patterns
- Configuration management system
- Service management scripts (install, enable, disable, restart, uninstall)
- Core aggregation logic and algorithms

### Modifications for SmartShunt Aggregation

Adapted and extended by Clinton Goudie-Nice for SmartShunt-specific use:
- Reactive updates (event-driven instead of polling)
- Auto-detection of SmartShunts and capacity
- Smart voltage/temperature algorithms prioritizing battery safety
- Stateless operation (all data derived from physical devices)
- Aggregated history data from physical shunts
- Exponential backoff for device discovery

**Thanks to Anton Labanc PhD for creating the original dbus-aggregate-batteries project and sharing it under the MIT license, making this derivative work easy!**

## License

MIT License - See LICENSE file for full text

**Copyright (c) 2025 Clinton Goudie-Nice**  
**Copyright (c) 2022 Anton Labanc PhD**

This software is derived from dbus-aggregate-batteries by Anton Labanc PhD.
Portions of the original work (D-Bus architecture, configuration management, 
service scripts, and core aggregation logic) are retained and modified.

## Support

For issues, questions, or contributions, please open an issue on GitHub.
