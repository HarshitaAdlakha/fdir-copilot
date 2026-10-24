# FDIR Copilot: submission texts (copy and paste)

## Project name
FDIR Copilot

## One-line pitch
An AI flight controller for small-satellite teams: it reads every telemetry pass, traces symptoms back through a causal model of the spacecraft to the real root cause, and learns from every closed anomaly report. In our benchmark it gets 93% of root causes right; a classic limit-check console gets 32%.

## The data layer in action
**What data the agent uses**
1. **Telemetry passes (time-series).** 13 channels across 4 subsystems (EPS, thermal, ADCS, comms), sampled every 10 s. That is 720 samples per pass, or 9,360 rows. Every downlinked pass is written to PostgreSQL (`passes` and `telemetry` tables) and queried back by the agent.
2. **Orbit ephemeris context.** Eclipse and sunlit state for each sample. The agent uses it to build orbit-aware baselines, so "solar current = 0 A" in eclipse is not flagged as a fault.
3. **Spacecraft causal model.** A system-dynamics graph (for example solar array → bus voltage → state of charge → battery voltage → battery temperature), with gains and transport delays. It lets the agent tell causes from symptoms.
4. **Long-term incident memory.** The `incidents` table in PostgreSQL. Every closed anomaly report stores the fault signature (signed onset-order vector across channels), the confirmed root cause and the recovery procedure. The agent runs k-nearest-neighbour recall over it on every new pass.
5. **Run log and benchmarks.** The `agent_runs` table holds every diagnosis with its full reasoning trace, confidence and latency, for audit. The `benchmarks` table holds evaluation results.

**How it flows**
Downlink → PostgreSQL (`telemetry`) → agent step 1 **detect** (orbit-aware residuals, persistence filter) → step 2 **isolate** (backtrack the causal graph: root presence, sign, precedence, downstream coverage, unexplained anomalies) → step 3 **recall** (kNN over incident memory in PostgreSQL) → step 4 **fuse** the evidence → step 5 **recommend** a recovery procedure, plus an operator briefing (LLM, when a key is set) → `agent_runs` → the operator closes the incident → the confirmed root cause is written back to `incidents`. The next pass is diagnosed with more memory, so the system improves with every run.

Aiven PostgreSQL is the system of record (connected with `DATABASE_URL` through an Aiven Runtime service integration). The app is containerised (Dockerfile) and deployed on Aiven Runtime.

## Evidence it works
Benchmark: 300 fault-injected telemetry passes (6 fault classes × 50, random onset, severity and orbit phase). Passes arrive online, and memory starts **empty** and grows only from closed incidents.

| Method | Root-cause accuracy |
|---|---|
| Classic limit-check console (naive baseline, no data context) | **32.3%** |
| Agent: causal model only (no memory) | 75.3% |
| Agent: memory only | 86.7% |
| **Agent: causal + memory (full)** | **93.0%** |

- **Missed alarms:** the limit-check console never alarms on 49.7% of faults, because the channels stay inside their limits. The agent flags all of them.
- **Detection delay (median):** agent 70 s after fault onset, against 450 s for the console.
- **Learning over time:** agent accuracy goes 76% → 86% → 98% → 98% → 100% → 100% across blocks of 50 passes as memory fills. The console stays flat at about 30%.
- **Cost/latency:** a diagnosis takes a median of 4 ms of compute (the LLM is used only for the human briefing).
- Reproduce with `python -m fdir.benchmark 50`, or click "Re-run benchmark" in the app.

## One pitch slide
(See `docs/images/pitch_slide.png`: put it into Google Slides and set sharing to "Anyone with the link can view".)

## 3-minute demo script
1. **0:00–0:25, problem.** "A 3-person NewSpace ops team flies 10+ satellites. Their console alarms on symptoms, like battery temperature, not causes, like a degrading solar array. Half of real faults never cross a limit at all."
2. **0:25–1:20, live run.** Click *Downlink new pass*. Point at the eclipse shading and the red limit lines: nothing is red. Click *Run FDIR agent* and walk through the 5 steps: detect, isolate (causal graph), recall (memory in Aiven Postgres), fuse, recommend. Show the console verdict next to the agent verdict.
3. **1:20–1:50, memory.** Click *Close incident → memory*. The incident count goes up. Run 1–2 more passes.
4. **1:50–2:30, evidence.** Show the KPI tiles and the learning curve: 32% → 93%, 50% of faults silent on the console, 70 s against 450 s.
5. **2:30–3:00, data layer and go-to-market.** Aiven PostgreSQL tables, Aiven Runtime deployment. First users: university CubeSat teams and seed-stage NewSpace operators. Integration with OpenMCT/YAMCS telemetry exports.
