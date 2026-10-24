"""FDIR agent: detect -> isolate (causal graph) -> recall (incident memory) -> fuse -> recommend.

Also contains the naive baseline that mimics a classic limit-check ops console.
"""
import math
import os
import time

from .sim import CHANNELS, PARENTS, EDGES, FAULTS, TOPO, N, eclipse_mask

BASE_N = 150      # samples assumed nominal at start of pass
Z = 5.0
PERSIST = 3


# ---------------- naive baseline ----------------
def limit_check(tel):
    """What most small-sat consoles do: first channel out of hard limits -> blame its subsystem."""
    first = None
    for c, (_, _, lo, hi, _, _) in CHANNELS.items():
        for i, v in enumerate(tel[c]):
            if v < lo or v > hi:
                if first is None or i < first[1]:
                    first = (c, i)
                break
    if not first:
        return None, None
    c = first[0]
    for f, (root, _, _) in FAULTS.items():
        if root == c:
            return f, first
    sub = CHANNELS[c][5]
    for f, (root, _, _) in FAULTS.items():
        if CHANNELS[root][5] == sub:
            return f, first
    return None, first


# ---------------- agent tools ----------------
def detect(tel, phase):
    """Orbit-aware residuals: baseline conditioned on eclipse/sunlit from ephemeris."""
    ecl = eclipse_mask(phase)
    out = {}
    for c in CHANNELS:
        x = tel[c]
        stats = {}
        for flag in (True, False):
            xs = [x[i] for i in range(BASE_N) if ecl[i] == flag] or [x[i] for i in range(BASE_N)]
            mu = sum(xs) / len(xs)
            sd = math.sqrt(sum((v - mu) ** 2 for v in xs) / max(1, len(xs) - 1))
            stats[flag] = (mu, max(sd, 1e-3, 0.02 * abs(mu) if c == "st_valid" else 1e-3))
        run, onset, sign, peak = 0, None, 0, 0.0
        for i in range(BASE_N, N):
            mu, sd = stats[ecl[i]]
            z = (x[i] - mu) / sd
            if c == "st_valid":
                z = -10.0 if x[i] < 0.5 else 0.0
            if abs(z) > Z:
                run += 1
                if run >= PERSIST and onset is None:
                    onset, sign = i - PERSIST + 1, (1 if z > 0 else -1)
            else:
                run = 0
            peak = max(peak, abs(z))
        if onset is not None:
            out[c] = {"onset": onset, "sign": sign, "peak_z": round(min(peak, 99), 1)}
    return out


def signature(anoms):
    order = sorted(anoms, key=lambda c: anoms[c]["onset"])
    rank = {c: r for r, c in enumerate(order)}
    return [anoms[c]["sign"] / (1 + rank[c]) if c in anoms else 0.0 for c in TOPO]


def isolate(anoms):
    """Walk the causal graph: score each fault by root presence, sign, precedence and downstream coverage."""
    scores = {}
    if not anoms:
        return scores
    first_onset = min(a["onset"] for a in anoms.values())
    for f, (root, step, _) in FAULTS.items():
        s = 0.0
        if root in anoms:
            a = anoms[root]
            s += 2.0 if a["sign"] == (1 if step > 0 else -1) else 0.5
            s += 1.5 if a["onset"] <= first_onset + 6 else 0.0
            if not any(p in anoms and anoms[p]["onset"] < a["onset"] for p in PARENTS.get(root, [])):
                s += 0.5
        kids = _descendants(root)
        if kids:
            s += 1.5 * sum(1 for k in kids if k in anoms) / len(kids)
        # anomalies NOT explained by this root are evidence against it
        unexplained = [c for c in anoms if c != root and c not in kids]
        s -= 0.6 * len(unexplained)
        scores[f] = round(s, 3)
    return scores


def _descendants(c, seen=None):
    seen = seen or set()
    for k, _, _ in EDGES.get(c, []):
        if k not in seen:
            seen.add(k)
            _descendants(k, seen)
    return seen


