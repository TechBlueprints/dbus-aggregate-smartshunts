"""Tests for the reactive-update gating helpers in ``gate.py``.

The gating logic decides whether ``_update()`` should write to D-Bus
on a given cycle.  Wrong semantics here means either spurious emits
(perf regression) or missed updates (consumers see stale data).
"""

from __future__ import annotations

import pytest

import gate


# Convenient test-only thresholds — easier to reason about than the
# production values.  Tests that need to exercise the production
# thresholds use ``gate.AGGREGATE_THRESHOLDS`` directly.
T = {
    "/V": 0.5,
    "/I": 1.0,
    "/P": 10.0,
}


class TestNoLastValues:
    """First call (empty last_values dict) — any non-None new value is substantial."""

    def test_empty_last_with_value(self):
        assert gate._is_substantial({"/V": 12.3}, {}, T) is True

    def test_empty_last_all_none(self):
        # None values are skipped; if nothing else is set, nothing is substantial.
        assert gate._is_substantial({"/V": None, "/I": None}, {}, T) is False

    def test_empty_last_some_none(self):
        assert gate._is_substantial({"/V": None, "/I": 5.0}, {}, T) is True


class TestThresholdBoundary:
    """The comparison is ``>=`` so a value exactly at the threshold triggers."""

    def test_exactly_at_threshold(self):
        # Threshold for /V is 0.5; new minus last is exactly 0.5 → substantial.
        assert gate._is_substantial({"/V": 12.5}, {"/V": 12.0}, T) is True

    def test_just_under_threshold(self):
        assert gate._is_substantial({"/V": 12.49}, {"/V": 12.0}, T) is False

    def test_just_over_threshold(self):
        assert gate._is_substantial({"/V": 12.51}, {"/V": 12.0}, T) is True

    def test_exact_equal(self):
        assert gate._is_substantial({"/V": 12.0}, {"/V": 12.0}, T) is False


class TestSignedDelta:
    """Use ``abs()`` so negative and positive moves of the same magnitude trigger equally."""

    def test_negative_move_over_threshold(self):
        # 12.0 -> 11.5 = 0.5 delta = threshold = substantial.
        assert gate._is_substantial({"/V": 11.5}, {"/V": 12.0}, T) is True

    def test_negative_move_under_threshold(self):
        assert gate._is_substantial({"/V": 11.6}, {"/V": 12.0}, T) is False

    def test_zero_crossing(self):
        # /I 0.4 -> -0.4 = 0.8 delta < 1.0 threshold → not substantial.
        assert gate._is_substantial({"/I": -0.4}, {"/I": 0.4}, T) is False
        # /I 0.6 -> -0.6 = 1.2 delta >= 1.0 → substantial.
        assert gate._is_substantial({"/I": -0.6}, {"/I": 0.6}, T) is True


class TestNoneAndMissing:
    """None values are skipped; paths without thresholds are skipped."""

    def test_none_new_is_skipped(self):
        # /V is None even though last had a value.  Skipped → not substantial.
        assert gate._is_substantial({"/V": None}, {"/V": 12.0}, T) is False

    def test_path_without_threshold_is_skipped(self):
        # /Other is in new_values but not in thresholds.  Should be skipped
        # even if last has no value (no first-sample bonus).
        assert gate._is_substantial({"/Other": 999.0}, {}, T) is False

    def test_path_without_threshold_does_not_mask_others(self):
        # Even if an ungated path looks unchanged, a gated path's
        # threshold crossing still triggers.
        assert gate._is_substantial(
            {"/Other": 1.0, "/V": 13.0},
            {"/Other": 1.0, "/V": 12.0},
            T,
        ) is True


class TestMultiplePaths:
    """Any one path crossing its threshold is enough — short-circuit semantics."""

    def test_one_of_three_crosses(self):
        new = {"/V": 12.0, "/I": 5.0, "/P": 70.0}
        last = {"/V": 12.0, "/I": 5.0, "/P": 50.0}   # /P moved by 20 ≥ 10
        assert gate._is_substantial(new, last, T) is True

    def test_none_cross(self):
        new = {"/V": 12.1, "/I": 5.4, "/P": 58.0}
        last = {"/V": 12.0, "/I": 5.0, "/P": 50.0}
        # All three moves: 0.1 (< 0.5), 0.4 (< 1.0), 8 (< 10) — none substantial.
        assert gate._is_substantial(new, last, T) is False


