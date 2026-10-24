"""FDIR Copilot web app (FastAPI). Run: uvicorn fdir.app:app --port 8080"""
import json
import os
import random
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from .db import DB
from .sim import simulate, FAULTS, CHANNELS, eclipse_mask
from .agent import run_agent, limit_check, briefing
from . import benchmark

app = FastAPI(title="FDIR Copilot")
db = DB()
HERE = os.path.dirname(__file__)


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/api/status")
def status():
    n_inc = db.all("SELECT COUNT(*) FROM incidents")[0][0]
    n_pass = db.all("SELECT COUNT(*) FROM passes")[0][0]
    n_tel = db.all("SELECT COUNT(*) FROM telemetry")[0][0]
    return {"backend": db.backend, "incidents_in_memory": n_inc, "passes": n_pass, "telemetry_rows": n_tel,
            "llm": bool(os.environ.get("ANTHROPIC_API_KEY"))}


@app.post("/api/pass")
def new_pass(fault: str | None = None):
    """A new downlinked telemetry pass arrives (with a hidden injected fault)."""
    fault = fault or random.choice(list(FAULTS))
    if fault not in FAULTS:
        raise HTTPException(400, "unknown fault")
    pid = uuid.uuid4().hex[:8]
    tel, truth = simulate(fault, seed=random.randrange(10**9))
    db.save_pass(pid, tel, truth)
    return {"pass_id": pid}


@app.get("/api/pass/{pid}")
def get_pass(pid: str):
    tel, meta = db.load_pass(pid)
    if tel is None:
        raise HTTPException(404)
    return {"meta": {"id": meta["id"], "status": meta["status"]}, "telemetry": tel,
            "eclipse": eclipse_mask(meta["phase"]),
            "channels": {c: {"unit": v[4], "subsystem": v[5], "lo": v[2], "hi": v[3]} for c, v in CHANNELS.items()}}


@app.post("/api/pass/{pid}/diagnose")
def diagnose(pid: str):
    tel, meta = db.load_pass(pid)
    if tel is None:
        raise HTTPException(404)
    memory = db.incidents()
    res = run_agent(tel, meta["phase"], memory)
    naive, first = limit_check(tel)
    res["briefing"] = briefing(res)
    db.exec("INSERT INTO agent_runs(id, pass_id, diagnosis, confidence, latency_ms, trace) VALUES (?,?,?,?,?,?)",
            (uuid.uuid4().hex[:8], pid, res["diagnosis"], res["confidence"], res["latency_ms"], json.dumps(res["trace"])))
    return {"agent": {k: res[k] for k in ("diagnosis", "confidence", "trace", "briefing", "latency_ms") if k in res},
            "naive": {"diagnosis": naive, "first_alarm": first},
            "memory_size": len(memory)}


@app.post("/api/pass/{pid}/confirm")
def confirm(pid: str):
    """Operator closes the incident: ground truth (from the anomaly report) is written to long-term memory."""
    tel, meta = db.load_pass(pid)
    if tel is None:
        raise HTTPException(404)
    res = run_agent(tel, meta["phase"], [], use_memory=False)
    if res["sig"]:
        db.add_incident(uuid.uuid4().hex[:8], pid, meta["fault"], res["sig"], FAULTS[meta["fault"]][2], "operator")
    db.exec("UPDATE passes SET status='closed' WHERE id=?", (pid,))
    return {"true_root_cause": meta["fault"], "learned": bool(res["sig"])}


@app.post("/api/memory/seed")
def seed_memory(n: int = 10):
    """Load historical anomaly reports (n per fault class) into incident memory."""
    added = 0
    for f in FAULTS:
        for _ in range(n):
            tel, truth = simulate(f, seed=random.randrange(10**9))
            r = run_agent(tel, truth["phase"], [], use_memory=False)
            if r["sig"]:
                db.add_incident(uuid.uuid4().hex[:8], None, f, r["sig"], FAULTS[f][2], "history")
                added += 1
    return {"added": added}


@app.post("/api/memory/reset")
def reset_memory():
    db.clear_incidents()
    return {"ok": True}


@app.get("/api/benchmark")
def get_benchmark():
    rows = db.all("SELECT result FROM benchmarks ORDER BY created_at DESC LIMIT 1")
    if rows:
        return json.loads(rows[0][0])
    path = os.path.join(HERE, "..", "bench_result.json")
    if os.path.exists(path):
        return json.load(open(path))
    return {}


@app.post("/api/benchmark")
def run_benchmark(n_per_fault: int = 20):
    s = benchmark.summarise(benchmark.run(n_per_fault, seed=random.randrange(10**6)))
    db.exec("INSERT INTO benchmarks(id, result) VALUES (?,?)", (uuid.uuid4().hex[:8], json.dumps(s)))
    return s
