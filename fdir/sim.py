"""Small-sat telemetry simulator with fault injection.

Faults propagate through a causal (system-dynamics) graph with transport delays,
so the first channel to trip a limit is often a *symptom*, not the root cause.
"""
import math
import random

DT = 10            # seconds per sample
N = 720            # 2 hours of telemetry (~1.3 orbits)
ORBIT = 5700       # 95 min orbit
ECLIPSE_FRAC = 0.36

# channel -> (nominal mean, noise sigma, low limit, high limit, unit, subsystem)
CHANNELS = {
    "sa_current":     (6.0, 0.15, -0.5, 9.0, "A", "EPS"),
    "bus_voltage":    (28.0, 0.10, 25.0, 32.0, "V", "EPS"),
    "batt_voltage":   (16.4, 0.05, 14.5, 17.0, "V", "EPS"),
    "batt_soc":       (85.0, 0.30, 40.0, 100.0, "%", "EPS"),
    "batt_temp":      (18.0, 0.30, 0.0, 30.0, "C", "THERMAL"),
    "heater_current": (0.4, 0.03, 0.0, 1.6, "A", "THERMAL"),
    "panel_temp":     (22.0, 0.40, -20.0, 45.0, "C", "THERMAL"),
    "rw_current":     (0.30, 0.02, 0.0, 1.2, "A", "ADCS"),
    "rw_temp":        (25.0, 0.30, -10.0, 50.0, "C", "ADCS"),
    "att_error":      (0.05, 0.01, 0.0, 0.5, "deg", "ADCS"),
    "st_valid":       (1.0, 0.0, 0.5, 1.0, "flag", "ADCS"),
    "comm_temp":      (24.0, 0.30, -10.0, 45.0, "C", "COMMS"),
    "downlink_snr":   (12.0, 0.30, 6.0, 30.0, "dB", "COMMS"),
}

# Causal edges: parent -> [(child, gain, delay_s)]  (signed response per unit of parent deviation)
EDGES = {
    "sa_current":     [("bus_voltage", 0.45, 60)],
    "bus_voltage":    [("batt_soc", 4.0, 300)],
    "batt_soc":       [("batt_voltage", 0.04, 120)],
    "batt_voltage":   [("batt_temp", -6.0, 240)],
    "heater_current": [("panel_temp", 9.0, 300), ("bus_voltage", -0.6, 30), ("batt_temp", 4.0, 600)],
    "rw_current":     [("rw_temp", 18.0, 400), ("att_error", 0.25, 200)],
    "st_valid":       [("att_error", -0.35, 60)],
    "comm_temp":      [("downlink_snr", -0.45, 120)],
}

TOPO = ["sa_current", "heater_current", "bus_voltage", "batt_soc", "batt_voltage", "batt_temp",
        "panel_temp", "rw_current", "rw_temp", "st_valid", "att_error", "comm_temp", "downlink_snr"]

PARENTS = {}
for p, kids in EDGES.items():
    for c, _, _ in kids:
        PARENTS.setdefault(c, []).append(p)

# Fault classes: root channel, step deviation (in channel units), recovery action
FAULTS = {
    "SA_DEGRADATION":      ("sa_current", -2.2, "Re-point arrays / check SADA; shed non-critical loads; plan reduced-power ops."),
    "BATTERY_CELL_SHORT":  ("batt_voltage", -0.9, "Isolate battery string; lower charge rate; enter power-safe mode."),
    "HEATER_STUCK_ON":     ("heater_current", 1.0, "Cycle heater relay; switch to redundant thermostat; open LCL if persists."),
    "RW_BEARING_FRICTION": ("rw_current", 0.45, "Spin down wheel, switch to 3-wheel + magnetorquer mode; schedule lubrication heater."),
    "STAR_TRACKER_BLIND":  ("st_valid", -1.0, "Switch to gyro/sun-sensor attitude; check Sun/Moon exclusion; slew to re-acquire."),
    "TRANSPONDER_THERMAL": ("comm_temp", 14.0, "Reduce TX duty cycle / power level; switch to redundant transponder."),
}


def _eclipse(t):
    return ((t % ORBIT) / ORBIT) > (1 - ECLIPSE_FRAC)


def simulate(fault=None, seed=None, onset=None, severity=None):
    """Return (telemetry dict channel->list, truth dict)."""
    rng = random.Random(seed)
    phase = rng.uniform(0, ORBIT)
    onset = onset if onset is not None else rng.randint(200, 420) * DT
    severity = severity if severity is not None else rng.uniform(0.6, 1.4)
    tel = {c: [] for c in CHANNELS}

    # deviation signal per channel as a function of time (first-order lag through graph)
    dev = {c: [0.0] * N for c in CHANNELS}
    if fault:
        root, step, _ = FAULTS[fault]
        for i in range(N):
            t = i * DT
            if t >= onset:
                ramp = min(1.0, (t - onset) / 600.0)  # faults develop over ~10 min
                dev[root][i] = step * severity * ramp
        # propagate in topological order: each child = own fault + first-order-lag of parents
        for c in TOPO:
            for p in PARENTS.get(c, []):
                g, d = next((g, d) for cc, g, d in EDGES[p] if cc == c)
                lag = d // DT
                tau = max(1, lag)
                acc = 0.0
                for i in range(N):
                    src = dev[p][i - lag] if i - lag >= 0 else 0.0
                    acc += (g * src - acc) / tau
                    dev[c][i] += acc

    for i in range(N):
        t = i * DT + phase
        ecl = _eclipse(t)
        for c, (mu, sd, lo, hi, _, _) in CHANNELS.items():
            v = mu
            if c == "sa_current":
                v = 0.05 if ecl else mu
            elif c == "batt_soc":
                v = mu - (6.0 if ecl else 0.0)
            elif c == "panel_temp":
                v = mu + (-14.0 if ecl else 6.0) * 0.5
            elif c == "batt_temp":
                v = mu + (-1.5 if ecl else 0.5)
            d = dev[c][i]
            if c == "sa_current" and ecl:
                d = 0.0  # eclipse masks solar-array faults
            v = v + d + rng.gauss(0, sd)
            if c == "st_valid":
                v = 1.0 if (1.0 + d) > 0.5 and rng.random() > 0.0003 else 0.0
            tel[c].append(round(v, 4))
    truth = {"fault": fault, "onset": onset, "severity": round(severity, 3), "phase": round(phase, 1)}
    return tel, truth


def eclipse_mask(phase):
    return [_eclipse(i * DT + phase) for i in range(N)]