class TestProductionThresholds:
    """Smoke tests against the real ``AGGREGATE_THRESHOLDS`` so a
    future tweak to those values doesn't silently make a real scenario
    flip behaviour."""

    def test_house_battery_idle_no_emit(self):
        # Small idle flicker below every threshold: voltage 13.78 ↔ 13.82
        # (0.04 V < 0.05), current 0 ↔ 0.3 A (< 0.5), power 0 ↔ 3 W (< 5).
        # Note: the current/power gates now sit *on* the idle-flicker floor
        # (0.5 A / 5 W), so a full-swing flicker would emit by design —
        # see test_idle_full_flicker_emits.  This case stays gated.
        new = {
            "/Dc/0/Voltage": 13.82,
            "/Dc/0/Current": 0.3,
            "/Dc/0/Power":   3,
            "/Soc":          99.5,
        }
        last = {
            "/Dc/0/Voltage": 13.78,
            "/Dc/0/Current": 0.0,
            "/Dc/0/Power":   0,
            "/Soc":          99.4,
        }
        assert gate._is_substantial(new, last, gate.AGGREGATE_THRESHOLDS) is False

    def test_idle_full_flicker_emits(self):
        # Accepted tradeoff of the tight current gate: a 0.5 A idle swing
        # is exactly the threshold (>=) so it emits.  Documented, not a bug.
        new = {"/Dc/0/Current": 0.5}
        last = {"/Dc/0/Current": 0.0}
        assert gate._is_substantial(new, last, gate.AGGREGATE_THRESHOLDS) is True

    def test_fridge_kicks_on(self):
        # 200 W load comes on — power moves 0 → 200, current 0 → 16.  Definitely substantial.
        new = {"/Dc/0/Power": 200, "/Dc/0/Current": 16}
        last = {"/Dc/0/Power": 0, "/Dc/0/Current": 0}
        assert gate._is_substantial(new, last, gate.AGGREGATE_THRESHOLDS) is True

    def test_soc_drift(self):
        # SoC moved by exactly 1.0 — threshold is 1.0, ``>=`` → substantial.
        assert gate._is_substantial(
            {"/Soc": 78.0}, {"/Soc": 79.0}, gate.AGGREGATE_THRESHOLDS
        ) is True


class TestThresholdCoverage:
    """Lock in the set of paths the aggregator gates so a future
    addition (e.g. a new aggregate field) is a forced design decision."""

    def test_expected_paths_in_thresholds(self):
        # If the aggregator publishes a new path, the test author has
        # to decide whether it needs gating.  This list is the
        # canonical set of "fast-changing values worth gating."
        expected = {
            "/Dc/0/Voltage", "/Dc/0/Current", "/Dc/0/Power",
            "/Dc/0/Temperature", "/Soc", "/ConsumedAmphours",
            "/TimeToGo",
        }
        assert set(gate.AGGREGATE_THRESHOLDS.keys()) == expected


class TestHeartbeatConstant:
    """Lock in the integer value so an accidental edit trips a test."""

    def test_heartbeat_is_fifteen_minutes(self):
        assert gate.HEARTBEAT_INTERVAL_S == 900

    def test_no_debounce_constant(self):
        # The time debounce was removed; the value-based gate is the
        # sole rate-limiter.  Guard against it creeping back in.
        assert not hasattr(gate, "DEBOUNCE_INTERVAL_MS")


class TestVoltageResponsiveness:
    """Voltage feeds a downstream control/alarm loop, so the gate must
    react to small voltage moves that the old 0.2 V threshold swallowed."""

    def test_voltage_threshold_is_tight(self):
        assert gate.AGGREGATE_THRESHOLDS["/Dc/0/Voltage"] == 0.05

    def test_small_voltage_rise_is_substantial(self):
        # A 0.1 V rise toward an over-voltage limit must emit so the
        # control loop can react — the old 0.2 V gate would have hidden it.
        assert gate._is_substantial(
            {"/Dc/0/Voltage": 14.95}, {"/Dc/0/Voltage": 14.85},
            gate.AGGREGATE_THRESHOLDS,
        ) is True

    def test_sub_noise_voltage_flicker_still_gated(self):
        # 0.04 V idle flicker stays below the 0.05 V floor → no emit.
        assert gate._is_substantial(
            {"/Dc/0/Voltage": 13.82}, {"/Dc/0/Voltage": 13.78},
            gate.AGGREGATE_THRESHOLDS,
        ) is False
