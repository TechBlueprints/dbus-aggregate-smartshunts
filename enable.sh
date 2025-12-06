#!/bin/bash
#
# Enable script for dbus-aggregate-smartshunts
# This script is run on every boot via rc.local to ensure the service is properly set up
#

# remove comment for easier troubleshooting
#set -x

INSTALL_DIR="/data/apps/dbus-aggregate-smartshunts"
SERVICE_NAME="dbus-aggregate-smartshunts"

# Fix permissions
chmod +x "$INSTALL_DIR"/*.sh 2>/dev/null || true
chmod +x "$INSTALL_DIR"/*.py 2>/dev/null || true
chmod +x "$INSTALL_DIR"/service/run 2>/dev/null || true
chmod +x "$INSTALL_DIR"/service/log/run 2>/dev/null || true

# Create rc.local if it doesn't exist
if [ ! -f /data/rc.local ]; then
    echo "#!/bin/bash" > /data/rc.local
    chmod 755 /data/rc.local
fi

# Remove ALL old entries for this service from rc.local (aggressive cleanup)
sed -i "/.*$SERVICE_NAME.*/d" /data/rc.local

# Add enable script to rc.local (runs in background with logging)
RC_ENTRY="bash $INSTALL_DIR/enable.sh > $INSTALL_DIR/startup.log 2>&1 &"
echo "$RC_ENTRY" >> /data/rc.local

# Stop service if running
if [ -d "/service/$SERVICE_NAME" ]; then
    svc -d "/service/$SERVICE_NAME" 2>/dev/null || true
fi
sleep 1

# Kill any remaining processes
pkill -f "supervise $SERVICE_NAME" 2>/dev/null || true
pkill -f "multilog .* /var/log/$SERVICE_NAME" 2>/dev/null || true
pkill -f "python.*$SERVICE_NAME" 2>/dev/null || true

# Create symlink to service directory
if [ -L "/service/$SERVICE_NAME" ]; then
    rm "/service/$SERVICE_NAME"
fi
ln -s "$INSTALL_DIR/service" "/service/$SERVICE_NAME"

echo "$SERVICE_NAME enabled"
