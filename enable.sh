#!/bin/bash
#
# Enable script for dbus-aggregate-smartshunts
# This script is run on every boot via rc.local to ensure the service is properly set up
#

# Fix permissions
chmod +x /data/apps/dbus-aggregate-smartshunts/*.py
chmod +x /data/apps/dbus-aggregate-smartshunts/service/run
chmod +x /data/apps/dbus-aggregate-smartshunts/service/log/run

# Create rc.local if it doesn't exist
if [ ! -f /data/rc.local ]; then
    echo "#!/bin/bash" > /data/rc.local
    chmod 755 /data/rc.local
fi

# Add enable script to rc.local (runs on every boot)
RC_ENTRY="bash /data/apps/dbus-aggregate-smartshunts/enable.sh"
grep -qxF "$RC_ENTRY" /data/rc.local || echo "$RC_ENTRY" >> /data/rc.local

# Remove old-style symlink-only entries from rc.local
sed -i '/ln -sf \/data\/apps\/dbus-aggregate-smartshunts\/service \/service\/dbus-aggregate-smartshunts/d' /data/rc.local

# Create symlink to service directory
if [ -L /service/dbus-aggregate-smartshunts ]; then
    rm /service/dbus-aggregate-smartshunts
fi
ln -s /data/apps/dbus-aggregate-smartshunts/service /service/dbus-aggregate-smartshunts

echo "dbus-aggregate-smartshunts enabled"
