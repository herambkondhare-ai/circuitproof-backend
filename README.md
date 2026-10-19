# CircuitProof backend

A compiler and linter for hardware schematics. Nemotron proposes a design, a deterministic referee
checks it, and nothing is written to KiCad until the referee has had its say. Existing schematics
can be audited too.

```
prompt ─▶ Nemotron (planner.py) ─▶ schema + KiCad library check ─▶ referee (circuitproof_core.py)
              ▲                                                         │
              └──────────── errors and FAIL findings fed back ──────────┘
                                         │ pass (or repair limit reached)
                                         ▼
                   generator.py ─▶ .kicad_sch ─▶ kicad-cli ERC ─▶ audit report (.md)
```

## Setup

```bash
pip install -r requirements.txt
```

**KiCad symbol libraries.** Use the `kicad-symbols` checkout that matches your installed KiCad version
(check out the matching branch/tag). Libraries newer than your KiCad can produce a schematic version your
KiCad refuses to open. Point CircuitProof at the folder containing the `*.kicad_sym` files (or
`*.kicad_symdir` folders):

```bash
set CP_SYMBOL_DIR=C:\path\to\kicad-symbols        # Windows
export CP_SYMBOL_DIR=/path/to/kicad-symbols       # macOS / Linux
```

For a reliable demo, copy just the libraries you use (e.g. `Device`, `MCU_ST_STM32F1`) into `./symbols`.

**kicad-cli** (KiCad 8+) is used for auditing uploaded schematics and for ERC. It is found automatically
on the PATH or in the default Windows install folder; otherwise set `KICAD_CLI`.

**Nemotron.** Pick one:

| Where it runs | Variables |
| --- | --- |
| Local, Ollama (default) | `CP_LLM_PROVIDER=ollama`, `CP_LLM_MODEL=<your pulled Nemotron model tag>` |
| NVIDIA API catalog | `CP_LLM_PROVIDER=nvidia`, `CP_LLM_MODEL=<model id from build.nvidia.com>`, `CP_LLM_API_KEY=...` |
| Any OpenAI-compatible endpoint (e.g. Nebius) | `CP_LLM_PROVIDER=custom`, `CP_LLM_BASE_URL=...`, `CP_LLM_MODEL=...`, `CP_LLM_API_KEY=...` |

Copy the model id exactly from your provider's catalog. Never commit API keys.

**Knowledge store.**

```bash
python load_components.py     # seeds parts and maps them to installed KiCad symbols
python selftest.py            # offline checks, no LLM or KiCad needed
uvicorn main:app --reload --port 8000
```

Open http://127.0.0.1:8000/docs and call `GET /api/health` first; it reports whether the LLM,
symbol libraries and parts catalog are ready.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/health` | LLM / libraries / catalog status |
| GET | `/api/parts` | Parts Nemotron is allowed to use |
| POST | `/api/generate-from-prompt` | `{user_prompt, project_name, max_repair_rounds}` → design, findings, repair history, ERC, file links |
| POST | `/api/audit` | multipart upload of a `.kicad_sch` → findings, ERC, report link |
| POST | `/api/audit-design` | `{project_name, design}` → findings (no LLM or KiCad needed) |
| GET | `/api/files/{name}` | download a generated schematic or report |

Error codes: 422 = design problem (details in body), 502 = LLM call failed, 503 = KiCad libraries or
kicad-cli missing.

## Referee rules

Each finding is PASS, FAIL, WARN or UNVERIFIED. Missing data is reported as UNVERIFIED and never
replaced with a guessed default.

- **Supply:** supply pins on a rail within the part's range; not tied to ground; declared supply pins connected.
- **Ground:** ground pins on a ground net.
- **Power pins:** IC power pins left unconnected.
- **I2C:** pull-up present on SDA/SCL; pull-up voltage ≥ every device's VIH and ≤ its maximum input voltage;
  pull-up value 1k–10k; no pull-downs or duplicate pull-ups.
- **SPI:** declared bus clock ≤ each device's limit.
- **Connectivity:** nets that reach only one pin.
- **Part data:** ICs with no facts in the knowledge store.

## Before the demo

1. The seed numbers in `load_components.py` are **unverified**. Check each against its datasheet, then set
   `"verified": True`. Reports label which findings rest on unverified data.
2. Add `vin_max` (or per-pin `vin_max`) for your I2C parts, otherwise level checks stay UNVERIFIED.
3. Add the parts you want Nemotron to use (regulator, your sensor) with their real KiCad `lib_id`.
4. `demo/buggy_i2c.json` contains planted bugs (missing SCL pull-up, sensor on +5 V);
   `demo/good_i2c.json` is the fixed version. Send either to `/api/audit-design`.

## Changes from the previous version

- `main.py`: complete app (imports, models, CORS, error codes, audit and download endpoints); no silent fallback design.
- `cloud_reviewer.py` → **deleted**, replaced by `llm_client.py` (fixed the syntax error; local/NVIDIA/custom; raises on failure).
- `planner.py` (new): strict JSON contract, catalog-constrained parts, generate → verify → repair loop.
- `design.py` (new): the single design format shared by every module.
- `circuitproof_core.py`: rules run over the actual netlist; UNVERIFIED instead of invented defaults; correct I2C logic.
- `knowledge_store.py`: one facts schema, part ↔ KiCad symbol map, provenance (`verified`, `source`), thread-safe.
- `scan_project.py`: connectivity via KiCad's netlist exporter; never writes guessed facts; ERC runner.
- `report_exporter.py`: built from findings; shows verdict, repair history, ERC and data provenance.
- `generator.py`: real pin positions, embedded symbols, grid snapping, zone placement, net labels; supports `.kicad_symdir`.
- `load_components.py`: new schema, symbol-mapping check, honest verification flags.
- Added `requirements.txt`, `selftest.py`, demo fixtures, `.gitignore`.

The old `components` table in `circuitproof_notebook.db` is no longer used. Delete the file and re-run
`load_components.py` for a clean store.
