#!/bin/bash

# Show logs from dbus-aggregate-smartshunts

LOG_DIR="/data/apps/dbus-aggregate-smartshunts/service/log"

if [ -d "$LOG_DIR" ]; then
    echo "=== dbus-aggregate-smartshunts logs ==="
    echo "Press Ctrl+C to exit"
    echo ""
    tail -f "$LOG_DIR/current" | tai64nlocal
else
    echo "Error: Log directory not found at $LOG_DIR"
    exit 1
fi

