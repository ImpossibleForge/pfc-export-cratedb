#!/usr/bin/env python3
"""
pfc-export-cratedb — Comprehensive Test Suite
==============================================
Happy-Path + Error/Resilience Tests
Run on server: python3 test_pfc_export_cratedb.py

Author: ForgeBuddy + Dante | 2026-04-23
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

# ── Config ──────────────────────────────────────────────────────────────────
SCRIPT    = Path(__file__).parent / "pfc_export_cratedb.py"
PFC_BIN   = "/usr/local/bin/pfc_jsonl"
CRATE_HOST = "localhost"
CRATE_PORT = 5433          # Docker mapped port
CRATE_USER = "crate"
CRATE_PASS = ""
CRATE_DB   = "doc"
CRATE_SCH  = "doc"
TEST_TABLE = "pfc_export_test"
OUTDIR     = Path("/root/pfc_export_test_output")
OUTDIR.mkdir(exist_ok=True)

# ── Result tracking ──────────────────────────────────────────────────────────
results = []

def run(name, cmd, expect_exit=0, expect_in_stdout=None, expect_in_stderr=None):
    """Run a test command, record PASS/FAIL."""
    t0  = time.time()
    res = subprocess.run(cmd, capture_output=True, text=True)
    dt  = time.time() - t0

    ok = True
    reasons = []

    if res.returncode != expect_exit:
        ok = False
        reasons.append(f"exit={res.returncode} expected={expect_exit}")

    for s in (expect_in_stdout or []):
        if s not in (res.stdout + res.stderr):
            ok = False
            reasons.append(f"missing: {s!r}")

    status = "✅ PASS" if ok else "❌ FAIL"
    print(f"  {status}  [{dt:.1f}s]  {name}")
    if not ok:
        for r in reasons:
            print(f"           → {r}")
        if res.stdout.strip():
            print(f"           stdout: {res.stdout.strip()[:200]}")
        if res.stderr.strip():
            print(f"           stderr: {res.stderr.strip()[:200]}")

    results.append((name, ok, dt))
    return ok, res


def py(args, name=None, **kwargs):
    """Shorthand: run pfc_export_cratedb.py with given args."""
    label = name or " ".join(str(a) for a in args[:3])
    return run(label, ["python3", str(SCRIPT)] + [str(a) for a in args], **kwargs)


# ── DB helpers ───────────────────────────────────────────────────────────────
def db_connect():
    import psycopg2
    return psycopg2.connect(
        host=CRATE_HOST, port=CRATE_PORT,
        user=CRATE_USER, password=CRATE_PASS,
        dbname=CRATE_DB, connect_timeout=10,
    )


def setup_test_data(rows=50_000):
    """Create test table and insert rows."""
    import psycopg2
    conn = db_connect()
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute(f'DROP TABLE IF EXISTS "{CRATE_SCH}"."{TEST_TABLE}"')
    time.sleep(1)  # CrateDB needs a moment after DROP before CREATE
    cur.execute(f"""
        CREATE TABLE "{CRATE_SCH}"."{TEST_TABLE}" (
            "timestamp"  TIMESTAMP WITH TIME ZONE,
            "level"      TEXT,
            "service"    TEXT,
            "message"    TEXT,
            "request_id" TEXT,
            "latency_ms" INTEGER,
            "user_id"    INTEGER
        )
    """)
    time.sleep(2)  # CrateDB shard init before first INSERT

    levels   = ["INFO", "WARN", "ERROR", "DEBUG"]
    services = ["api-gateway", "auth", "payment", "notification", "search"]
    base_ts  = datetime(2026, 1, 1, tzinfo=timezone.utc)

    batch = []
    for i in range(rows):
        ts = base_ts + timedelta(seconds=i * 2)
        batch.append((
            ts.isoformat(),
            levels[i % len(levels)],
            services[i % len(services)],
            f"Request processed successfully for user {i % 1000}",
            f"req-{i:08d}",
            10 + (i % 490),
            i % 10000,
        ))
        if len(batch) == 5000:
            cur.executemany(
                f'INSERT INTO "{CRATE_SCH}"."{TEST_TABLE}" VALUES (%s,%s,%s,%s,%s,%s,%s)',
                batch
            )
            batch.clear()

    if batch:
        cur.executemany(
            f'INSERT INTO "{CRATE_SCH}"."{TEST_TABLE}" VALUES (%s,%s,%s,%s,%s,%s,%s)',
            batch
        )

    time.sleep(2)  # CrateDB needs a moment to refresh shards
    cur.execute(f'REFRESH TABLE "{CRATE_SCH}"."{TEST_TABLE}"')

    cur.execute(f'SELECT COUNT(*) FROM "{CRATE_SCH}"."{TEST_TABLE}"')
    count = cur.fetchone()[0]
    conn.close()
    return count


def count_rows_in_pfc(pfc_path):
    """Decompress .pfc and count JSONL lines."""
    res = subprocess.run(
        [PFC_BIN, "decompress", str(pfc_path), "-"],
        capture_output=True
    )
    if res.returncode != 0:
        return -1
    return sum(1 for line in res.stdout.split(b"\n") if line.strip().startswith(b"{"))


# ══════════════════════════════════════════════════════════════════════════════
print("=" * 65)
print("pfc-export-cratedb v0.1.0 — Test Suite")
print("=" * 65)

# ── Setup ────────────────────────────────────────────────────────────────────
print("\n[SETUP] Creating test table with 50,000 rows ...")
try:
    inserted = setup_test_data(50_000)
    print(f"        ✅ {inserted:,} rows inserted")
except Exception as e:
    print(f"        ❌ Setup failed: {e}")
    sys.exit(1)

# ══════════════════════════════════════════════════════════════════════════════
print("\n[1] HAPPY PATH — Full export")
# ── T01: Full table export ────────────────────────────────────────────────────
out01 = OUTDIR / "T01_full.pfc"
ok, _ = py(["--host", CRATE_HOST, "--port", CRATE_PORT, "--table", TEST_TABLE,
            "--output", out01, "--verbose"],
           name="T01: Full table export (50k rows)",
           expect_in_stdout=["50,000"])

if ok:
    rows = count_rows_in_pfc(out01)
    status = "✅" if rows == 50_000 else "❌"
    print(f"           {status} Roundtrip row count: {rows:,} / 50,000")
    results.append(("T01b: Roundtrip row count", rows == 50_000, 0))

# ── T02: Time-range export ────────────────────────────────────────────────────
out02 = OUTDIR / "T02_range.pfc"
py(["--host", CRATE_HOST, "--port", CRATE_PORT, "--table", TEST_TABLE,
    "--ts-column", "timestamp",
    "--from-ts", "2026-01-01T00:00:00",
    "--to-ts",   "2026-01-08T00:00:00",
    "--output", out02, "--verbose"],
   name="T02: Time-range export (Jan 1–7)",
   expect_in_stdout=["rows"])

# ── T03: Auto output filename ─────────────────────────────────────────────────
orig_dir = Path.cwd()
os.chdir(OUTDIR)
py(["--host", CRATE_HOST, "--port", CRATE_PORT, "--table", TEST_TABLE,
    "--ts-column", "timestamp",
    "--from-ts", "2026-02-01T00:00:00",
    "--to-ts",   "2026-02-02T00:00:00"],
   name="T03: Auto output filename (no --output)",
   expect_in_stdout=["rows"])
os.chdir(orig_dir)

# ── T04: Schema explicit ──────────────────────────────────────────────────────
out04 = OUTDIR / "T04_schema.pfc"
py(["--host", CRATE_HOST, "--port", CRATE_PORT, "--table", TEST_TABLE,
    "--schema", "doc", "--output", out04],
   name="T04: Explicit --schema doc",
   expect_in_stdout=["rows"])

# ── T05: Batch-size small ─────────────────────────────────────────────────────
out05 = OUTDIR / "T05_batch.pfc"
py(["--host", CRATE_HOST, "--port", CRATE_PORT, "--table", TEST_TABLE,
    "--batch-size", "1000", "--output", out05, "--verbose"],
   name="T05: Small batch-size (1000)",
   expect_in_stdout=["50,000"])

# ── T06: Stress — large export ────────────────────────────────────────────────
print("\n[2] STRESS TEST — Large export")
out06 = OUTDIR / "T06_stress.pfc"
t0 = time.time()
ok, res = py(["--host", CRATE_HOST, "--port", CRATE_PORT, "--table", TEST_TABLE,
              "--output", out06],
             name="T06: Stress export 50k rows (timing)")
if ok and out06.exists():
    mb    = out06.stat().st_size / 1_048_576
    speed = 50_000 / (time.time() - t0)
    print(f"           → Output: {mb:.1f} MiB  |  Speed: {speed:.0f} rows/s")

# ══════════════════════════════════════════════════════════════════════════════
print("\n[3] ERROR HANDLING — Enterprise-ready failures")

# ── T07: Wrong host ───────────────────────────────────────────────────────────
py(["--host", "nonexistent.host.invalid", "--port", "5433",
    "--table", TEST_TABLE, "--output", OUTDIR / "T07.pfc"],
   name="T07: Wrong host → clear error",
   expect_exit=1,
   expect_in_stdout=["ERROR"])

# ── T08: Wrong port ───────────────────────────────────────────────────────────
py(["--host", CRATE_HOST, "--port", "9999",
    "--table", TEST_TABLE, "--output", OUTDIR / "T08.pfc"],
   name="T08: Wrong port → clear error",
   expect_exit=1,
   expect_in_stdout=["ERROR"])

# ── T09: Wrong credentials ────────────────────────────────────────────────────
py(["--host", CRATE_HOST, "--port", CRATE_PORT,
    "--user", "wronguser", "--password", "wrongpass",
    "--table", TEST_TABLE, "--output", OUTDIR / "T09.pfc"],
   name="T09: Wrong credentials → clear error",
   expect_exit=1,
   expect_in_stdout=["ERROR"])

# ── T10: Table not found ──────────────────────────────────────────────────────
py(["--host", CRATE_HOST, "--port", CRATE_PORT,
    "--table", "nonexistent_table_xyz", "--output", OUTDIR / "T10.pfc"],
   name="T10: Table not found → clear error",
   expect_exit=1,
   expect_in_stdout=["ERROR"])

# ── T11: pfc_jsonl binary not found ──────────────────────────────────────────
py(["--host", CRATE_HOST, "--port", CRATE_PORT, "--table", TEST_TABLE,
    "--pfc-binary", "/nonexistent/pfc_jsonl", "--output", OUTDIR / "T11.pfc"],
   name="T11: pfc_jsonl binary missing → clear error",
   expect_exit=1,
   expect_in_stdout=["ERROR"])

# ── T12: Empty result set (valid time range, no data) ─────────────────────────
py(["--host", CRATE_HOST, "--port", CRATE_PORT, "--table", TEST_TABLE,
    "--ts-column", "timestamp",
    "--from-ts", "2099-01-01T00:00:00",
    "--to-ts",   "2099-01-02T00:00:00",
    "--output", OUTDIR / "T12.pfc"],
   name="T12: Empty result set (future date) → 0 rows gracefully",
   expect_in_stdout=["0 rows"],
   expect_exit=0)

# ── T13: CrateDB container stopped mid-test ──────────────────────────────────
print("\n[4] RESILIENCE — CrateDB down")
subprocess.run(["docker", "stop", "crate-test"], capture_output=True)
time.sleep(2)

py(["--host", CRATE_HOST, "--port", CRATE_PORT,
    "--table", TEST_TABLE, "--output", OUTDIR / "T13.pfc"],
   name="T13: CrateDB stopped → clean error, no crash",
   expect_exit=1,
   expect_in_stdout=["ERROR"])

# Restart CrateDB
subprocess.run(["docker", "start", "crate-test"], capture_output=True)
time.sleep(5)
print("           (CrateDB restarted)")

# ── T14: Version flag ─────────────────────────────────────────────────────────
run("T14: --version flag",
    ["python3", str(SCRIPT), "--version"],
    expect_in_stdout=["0.1.0"])

# ── T15: --help flag ──────────────────────────────────────────────────────────
run("T15: --help flag",
    ["python3", str(SCRIPT), "--help"],
    expect_in_stdout=["--host", "--table", "--ts-column"])

# ══════════════════════════════════════════════════════════════════════════════
print("\n[5] OUTPUT FILE VERIFICATION")
existing = [(p.name, p.stat().st_size / 1_048_576)
            for p in OUTDIR.glob("*.pfc") if p.stat().st_size > 0]
for name, mb in sorted(existing):
    print(f"  📄 {name:<30} {mb:.2f} MiB")

# ══════════════════════════════════════════════════════════════════════════════
passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)
total  = len(results)
total_time = sum(dt for _, _, dt in results)

print(f"\n{'='*65}")
print(f"RESULTS: {passed}/{total} PASS  |  {failed} FAIL  |  {total_time:.1f}s total")
print(f"{'='*65}")

if failed > 0:
    print("\nFailed tests:")
    for name, ok, _ in results:
        if not ok:
            print(f"  ❌ {name}")

sys.exit(0 if failed == 0 else 1)