def recall(sig, memory, k=5):
    if not memory:
        return {}, []
    def cos(a, b):
        na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
        return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0
    sims = sorted(((cos(sig, s), f) for f, s in memory), reverse=True)[:k]
    votes = {}
    for sim, f in sims:
        votes[f] = votes.get(f, 0) + max(sim, 0)
    return votes, [(f, round(s, 3)) for s, f in sims]


def run_agent(tel, phase, memory, use_memory=True, use_causal=True):
    t0 = time.perf_counter()
    trace = []
    anoms = detect(tel, phase)
    trace.append({"step": "detect", "tool": "telemetry.query + orbit-aware residuals",
                  "result": f"{len(anoms)} anomalous channels", "detail": anoms})
    if not anoms:
        return {"diagnosis": "NOMINAL", "confidence": 0.9, "trace": trace, "anoms": anoms, "sig": None,
                "latency_ms": (time.perf_counter() - t0) * 1000}
    sig = signature(anoms)
    causal = isolate(anoms) if use_causal else {}
    trace.append({"step": "isolate", "tool": "causal_graph.backtrack", "result": _top(causal), "detail": causal})
    votes, neighbours = recall(sig, memory) if use_memory else ({}, [])
    trace.append({"step": "recall", "tool": "incident_memory.knn", "result": f"{len(memory) if use_memory else 0} incidents searched; top: {neighbours[:3]}",
                  "detail": votes})
    fused = {}
    for f in FAULTS:
        c = causal.get(f, 0.0)
        m = votes.get(f, 0.0)
        fused[f] = (c / 5.5 if use_causal else 0) + (0.9 * m / 5 if use_memory else 0)
    ranked = sorted(fused.items(), key=lambda kv: -kv[1])
    best, second = ranked[0], ranked[1]
    conf = 1 / (1 + math.exp(-6 * (best[1] - second[1])))
    trace.append({"step": "fuse", "tool": "evidence_fusion", "result": f"{best[0]} (margin {best[1]-second[1]:.2f})",
                  "detail": {k: round(v, 3) for k, v in ranked}})
    trace.append({"step": "recommend", "tool": "procedures.lookup", "result": FAULTS[best[0]][2]})
    return {"diagnosis": best[0], "confidence": round(conf, 3), "trace": trace, "anoms": anoms, "sig": sig,
            "ranked": ranked[:3], "latency_ms": (time.perf_counter() - t0) * 1000}


def _top(d):
    if not d:
        return "n/a"
    f = max(d, key=d.get)
    return f"{f} ({d[f]})"


def briefing(result, truth_phase=None):
    """Operator briefing. Uses an LLM if ANTHROPIC_API_KEY is set, otherwise a deterministic template."""
    d = result["diagnosis"]
    anoms = result["anoms"]
    chain = " -> ".join(sorted(anoms, key=lambda c: anoms[c]["onset"]))
    text = (f"Most likely root cause: {d} (confidence {result['confidence']:.0%}). "
            f"Symptom chain by onset: {chain}. "
            f"Recommended recovery: {FAULTS[d][2] if d in FAULTS else 'none'}")
    if os.environ.get("ANTHROPIC_API_KEY") and d in FAULTS:
        try:
            import anthropic
            msg = anthropic.Anthropic().messages.create(
                model=os.environ.get("FDIR_MODEL", "claude-sonnet-5-5"), max_tokens=300,
                messages=[{"role": "user", "content":
                           "You are a spacecraft FDIR flight controller. Write a 4-line operator briefing "
                           "(root cause, evidence, immediate action, what to watch) from this agent trace. "
                           f"Do not invent data.\n{result['trace']}"}])
            text = msg.content[0].text
        except Exception as e:  # never block ops on the LLM
            text += f"  [LLM briefing unavailable: {type(e).__name__}]"
    return text
