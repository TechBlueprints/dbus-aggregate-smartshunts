#!/bin/bash

# Disable dbus-aggregate-smartshunts service

SERVICE_LINK="/service/dbus-aggregate-smartshunts"

echo "=== Disabling dbus-aggregate-smartshunts service ==="

if [ -L "$SERVICE_LINK" ]; then
    echo "Stopping service..."
    svc -d "$SERVICE_LINK"
    sleep 2
    
    echo "Removing service link..."
    rm "$SERVICE_LINK"
    
    echo "Service disabled!"
else
    echo "Service link not found. Already disabled?"
fi

echo ""

