# pfc-export-cratedb — Export CrateDB tables to PFC cold storage

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://python.org)
[![PFC-JSONL](https://img.shields.io/badge/PFC--JSONL-v3.4-green.svg)](https://github.com/ImpossibleForge/pfc-jsonl)
[![Version](https://img.shields.io/badge/pfc--export--cratedb-v0.1.0-brightgreen.svg)](https://github.com/ImpossibleForge/pfc-export-cratedb/releases)

Stream rows from a CrateDB table directly into a compressed `.pfc` archive with block-level timestamp index — ready for time-range queries via DuckDB or pfc-gateway without loading the full archive.

---

## The problem

CrateDB tables holding `application_logs`, `audit_trail`, or `change_stream_archive` grow continuously. Data older than 30 days is rarely queried but still sits in hot storage, inflating your cluster and slowing queries. Moving it out means losing the ability to query it — unless you use PFC.

---

## What PFC gives you

| | gzip archive | S3 + Athena | **PFC-JSONL** |
|---|---|---|---|
| Storage vs. raw JSONL | ~12% | ~12% | **~9%** |
| Query one hour from 30 days | Decompress all | $5/TB scan | **Decompress ~1/720** |
| Time-range index | ❌ | ❌ | ✅ built-in |
| Works offline / no cloud API | ✅ | ❌ | ✅ |

---

## Requirements

**The `pfc_jsonl` binary must be installed:**

```bash
# Linux x64:
curl -L https://github.com/ImpossibleForge/pfc-jsonl/releases/latest/download/pfc_jsonl-linux-x64 \
     -o /usr/local/bin/pfc_jsonl && chmod +x /usr/local/bin/pfc_jsonl

# macOS (Apple Silicon):
curl -L https://github.com/ImpossibleForge/pfc-jsonl/releases/latest/download/pfc_jsonl-macos-arm64 \
     -o /usr/local/bin/pfc_jsonl && chmod +x /usr/local/bin/pfc_jsonl
```

> **License note:** `pfc_jsonl` is free for personal and open-source use. Commercial use requires a written license — see [pfc-jsonl](https://github.com/ImpossibleForge/pfc-jsonl).

```bash
pip install psycopg2-binary
```

---

## Install

```bash
pip install pfc-export-cratedb
```

Or run directly:

```bash
python3 pfc_export_cratedb.py --help
```

---

## Usage

### Export a full table

```bash
pfc-export-cratedb \
  --host crate.example.com \
  --table application_logs \
  --output logs_archive.pfc
```

### Export a time range

```bash
pfc-export-cratedb \
  --host crate.example.com \
  --table application_logs \
  --ts-column timestamp \
  --from-ts "2026-01-01T00:00:00" \
  --to-ts   "2026-02-01T00:00:00" \
  --output  logs_jan2026.pfc \
  --verbose
```

### Auto-generated output filename

```bash
# Produces: application_logs_20260101T000000_20260201T000000.pfc
pfc-export-cratedb \
  --host crate.example.com \
  --table application_logs \
  --ts-column timestamp \
  --from-ts "2026-01-01T00:00:00" \
  --to-ts   "2026-02-01T00:00:00"
```

---

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `--host` | required | CrateDB hostname or IP |
| `--port` | `5432` | PostgreSQL wire protocol port |
| `--user` | `crate` | Username |
| `--password` | *(empty)* | Password |
| `--dbname` | `doc` | Database name |
| `--schema` | `doc` | Schema |
| `--table` | required | Table name to export |
| `--ts-column` | — | Timestamp column for filtering and ORDER BY |
| `--from-ts` | — | Start of time range (ISO 8601, inclusive) |
| `--to-ts` | — | End of time range (ISO 8601, exclusive) |
| `--output` | auto | Output `.pfc` file |
| `--batch-size` | `10000` | Rows per fetch batch |
| `--verbose` / `-v` | — | Show row progress and size stats |
| `--pfc-binary` | auto-detect | Path to `pfc_jsonl` binary |

---

## Query the archive

Once exported, query with DuckDB:

```sql
INSTALL pfc FROM community;
LOAD pfc;
LOAD json;

SELECT line->>'$.level', line->>'$.message'
FROM read_pfc_jsonl(
    'logs_jan2026.pfc',
    ts_from = epoch(TIMESTAMPTZ '2026-01-15T08:00:00+00'),
    ts_to   = epoch(TIMESTAMPTZ '2026-01-15T09:00:00+00')
);
```

Or via [pfc-gateway](https://github.com/ImpossibleForge/pfc-gateway) HTTP REST API:

```bash
curl -s -X POST http://localhost:8765/query \
  -H "X-API-Key: secret" \
  -d '{"file": "logs_jan2026.pfc", "from_ts": "2026-01-15T08:00", "to_ts": "2026-01-15T09:00"}'
```

---

## How it works

```
CrateDB table
    │
    ▼  psycopg2 (PostgreSQL wire protocol, port 5432)
    │  fetchmany(10,000) batching — memory-safe, no named cursors
    │
    ▼  JSONL temp file
    │  timestamp alias added: ts-column → "timestamp" field
    │  (ensures pfc_jsonl block index works regardless of column name)
    │
    ▼  pfc_jsonl compress
    │
    ▼  output.pfc  +  output.pfc.bidx  +  output.pfc.idx
```

> **Note for programmatic use:** `pfc_jsonl` writes compression progress lines (e.g. `[PFC-JSONL] Compressing block 1/4...`) to stdout alongside the output. When parsing stdout programmatically, only process lines that start with `{`.

---

## Test results

```
pfc-export-cratedb v0.1.0 — Test Suite
16/16 PASS | 0 FAIL | 19.0s

✅ Full table export (50k rows) — Roundtrip row count: 50,000 / 50,000
✅ Time-range export
✅ Auto output filename
✅ Explicit --schema
✅ Small batch-size (1000)
✅ Stress export 50k rows — ~17,700 rows/s
✅ Wrong host → clear error
✅ Wrong port → clear error
✅ Wrong credentials → clear error
✅ Table not found → clear error
✅ pfc_jsonl binary missing → clear error
✅ Empty result set → 0 rows gracefully (exit 0)
✅ CrateDB down → clean error, no crash
✅ --version, --help
```

---

## Part of the PFC Ecosystem

**[→ View all PFC tools & integrations](https://github.com/ImpossibleForge/pfc-jsonl#ecosystem)**

| Direct integration | Why |
|---|---|
| [pfc-archiver-cratedb](https://github.com/ImpossibleForge/pfc-archiver-cratedb) | Same DB, different mode — archiver runs as a continuous daemon; exporter is one-shot CLI |
| [pfc-export-questdb](https://github.com/ImpossibleForge/pfc-export-questdb) | Same tool for QuestDB |
| [pfc-migrate](https://github.com/ImpossibleForge/pfc-migrate) | Complement — migrates existing gzip/zstd file archives to .pfc |

---

## Disclaimer

pfc-export-cratedb is an independent open-source project and is not affiliated with, endorsed by, or associated with Crate.io GmbH or the CrateDB project.

---

## License

pfc-export-cratedb (this repository) is released under the MIT License — see [LICENSE](LICENSE).

The PFC-JSONL binary (`pfc_jsonl`) is proprietary software — free for personal and open-source use. Commercial use requires a license: [info@impossibleforge.com](mailto:info@impossibleforge.com)
