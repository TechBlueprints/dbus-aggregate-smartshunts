#!/bin/bash
#
# Disable script for dbus-aggregate-smartshunts
# Cleanly stops and removes the service and all its settings
#

# remove comment for easier troubleshooting
#set -x

INSTALL_DIR="/data/apps/dbus-aggregate-smartshunts"
SERVICE_NAME="dbus-aggregate-smartshunts"

echo
echo "Disabling $SERVICE_NAME..."

# Remove service symlink
rm -rf "/service/$SERVICE_NAME" 2>/dev/null || true

# Kill any remaining processes
pkill -f "supervise $SERVICE_NAME" 2>/dev/null || true
pkill -f "multilog .* /var/log/$SERVICE_NAME" 2>/dev/null || true
pkill -f "python.*$SERVICE_NAME" 2>/dev/null || true
pkill -f "python.*smartshunt" 2>/dev/null || true

# Remove enable script from rc.local
sed -i "/.*$SERVICE_NAME.*/d" /data/rc.local 2>/dev/null || true

echo "Service stopped and rc.local cleaned"

# Clean up D-Bus settings
echo "Cleaning up D-Bus settings..."

# Function to delete a settings path
delete_setting() {
    local path="$1"
    dbus -y com.victronenergy.settings "$path" SetValue "" 2>/dev/null || true
}

# Clean up settings paths for aggregate smartshunts
for path in $(dbus -y com.victronenergy.settings / GetValue 2>/dev/null | grep -oE "Settings/Devices/aggregate_smartshunts/[^']*" | sort -u); do
    echo "  Removing /$path"
    delete_setting "/$path"
done

# Clean up aggregateshunts device settings
for path in $(dbus -y com.victronenergy.settings / GetValue 2>/dev/null | grep -oE "Settings/Devices/aggregateshunts/[^']*" | sort -u); do
    echo "  Removing /$path"
    delete_setting "/$path"
done

echo
echo "$SERVICE_NAME disabled and settings cleaned"
echo
echo "Note: To completely remove, also delete: $INSTALL_DIR"
echo
