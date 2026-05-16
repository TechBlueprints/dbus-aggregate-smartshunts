"""
Pure-Python helpers for the aggregator's reactive-update gating.

Extracted from ``dbus-aggregate-smartshunts.py`` so unit tests can
exercise the threshold-comparison logic without pulling in
dbus-python, gi.repository, vedbus, dbusmonitor, etc. — none of
which are installed in a typical CI / dev environment.

The aggregator imports the constants and ``_is_substantial`` from
this module.
"""


# ── Reactive-update gating (perf) ────────────────────────────────────────
#
# Coarser thresholds = quieter bus, less responsive.  Values tuned for a
# 12 V house bank where natural noise is sub-50 mV / sub-100 mA but real
# events (load on/off, sun coming up) easily exceed.
AGGREGATE_THRESHOLDS = {
    "/Dc/0/Voltage":      0.2,    # V  — half a percent at 12 V; above noise
    "/Dc/0/Current":      2.0,    # A  — fridge inrush still trips
    "/Dc/0/Power":        25,     # W  — small loads still register
    "/Dc/0/Temperature":  1.0,    # °C — battery temps drift slowly
    "/Soc":               1.0,    # %
    "/ConsumedAmphours":  0.5,    # Ah
    "/TimeToGo":          120,    # s  — 2 min
}

# 1-second debounce on the reactive trigger.  Collapses bursts of
# per-shunt PropertiesChanged signals into one aggregation pass.
DEBOUNCE_INTERVAL_MS = 1000

# Force a full write at least this often even when nothing crosses a
# threshold, so freshness watchers (VRM uptime, GUI "data is fresh"
# indicators) can tell the aggregator is alive.
HEARTBEAT_INTERVAL_S = 900


def _is_substantial(new_values: dict, last_values: dict, thresholds: dict) -> bool:
    """Return True iff at least one path in *new_values* has moved
    by at least its threshold (or has no prior value).

    Used inside ``_update()`` to decide whether to write to D-Bus at
    all.  Returning False means we skip the whole ``with
    self._dbusservice`` block, and vedbus emits no ``ItemsChanged``
    signal for this cycle.

    Semantics:
      * ``None`` values in ``new_values`` are skipped (no data yet
        from that path).
      * Paths without an entry in ``thresholds`` are skipped (caller
        chose not to gate on them).
      * ``last_values.get(path) is None`` means "first sample" —
        always substantial.
      * The comparison is ``>=``, so a value sitting *exactly* on
        the threshold boundary triggers an emit.
    """
    for path, val in new_values.items():
        if val is None:
            continue
        threshold = thresholds.get(path)
        if threshold is None:
            continue
        last = last_values.get(path)
        if last is None or abs(val - last) >= threshold:
            return True
    return False
