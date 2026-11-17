# Shunt Bus-Link Detection — Design Spec (v3, one rule)

Status: DESIGN ONLY — not implemented, thresholds not yet approved, and
none of the "Verify before trusting" checks have been run. Design
discussion ran 2026-07-01 to 2026-08-10.

## Problem

The aggregate's `/Dc/0/Voltage` feeds the DVCC/SVS charge loop
(`SharedVoltageSense=1`; `/Dc/Battery/VoltageService` *is* the
aggregate). A battery disconnected from the bus leaves its SmartShunt
powered via `Vbatt+`, reporting the isolated battery's resting voltage
(~13.3 V) at 0 A. Averaged in, it holds the reported voltage below the
real bus, so the chargers ramp forever. Observed twice: 15.2 V.

## The rule

A shunt is **DISCONNECTED** when, continuously for `T_FLAG`:

```
I == 0.0            — exactly-zero current: an open circuit carries
                      NOTHING, so any nonzero reading (even 0.1 A, the
                      shunt's smallest step) is proof of connection.
                      Implemented as abs(I) < 0.05 — float hygiene
                      below the 0.1 A reporting step, not a tunable.
AND V < V_HELD      — resting-band voltage: an isolated LiFePO4 relaxes
                      to ≤13.4 V; a connected battery is either held
                      above that by a charger (14.4 abs / 13.8 float)
                      or is carrying current (no charger ⇒ the 24/7
                      house loads run off the batteries)
```

It is **CONNECTED** again when, continuously for `T_CLEAR`:

```
I != 0.0   OR   V >= V_HELD
```

No current hysteresis is needed: a connected battery flickering
0.0/0.1 in a deep lull resets the flag timer on every 0.1 blip, while
a genuinely open circuit can never produce one. The time windows do
all the damping.

Persistence is wall-clock monotonic (never update-cycle counts —
`_update()` is event-driven and can fire many times per second).

| Key | Default | Notes |
|---|---|---|
| `LINK_V_HELD` | 13.55 V | LFP resting ≤13.4 vs float 13.8 here. **Chemistry-dependent** — lead-acid rigs must lower it |
| `LINK_T_FLAG_S` | 60 s | flap-proof; ≤1 min exposure on a charge-time disconnect |
| `LINK_T_CLEAR_S` | 5 s | fast restore; rejects a spurious one-sample blip |

## Response

| State | Voltage | SoC / Current / Power / TTG | Alarm |
|---|---|---|---|
| all connected | normal | normal | — |
| some flagged | trusted-only aggregate | trusted-only | `/Alarms/Alarm = 1` |
| all flagged | **invalidate (None)** — SVS loses its sense value, every charger reverts to regulating its own terminals, capped at its setpoints | invalidate | `/Alarms/Alarm = 2` |

Diagnostics: per-shunt `/Aggregate/Shunt<N>/LinkStatus` (0/1).
Housekeeping: drop link state for shunts disabled via UI switch or
whose service disappears.

## Case coverage

| Scenario | Outcome |
|---|---|
| one battery out during charge | flagged ≤60 s (13.3/0.0 vs held bus), excluded |
| both out during charge (fault 1) | both flagged, voltage invalidated, chargers cap — no runaway |
| out during quiet, charger ramps at dawn | flagged overnight, long before ramp |
| full battery tapered to 0 A in absorption | V=14.4 ≥ V_HELD → trusted |
| shore-power float | V=13.8 → trusted |
| off-grid night lull | any nonzero reading (0.1+) → trusted |
| charger shutoff (dusk/shore drop) | batteries pick up loads → current → trusted |
| rig intentionally dark (storage disconnect) | flagged + alarm — factually correct |

## Accepted limits

- ~60 s detection latency (no fast-path fingerprint) → up to a minute
  of mild overshoot before exclusion. Bounded; fine for LFP.
- Surface charge: a battery disconnected right after absorption can
  read >13.55 V for ~30–60 min before relaxing into detection.
  Self-resolving; bus overshoot in that window is small (its stale
  reading is near float anyway).
- `V_HELD` is chemistry-specific.
- Relies on the never-idle-bus prior (true here: 24/7 loads, ~22 A
  observed). A dead-idle rig degrades to a false flag — which fails
  safe (invalid voltage while nothing is charging is harmless).

## Verify before trusting

1. **Linchpin:** invalid `/Dc/0/Voltage` from the active battery
   service makes DVCC/SVS stop compensating (chargers revert to
   terminal regulation), not hold stale / auto-select a bogus fallback.
2. `/Soc = None` handling by systemcalc/GUI/VRM.
3. Both SmartShunts' "Current threshold" setting in VictronConnect: must
   be at the 0.1 A default. If raised (e.g. 0.5 A), real lull currents
   *report* as 0.0 and the exactly-zero test over-flags.

## Implementation

1. **Commit 1** — rewrite `link_detect.py` to the one rule (injectable
   clock), rewrite tests, stale-state cleanup.
2. **Commit 2** — response ladder in the aggregator: trusted-only
   publishing (exists), invalidation, alarms, LinkStatus paths.
3. **Validation** — unit suite; device linchpin test; fault injection
   (open one breaker during charge → flag ≤60 s; reconnect → clear
   ≤5 s; both breakers → invalidation, chargers cap, no ramp).

## History

v1 had peer-gap detection + a charger "tiebreak" observer; v2 removed
the observer (island-only) but kept a step-event fingerprint, peer
reference, and SOFT/HARD grading. v3 keeps only the island rule: the
grading existed to manage overnight false positives, which requiring
*exactly-zero* current (an open circuit cannot move even 0.1 A;
"idle" thresholds like 0.5 A were the false-positive source)
eliminates at the source. Current thresholds dropped entirely in
favor of the exact-zero test — config is three knobs. The
fingerprint/peer layers can return later as accelerators if 60 s
latency ever proves too slow — additive, not foundational.
