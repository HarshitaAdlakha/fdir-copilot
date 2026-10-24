"""Benchmark: naive limit-check console vs FDIR agent variants on fault-injected passes.

Online protocol: passes arrive in random order; after each diagnosis the operator-confirmed
truth is written to incident memory, so the memory-using agent improves over time.
"""
import json
import random
import sys
import time

from .sim import simulate, FAULTS, DT
from .agent import limit_check, run_agent

def run(n_per_fault=50, seed=7):
    rng = random.Random(seed)
    jobs = [(f, rng.randrange(10**9)) for f in FAULTS for _ in range(n_per_fault)]
    rng.shuffle(jobs)
    memory = []
    rows = []
    for k, (f, s) in enumerate(jobs):
        tel, truth = simulate(f, seed=s)
        onset_idx = truth["onset"] // DT
        nv, first = limit_check(tel)
        full = run_agent(tel, truth["phase"], memory)
        causal = run_agent(tel, truth["phase"], memory, use_memory=False)
        mem = run_agent(tel, truth["phase"], memory, use_causal=False)
        det = min((a["onset"] for a in full["anoms"].values()), default=None)
        rows.append({
            "truth": f, "naive": nv, "causal": causal["diagnosis"], "memory": mem["diagnosis"], "agent": full["diagnosis"],
            "naive_delay_s": (first[1] - onset_idx) * DT if first else None,
            "agent_delay_s": (det - onset_idx) * DT if det is not None else None,
            "latency_ms": full["latency_ms"], "conf": full["confidence"],
        })
        if full["sig"]:
            memory.append((f, full["sig"]))  # operator confirms root cause -> long-term memory
    return rows


def summarise(rows):
    n = len(rows)
    acc = lambda key, rs=rows: round(100 * sum(r[key] == r["truth"] for r in rs) / len(rs), 1)
    nd = [r["naive_delay_s"] for r in rows if r["naive_delay_s"] is not None]
    ad = [r["agent_delay_s"] for r in rows if r["agent_delay_s"] is not None]
    med = lambda xs: sorted(xs)[len(xs) // 2] if xs else None
    curve = []
    for b in range(0, n, 50):
        blk = rows[b:b + 50]
        curve.append({"passes": f"{b+1}-{b+len(blk)}", "agent": acc("agent", blk), "naive": acc("naive", blk),
                      "causal_only": acc("causal", blk)})
    per_fault = {}
    for f in FAULTS:
        rs = [r for r in rows if r["truth"] == f]
        per_fault[f] = {"naive": acc("naive", rs), "agent": acc("agent", rs)}
    return {
        "n_passes": n,
        "root_cause_accuracy_pct": {"naive_limit_check": acc("naive"), "agent_causal_only": acc("causal"),
                                    "agent_memory_only": acc("memory"), "agent_full": acc("agent")},
        "alarm_missed_pct_naive": round(100 * sum(r["naive_delay_s"] is None for r in rows) / n, 1),
        "median_detection_delay_s": {"naive_limit_check": med(nd), "agent": med(ad)},
        "agent_latency_ms_median": round(med([r["latency_ms"] for r in rows]), 1),
        "learning_curve": curve,
        "per_fault": per_fault,
    }


if __name__ == "__main__":
    t = time.time()
    s = summarise(run(int(sys.argv[1]) if len(sys.argv) > 1 else 50))
    s["wall_s"] = round(time.time() - t, 1)
    print(json.dumps(s, indent=2))
