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
   
   # Capacity is automatically detected from SmartShunt configuration (no setting needed)
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
2. **Reactive Monitoring**: Watches for value changes on all SmartShunts
3. **Instant Aggregation**: When any value changes:
   - **Current**: Sums all shunt currents (parallel batteries = currents add)
   - **Voltage**: Smart selection based on alarm states (reports most critical voltage)
   - **SoC**: Capacity-weighted average (accounts for different battery sizes)
   - **Temperature**: Smart selection (reports coldest when near freezing, hottest when overheating, average otherwise)
   - **Alarms**: Logical OR (if any shunt alarms, aggregate alarms)
   - **History**: Aggregates charge cycles, energy throughput, min/max values
4. **Publishing**: Updates virtual SmartShunt service immediately

## Configuration Reference

See `config.default.ini` for comprehensive documentation of all settings.

**Key settings:**

| Setting | Default | Description |
|---------|---------|-------------|
| `DEVICE_NAME` | Auto | Custom name for the aggregate device |
| `EXCLUDE_SHUNTS` | None | Comma-separated list of shunt names/IDs to exclude |
| `TEMP_COLD_DANGER` | `5.0` | Report MIN temp below this (°C) |
| `TEMP_HOT_DANGER` | `45.0` | Report MAX temp above this (°C) |

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

### Example 2: Three Shunts with Exclusion

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
