#!/bin/bash
#
# Disable script for dbus-aggregate-smartshunts
# Cleanly stops and removes the service
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

# Remove enable script from rc.local
sed -i "/.*$SERVICE_NAME.*/d" /data/rc.local 2>/dev/null || true

echo "$SERVICE_NAME disabled"
echo
