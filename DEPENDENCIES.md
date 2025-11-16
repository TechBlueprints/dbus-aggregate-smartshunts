# Dependencies

This project bundles external Python libraries in the `ext/` directory for deployment to Venus OS systems, which typically lack internet access and package management tools.

## Why Bundle Dependencies?

Venus OS (Cerbo GX, Venus GX, etc.) systems:
- **No pip or package manager** - cannot install from PyPI
- **Limited/no internet access** - especially in marine/RV environments
- **Immutable root filesystem** - custom software must be self-contained

This approach follows the pattern used by Victron's own [dbus-serialbattery](https://github.com/mr-manuel/venus-os_dbus-serialbattery) project.

## Bundled Libraries

### velib_python
- **Purpose**: Venus OS D-Bus integration library
- **Source**: https://github.com/victronenergy/velib_python
- **License**: MIT
- **Why needed**: 
  - Creates D-Bus service for the aggregated SmartShunt battery device
  - Monitors multiple SmartShunt devices via DbusMonitor
  - Manages device settings via SettingsDevice
  - Publishes aggregated battery metrics to Venus OS GUI/VRM

## Architecture Note

This service monitors multiple Victron SmartShunt devices on the D-Bus, aggregates their readings (voltage, current, SOC, temperature), and publishes a unified battery device. It provides UI-controlled temperature thresholds and SmartShunt selection via SwitchableOutput controls.

## Updating Dependencies

To update velib_python:

1. Clone the latest version:
   ```bash
   git clone https://github.com/victronenergy/velib_python /tmp/velib_python
   ```

2. Copy to `ext/`:
   ```bash
   cp -r /tmp/velib_python ext/
   ```

3. Test on Venus OS to ensure compatibility

4. Update this file with the new commit/version

## For Developers

When developing locally, you can install velib_python from source:

```bash
git clone https://github.com/victronenergy/velib_python
export PYTHONPATH="${PYTHONPATH}:$(pwd)/velib_python"
```

The code will preferentially import from `ext/velib_python/` when running on Venus OS.

