# dbus-aggregate-smartshunts

A Victron Venus OS service that aggregates multiple SmartShunts into a single virtual battery monitor or virtual BMS.

> **Note:** This project is derived from [dbus-aggregate-batteries](https://github.com/Dr-Gigavolt/dbus-aggregate-batteries) by Anton Labanc PhD and adapted for SmartShunt aggregation.

## Purpose

When you have multiple batteries in parallel, each with their own SmartShunt, Venus OS treats them as separate batteries. This service combines them into a single virtual device, providing accurate monitoring of your complete battery bank.

**Key Benefits:**
- 🎯 **Combined monitoring** - See your entire battery bank as one device
- 📊 **Accurate SoC** - Capacity-weighted state of charge across all batteries
- ⚡ **Reactive updates** - Instant response when any SmartShunt reports changes
- 🔧 **Zero configuration** - Auto-detects SmartShunts and calculates capacity
- 🛡️ **Smart protection** - Intelligent voltage and temperature reporting prioritizes battery safety
- 🎚️ **Optional BMS mode** - Can act as virtual BMS to control charging (for "dumb" batteries)

## Features

### Monitor Mode (Default)
- ✅ Auto-discovers all SmartShunts on the system
- ✅ Auto-detects total capacity from SmartShunt configurations
- ✅ Reactive updates (no polling delay)
- ✅ Combines current (sum of all shunts)
- ✅ Smart voltage reporting (prioritizes safety - reports minimum on low voltage alarm, maximum on high voltage alarm)
- ✅ Smart temperature reporting (prioritizes danger - reports coldest when near freezing, hottest when overheating)
- ✅ Capacity-weighted SoC calculation
- ✅ Passes through all alarms from physical shunts
- ✅ Aggregates history data (charge cycles, energy throughput, etc.)
- ✅ Completely stateless - all data derived from physical SmartShunts

### BMS Mode (Experimental)
- ✅ All monitor mode features, PLUS:
- ✅ Publishes charge control limits (CVL/CCL/DCL) for DVCC
- ✅ Acts as virtual Battery Management System
- ✅ Controls Multi/Quattro/MPPT charging through DVCC
- ✅ Provides `/Io/AllowToCharge` and `/Io/AllowToDischarge` flags

## Installation

### Prerequisites

- Victron Cerbo GX or Venus GX running Venus OS
- 2+ SmartShunts connected and visible on D-Bus
- SSH access to your Venus device

### Quick Install

1. **Copy files to your Venus device:**
   ```bash
   scp -r dbus-aggregate-smartshunts root@cerbo:/data/apps/
   ```

2. **SSH to your Venus device:**
   ```bash
   ssh root@cerbo
   ```

3. **Run the installation script:**
   ```bash
   cd /data/apps/dbus-aggregate-smartshunts
   ./install.sh
   ```

4. **Enable the service:**
   ```bash
   ./enable.sh
   ```

That's it! The service will:
- Auto-discover all SmartShunts
- Auto-detect total capacity
- Start aggregating immediately

### Optional Configuration

**Most users don't need a config file!** But if you want to customize:

1. **Create your config:**
   ```bash
   cp config.default.ini config.ini
   nano config.ini
   ```

2. **Recommended settings to review:**
   ```ini
   [DEFAULT]
   
   # Temperature thresholds (adjust for your battery chemistry)
   TEMP_COLD_DANGER = 5.0    # LiFePO4: 5°C, Lead-Acid: 0°C
   TEMP_HOT_DANGER = 45.0    # LiFePO4: 45°C, Lead-Acid: 40°C
   
   # Exclude specific shunts (leave empty to include all)
   EXCLUDE_SHUNTS = 
   
   # Manual capacity (leave empty for auto-detection)
   TOTAL_CAPACITY = 
   ```

3. **Enable charge control mode (only if needed):**
   ```ini
   # Only set these if you have "dumb" batteries without BMS and want charge control
   DEVICE_MODE = virtual-bms
   MAX_CHARGE_VOLTAGE = 14.6          # e.g., 14.6V for LiFePO4
   MAX_CHARGE_CURRENT = 300           # Combined limit for ALL batteries
   MAX_DISCHARGE_CURRENT = 600        # Combined limit for ALL batteries
   ```

4. **Restart the service:**
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
dbus -y com.victronenergy.battery.ttyS5 /InstalledCapacity GetValue
```

## How It Works

### Monitor Mode (Default)

1. **Discovery**: Finds all SmartShunts on D-Bus (runs every second initially, then backs off exponentially)
2. **Reactive Monitoring**: Watches for value changes on all SmartShunts
3. **Instant Aggregation**: When any value changes:
   - **Current**: Sums all shunt currents (parallel batteries = currents add)
   - **Voltage**: Smart selection based on alarm states (reports most critical voltage)
   - **SoC**: Capacity-weighted average (accounts for different battery sizes)
   - **Temperature**: Smart selection (reports coldest when near freezing, hottest when overheating, average otherwise)
   - **Alarms**: Logical OR (if any shunt alarms, aggregate alarms)
   - **History**: Aggregates charge cycles, energy throughput, min/max values
4. **Publishing**: Updates virtual SmartShunt service immediately

### BMS Mode (Optional)

Same as Monitor Mode, but also:
- Publishes `/Info/MaxChargeVoltage`, `/Info/MaxChargeCurrent`, `/Info/MaxDischargeCurrent`
- DVCC reads these limits and applies them to all Multi/Quattro/MPPT devices
- Publishes `/Io/AllowToCharge` and `/Io/AllowToDischarge` (both set to 1 by default)
- Appears as "BMS" in Venus OS interface (ProductId 0xBA77 instead of 0xA389)

## Configuration Reference

See `config.default.ini` for comprehensive documentation of all settings.

**Key settings:**

| Setting | Default | Description |
|---------|---------|-------------|
| `DEVICE_MODE` | `monitor` | Device mode: `monitor` or `virtual-bms` |
| `DEVICE_NAME` | Auto | Custom name for the aggregate device |
| `TOTAL_CAPACITY` | Auto-detect | Total capacity in Ah (leave empty for auto-detection) |
| `EXCLUDE_SHUNTS` | None | Comma-separated list of shunt names/IDs to exclude |
| `TEMP_COLD_DANGER` | `5.0` | Report MIN temp below this (°C) |
| `TEMP_HOT_DANGER` | `45.0` | Report MAX temp above this (°C) |
| `MAX_CHARGE_VOLTAGE` | Disabled | CVL for charge control modes (V) |
| `MAX_CHARGE_CURRENT` | Disabled | CCL for charge control modes (A, combined) |
| `MAX_DISCHARGE_CURRENT` | Disabled | DCL for charge control modes (A, combined) |

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

### Example 2: BMS Mode for "Dumb" Batteries

**System:**
- 2× 300Ah LiFePO4 batteries (no accessible BMS)
- 2× Victron SmartShunt 500A/50mV
- Each battery: 150A charge max, 300A discharge max

**Configuration:**
```ini
[DEFAULT]

# Device mode: virtual-bms for charge control
DEVICE_MODE = virtual-bms

# Battery limits (these control the entire system through DVCC)
MAX_CHARGE_VOLTAGE = 14.6      # 3.65V per cell for 4S LiFePO4
MAX_CHARGE_CURRENT = 300       # 150A + 150A (combined)
MAX_DISCHARGE_CURRENT = 600    # 300A + 300A (combined)
```

**Result:**
- Appears as "Aggregate BMS" in Venus OS (ProductId 0xBA77)
- DVCC enforces 14.6V / 300A charge / 600A discharge limits
- Dynamically disables charge/discharge based on alarms
- Controls all Multi/Quattro/MPPT devices automatically

### Example 3: Three Shunts with Exclusion

**System:**
- 3× SmartShunts, but one monitors a house battery (not part of the bank)

**Configuration:**
```ini
[DEFAULT]

# Exclude the house battery shunt
EXCLUDE_SHUNTS = "House Battery"
```

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
- **Config errors**: Check `config.ini` syntax (or delete it to use defaults)
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

**Manual override:**
```ini
TOTAL_CAPACITY = 600  # Set manually if auto-detection fails
```

### SoC seems incorrect

**Check individual shunts:**
```bash
dbus -y com.victronenergy.battery.ttyS5 /Soc GetValue
dbus -y com.victronenergy.battery.ttyS6 /Soc GetValue
```

If individual shunts are wrong, calibrate them in VictronConnect:
- Sync to 100% when batteries are full
- Ensure capacity is configured correctly

### BMS mode not controlling charging

**Verify DVCC is enabled:**
- Venus OS → Settings → DVCC → Enable DVCC

**Check that aggregate is the active battery:**
- Venus OS → Settings → System Setup → Battery Monitor
- Should show "Aggregate BMS" or similar

**Verify paths are published:**
```bash
dbus -y com.victronenergy.battery.aggregate_shunts /Info/MaxChargeVoltage GetValue
```

## Technical Details

**D-Bus Service:** `com.victronenergy.battery.aggregate_shunts`

**Product IDs:**
- `0xA389` (41865) - Monitor mode (SmartShunt)
- `0xBA77` (47735) - BMS mode (Battery Management System)

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
- `/Info/MaxChargeVoltage` - CVL (V, BMS mode only)
- `/Info/MaxChargeCurrent` - CCL (A, BMS mode only)
- `/Info/MaxDischargeCurrent` - DCL (A, BMS mode only)
- `/Io/AllowToCharge` - Charge enable flag (BMS mode only)
- `/Io/AllowToDischarge` - Discharge enable flag (BMS mode only)

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
- Optional BMS mode with configurable charge control
- Stateless operation (all data derived from physical devices)
- Aggregated history data from physical shunts
- Exponential backoff for device discovery

**Special thanks to Anton Labanc PhD for creating the original dbus-aggregate-batteries project and sharing it under the MIT license, making this derivative work possible!**

## License

MIT License - See LICENSE file for full text

**Copyright (c) 2025 Clinton Goudie-Nice**  
**Copyright (c) 2022 Anton Labanc PhD**

This software is derived from dbus-aggregate-batteries by Anton Labanc PhD.
Portions of the original work (D-Bus architecture, configuration management, 
service scripts, and core aggregation logic) are retained and modified.

## Support

For issues, questions, or contributions, please open an issue on GitHub.
