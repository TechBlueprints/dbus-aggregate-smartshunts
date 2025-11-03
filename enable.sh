#!/bin/bash

# Enable dbus-aggregate-smartshunts service on Venus OS

SERVICE_DIR="/data/apps/dbus-aggregate-smartshunts"
SERVICE_LINK="/service/dbus-aggregate-smartshunts"

echo "=== Enabling dbus-aggregate-smartshunts service ==="

# Remove old service link if it exists
if [ -L "$SERVICE_LINK" ]; then
    echo "Removing old service link..."
    rm "$SERVICE_LINK"
fi

# Create new service link
echo "Creating service link..."
ln -s "$SERVICE_DIR/service" "$SERVICE_LINK"

# Wait for service to start
sleep 2

# Check service status
svstat "$SERVICE_LINK"

echo ""
echo "Service enabled!"
echo ""
echo "To check logs:"
echo "  tail -f $SERVICE_DIR/service/log/current"
echo ""
echo "To restart:"
echo "  $SERVICE_DIR/restart.sh"
echo ""

