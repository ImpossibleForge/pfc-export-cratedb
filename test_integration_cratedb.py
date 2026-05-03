#!/usr/bin/env python3
"""
Integration tests for pfc-export-cratedb — requires live CrateDB.

Run on the server:
  python3 test_integration_cratedb.py

CrateDB: localhost:5433 (Docker container crate-test)
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

CRATE_HOST = "localhost"
CRATE_PORT = 5433
CRATE_USER = "crate"
CRATE_PASS = ""
CRATE_DB   = "doc"
PFC_BINARY = "/usr/local/bin/pfc_jsonl"

try:
    import psycopg2
    import psycopg2.extras
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False


def get_conn():
    return psycopg2.connect(
        host=CRATE_HOST, port=CRATE_PORT,
        user=CRATE_USER, password=CRATE_PASS,
        dbname=CRATE_DB, connect_timeout=10,
    )


def pfc_binary_available():
    return os.path.isfile(PFC_BINARY) and os.access(PFC_BINARY, os.X_OK)


@unittest.skipUnless(HAS_PSYCOPG2, "psycopg2 not installed")
class TestCrateDBIntegration(unittest.TestCase):

    TABLE = "pfc_export_integration_test"

    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()
        cls.conn.autocommit = True
        cur = cls.conn.cursor()
        cur.execute(f'DROP TABLE IF EXISTS "{cls.TABLE}"')
        cur.execute(f"""
            CREATE TABLE "{cls.TABLE}" (
                id         INTEGER,
                ts         TIMESTAMP WITH TIME ZONE,
                level      TEXT,
                message    TEXT,
                value      DOUBLE PRECISION
            )
        """)
        # Insert 200 rows spanning 10 days
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        rows = []
        for i in range(200):
            ts = base + timedelta(hours=i)
            level = ["INFO", "WARN", "ERROR"][i % 3]
            rows.append((i, ts.isoformat(), level, f"log message {i}", float(i) * 1.5))

        cur.executemany(
            f'INSERT INTO "{cls.TABLE}" (id, ts, level, message, value) VALUES (%s, %s, %s, %s, %s)',
            rows,
        )
        # CrateDB needs a refresh before SELECT sees new rows
        cur.execute(f'REFRESH TABLE "{cls.TABLE}"')
        cur.close()

    @classmethod
    def tearDownClass(cls):
        cur = cls.conn.cursor()
        cur.execute(f'DROP TABLE IF EXISTS "{cls.TABLE}"')
        cur.close()
        cls.conn.close()

    # ------------------------------------------------------------------
    # 1. Basic export — all rows
    # ------------------------------------------------------------------
    def test_basic_export_row_count(self):
        from pfc_export_cratedb import export_to_pfc
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "test_basic.pfc"
            result = export_to_pfc(
                host=CRATE_HOST, port=CRATE_PORT,
                user=CRATE_USER, password=CRATE_PASS,
                dbname=CRATE_DB, schema=None,
                table=self.TABLE, output_path=out,
                pfc_binary=PFC_BINARY,
            )
            self.assertEqual(result["rows"], 200, f"Expected 200 rows, got {result['rows']}")
            self.assertTrue(out.exists(), ".pfc file not created")

    # ------------------------------------------------------------------
    # 2. .bidx file is created alongside .pfc
    # ------------------------------------------------------------------
    @unittest.skipUnless(pfc_binary_available(), "pfc_jsonl binary not found")
    def test_bidx_created(self):
        from pfc_export_cratedb import export_to_pfc
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "test_bidx.pfc"
            export_to_pfc(
                host=CRATE_HOST, port=CRATE_PORT,
                user=CRATE_USER, password=CRATE_PASS,
                dbname=CRATE_DB, schema=None,
                table=self.TABLE, output_path=out,
                pfc_binary=PFC_BINARY, ts_column="ts",
            )
            bidx = Path(str(out) + ".bidx")
            self.assertTrue(bidx.exists(), ".pfc.bidx not created")

    # ------------------------------------------------------------------
    # 3. Time-range filter — only rows in range
    # ------------------------------------------------------------------
    def test_time_range_filter(self):
        from pfc_export_cratedb import export_to_pfc
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "test_range.pfc"
            # First 48 hours = rows 0..47 = 48 rows
            result = export_to_pfc(
                host=CRATE_HOST, port=CRATE_PORT,
                user=CRATE_USER, password=CRATE_PASS,
                dbname=CRATE_DB, schema=None,
                table=self.TABLE, output_path=out,
                pfc_binary=PFC_BINARY, ts_column="ts",
                from_ts="2024-01-01T00:00:00",
                to_ts="2024-01-03T00:00:00",
            )
            self.assertEqual(result["rows"], 48, f"Expected 48 rows, got {result['rows']}")

    # ------------------------------------------------------------------
    # 4. JSONL content — fields are preserved correctly
    # ------------------------------------------------------------------
    @unittest.skipUnless(pfc_binary_available(), "pfc_jsonl binary not found")
    def test_field_integrity(self):
        from pfc_export_cratedb import export_to_pfc
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "test_fields.pfc"
            export_to_pfc(
                host=CRATE_HOST, port=CRATE_PORT,
                user=CRATE_USER, password=CRATE_PASS,
                dbname=CRATE_DB, schema=None,
                table=self.TABLE, output_path=out,
                pfc_binary=PFC_BINARY,
                from_ts="2024-01-01T00:00:00",
                to_ts="2024-01-01T01:00:00",
                ts_column="ts",
            )
            # Decompress and check first record
            result = subprocess.run(
                [PFC_BINARY, "decompress", str(out), "-"],
                capture_output=True, text=True,
            )
            lines = [l for l in result.stdout.splitlines() if l.startswith("{")]
            self.assertGreater(len(lines), 0, "No JSON lines in decompress output")
            record = json.loads(lines[0])
            self.assertIn("id", record)
            self.assertIn("level", record)
            self.assertIn("message", record)
            self.assertIn("value", record)

    # ------------------------------------------------------------------
    # 5. .pfc file is non-empty and decompresses without error
    # ------------------------------------------------------------------
    @unittest.skipUnless(pfc_binary_available(), "pfc_jsonl binary not found")
    def test_pfc_decompresses_cleanly(self):
        from pfc_export_cratedb import export_to_pfc
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "test_decomp.pfc"
            result = export_to_pfc(
                host=CRATE_HOST, port=CRATE_PORT,
                user=CRATE_USER, password=CRATE_PASS,
                dbname=CRATE_DB, schema=None,
                table=self.TABLE, output_path=out,
                pfc_binary=PFC_BINARY,
            )
            self.assertGreater(os.path.getsize(out), 0, ".pfc file is empty")
            # Decompress must exit cleanly
            proc = subprocess.run(
                [PFC_BINARY, "decompress", str(out), "-"],
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 0, f"decompress failed: {proc.stderr.decode()[:200]}")

    # ------------------------------------------------------------------
    # 6. Empty result — no rows in range
    # ------------------------------------------------------------------
    def test_empty_range_no_crash(self):
        from pfc_export_cratedb import export_to_pfc
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "test_empty.pfc"
            result = export_to_pfc(
                host=CRATE_HOST, port=CRATE_PORT,
                user=CRATE_USER, password=CRATE_PASS,
                dbname=CRATE_DB, schema=None,
                table=self.TABLE, output_path=out,
                pfc_binary=PFC_BINARY, ts_column="ts",
                from_ts="2030-01-01T00:00:00",
                to_ts="2030-01-02T00:00:00",
            )
            self.assertEqual(result["rows"], 0)

    # ------------------------------------------------------------------
    # 7. Bad credentials — clean error
    # ------------------------------------------------------------------
    def test_bad_credentials_raises(self):
        from pfc_export_cratedb import export_to_pfc
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "test_bad.pfc"
            with self.assertRaises(Exception):
                export_to_pfc(
                    host=CRATE_HOST, port=CRATE_PORT,
                    user="wrong_user", password="wrong_pass",
                    dbname=CRATE_DB, schema=None,
                    table=self.TABLE, output_path=out,
                    pfc_binary=PFC_BINARY,
                )

    # ------------------------------------------------------------------
    # 8. Batch streaming — large batches work
    # ------------------------------------------------------------------
    def test_large_batch_size(self):
        from pfc_export_cratedb import export_to_pfc
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "test_batch.pfc"
            result = export_to_pfc(
                host=CRATE_HOST, port=CRATE_PORT,
                user=CRATE_USER, password=CRATE_PASS,
                dbname=CRATE_DB, schema=None,
                table=self.TABLE, output_path=out,
                pfc_binary=PFC_BINARY,
                batch_size=500,  # larger than row count — single fetch
            )
            self.assertEqual(result["rows"], 200)


if __name__ == "__main__":
    print(f"CrateDB Integration Tests — {CRATE_HOST}:{CRATE_PORT}")
    print(f"pfc_jsonl binary: {'found' if pfc_binary_available() else 'NOT FOUND — binary tests will skip'}")
    print("-" * 60)
    unittest.main(verbosity=2)
