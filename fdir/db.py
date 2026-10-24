"""Data layer: Aiven PostgreSQL when DATABASE_URL is set, SQLite locally otherwise."""
import json
import os
import sqlite3
import threading

URL = os.environ.get("DATABASE_URL")
_lock = threading.Lock()

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS passes (
        id TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        spacecraft TEXT, phase REAL, truth_fault TEXT, truth_onset INTEGER, status TEXT)""",
    """CREATE TABLE IF NOT EXISTS telemetry (
        pass_id TEXT, idx INTEGER, channel TEXT, value REAL)""",
    "CREATE INDEX IF NOT EXISTS ix_tel ON telemetry(pass_id, channel)",
    """CREATE TABLE IF NOT EXISTS incidents (
        id TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        pass_id TEXT, fault TEXT, signature TEXT, resolution TEXT, source TEXT)""",
    """CREATE TABLE IF NOT EXISTS agent_runs (
        id TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        pass_id TEXT, diagnosis TEXT, confidence REAL, latency_ms REAL, trace TEXT)""",
    """CREATE TABLE IF NOT EXISTS benchmarks (
        id TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, result TEXT)""",
]


class DB:
    def __init__(self):
        if URL:
            import psycopg
            self.pg = True
            self.conn = psycopg.connect(URL, autocommit=True)
        else:
            self.pg = False
            path = os.environ.get("SQLITE_PATH", os.path.join(os.path.dirname(__file__), "..", "fdir_local.db"))
            self.conn = sqlite3.connect(path, check_same_thread=False)
        for s in SCHEMA:
            self.exec(s)

    @property
    def backend(self):
        return "Aiven PostgreSQL" if self.pg else "SQLite (local)"

    def _q(self, sql):
        return sql.replace("?", "%s") if self.pg else sql

    def exec(self, sql, params=()):
        with _lock:
            cur = self.conn.cursor()
            cur.execute(self._q(sql), params)
            if not self.pg:
                self.conn.commit()

    def many(self, sql, rows):
        with _lock:
            cur = self.conn.cursor()
            cur.executemany(self._q(sql), rows)
            if not self.pg:
                self.conn.commit()

    def all(self, sql, params=()):
        with _lock:
            cur = self.conn.cursor()
            cur.execute(self._q(sql), params)
            return cur.fetchall()

    # --- domain helpers ---
    def save_pass(self, pid, tel, truth, spacecraft="DEMO-SAT-1"):
        self.exec("INSERT INTO passes(id, spacecraft, phase, truth_fault, truth_onset, status) VALUES (?,?,?,?,?,?)",
                  (pid, spacecraft, truth["phase"], truth["fault"], truth["onset"], "new"))
        rows = [(pid, i, c, v) for c, vals in tel.items() for i, v in enumerate(vals)]
        self.many("INSERT INTO telemetry(pass_id, idx, channel, value) VALUES (?,?,?,?)", rows)

    def load_pass(self, pid):
        meta = self.all("SELECT id, phase, truth_fault, truth_onset, status FROM passes WHERE id=?", (pid,))
        if not meta:
            return None, None
        tel = {}
        for c, i, v in self.all("SELECT channel, idx, value FROM telemetry WHERE pass_id=? ORDER BY idx", (pid,)):
            tel.setdefault(c, []).append(v)
        m = meta[0]
        return tel, {"id": m[0], "phase": m[1], "fault": m[2], "onset": m[3], "status": m[4]}

    def incidents(self):
        return [(f, json.loads(s)) for f, s in self.all("SELECT fault, signature FROM incidents")]

    def add_incident(self, iid, pid, fault, signature, resolution, source):
        self.exec("INSERT INTO incidents(id, pass_id, fault, signature, resolution, source) VALUES (?,?,?,?,?,?)",
                  (iid, pid, fault, json.dumps(signature), resolution, source))

    def clear_incidents(self):
        self.exec("DELETE FROM incidents")
