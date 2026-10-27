"""
CircuitProof API + Web Studio (backend/app/main.py)

Run from the repository root:   python run.py
   or:                          uvicorn backend.app.main:app --reload --port 8000
Studio: http://127.0.0.1:8000     API docs: http://127.0.0.1:8000/docs
"""

import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

from circuitproof_core import CircuitReferee, summarize  # noqa: E402
from design import DesignError, parse_design  # noqa: E402
from generator import SchematicSynthesizer, SynthesisError  # noqa: E402
from knowledge_store import DEFAULT_DB, KnowledgeStore  # noqa: E402
from llm_client import LLMClient, LLMError  # noqa: E402
from load_components import seed  # noqa: E402
from planner import GEN_BUDGET_S, DesignPlanner, PlanningError, PlanningTimeout  # noqa: E402
from report_exporter import ReportExporter  # noqa: E402
from scan_project import KiCadCliError, find_kicad_cli, run_erc, scan_directory, scan_schematic  # noqa: E402
from symbol_index import SymbolIndex  # noqa: E402
from timing import set_stage, timed, watching  # noqa: E402
from ui_compat import design_input_from_ui, explanation, ui_components, ui_findings, ui_nets  # noqa: E402

VERSION = "2.0.0"
BACKEND_DIR = APP_DIR.parent
ROOT_DIR = BACKEND_DIR.parent
FRONTEND_DIR = ROOT_DIR / "frontend"
PUBLIC_DIR = FRONTEND_DIR / "public"
DEMO_DIR = BACKEND_DIR / "demo"
EXPORTS_DIR = Path(os.getenv("CP_OUTPUT_DIR") or BACKEND_DIR / "exports")
SCAN_ROOT = Path(os.getenv("CP_SCAN_ROOT") or ROOT_DIR).resolve()
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------------ engines
store = KnowledgeStore(os.getenv("CP_DB") or DEFAULT_DB)
referee = CircuitReferee(store)
reporter = ReportExporter(str(EXPORTS_DIR))
llm = LLMClient()
if not llm.thinking_off:
    print(f"[CircuitProof] CP_LLM_THINKING is on: reasoning replies can take minutes, so calls may hit the "
          f"{llm.timeout:.0f}s per-call cap (CP_LLM_TIMEOUT) or the {GEN_BUDGET_S:.0f}s generation budget "
          f"(CP_GEN_BUDGET_S). Unset CP_LLM_THINKING for fast generation.")
try:
    synth = SchematicSynthesizer(str(EXPORTS_DIR), symbol_dirs=[os.getenv("CP_SYMBOL_DIR")])
    synth_error = None
except SynthesisError as e:
    synth, synth_error = None, str(e)
symbol_index = SymbolIndex(synth.lib.dirs if synth else [], str(BACKEND_DIR / "data" / "symbol_index.json"))
if synth:
    symbol_index.build_in_background()   # first run takes a little while; later starts load the cache


def erc_check(design):
    """Draw the design to a scratch schematic and run KiCad's ERC on it, so the planner can send real ERC
    errors back to the model. Returns None when kicad-cli is not installed."""
    if not kicad_cli_available():
        return None
    with tempfile.TemporaryDirectory() as tmp:
        try:
            path, _ = synth.synthesize(os.path.join(tmp, "erc_check.kicad_sch"), *design.generator_inputs(),
                                       open_single_pin_nets=True)  # drawn exactly like the delivered schematic
        except SynthesisError as e:
            return [f"[kicad-erc] the schematic could not be drawn: {e}"]
        erc = run_erc(path)
    if erc is None:
        return None
    return [f"[kicad-erc] {i['description']}: {'; '.join(i.get('pins') or [])}"
            for i in erc["items"] if i["severity"] == "error"]


planner = DesignPlanner(llm, store, synth, referee, index=symbol_index, erc_check=erc_check) if synth else None

# First run seeds the knowledge store; later runs only add seed parts that are new (never overwrite edits).
seed(store, synth.lib if synth else None, quiet=True, only_missing=store.count_parts() > 0)

app = FastAPI(title="CircuitProof", version=VERSION,
              description="Compiler and linter for hardware schematics: Nemotron proposes, a deterministic referee verifies.")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CP_CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000,http://localhost:5173,"
                            "http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000").split(","),
    allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9_-]", "_", name or "").strip("_")[:60] or "design"


def export_path(filename):
    path = EXPORTS_DIR / os.path.basename(filename)
    if not path.is_file():
        raise HTTPException(404, f"File '{os.path.basename(filename)}' not found")
    return path


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ------------------------------------------------------------------ models
class PromptGenRequest(BaseModel):
    user_prompt: str = Field(min_length=3, max_length=2000,
                             examples=["Minimal STM32F103C8 board on +3V3 with decoupling and a status LED on PC13"])
    project_name: str = "circuitproof_design"
    max_repair_rounds: int = Field(3, ge=0, le=6)


class VerifyRequest(BaseModel):
    components: List[Dict[str, Any]]
    nets: Optional[List[Dict[str, Any]]] = None


class DesignAuditRequest(BaseModel):
    project_name: str = "design"
    design: Dict[str, Any]


class ScanRequest(BaseModel):
    directory: Optional[str] = "."


class MistakeLogRequest(BaseModel):
    violation_text: str = Field(min_length=3)
    fix_text: str = ""


class AnalyticsEvent(BaseModel):
    event_name: str
    properties: Optional[Dict[str, Any]] = None


# ------------------------------------------------------------------ core API
def kicad_cli_available():
    try:
        find_kicad_cli()
        return True
    except KiCadCliError:
        return False


@app.get("/api/health")
def health():
    return {
        "status": "online", "service": "CircuitProof", "version": VERSION,
        "llm": llm.info(),
        "symbols_ready": synth is not None, "symbols_error": synth_error,
        "symbol_index": symbol_index.info(),
        "kicad_cli": kicad_cli_available(),
        "generation": {"busy": generation_lock.locked(), "budget_s": GEN_BUDGET_S},
        "stats": {"total_components": store.count_parts(), "planner_catalog": len(store.catalog())},
    }


@app.post("/api/generate-from-prompt")
def generate_from_prompt(req: PromptGenRequest):
    return run_generation(req)


# The planner and synthesizer keep per-request state on themselves, so generations run one at a time.
generation_lock = threading.Lock()


def run_generation(req: PromptGenRequest):
    """The whole prompt -> schematic pipeline. Raises HTTPException on failure."""
    with generation_lock, timed("generation total"):
        return _run_generation(req)


def _run_generation(req: PromptGenRequest):
    if planner is None:
        raise HTTPException(503, f"KiCad symbol libraries not available: {synth_error}")
    name = safe_name(req.project_name)
    try:
        with timed("planner total"):
            plan = planner.run(req.user_prompt, req.max_repair_rounds)
    except LLMError as e:
        raise HTTPException(502, f"Nemotron request failed: {e}")
    except PlanningTimeout as e:
        raise HTTPException(504, {"message": str(e), "history": e.history})
    except PlanningError as e:
        raise HTTPException(422, {"message": str(e), "history": e.history})

    set_stage("writing schematic")
    try:
        with timed("synthesis"):
            sch_path, warnings = synth.synthesize(f"{name}.kicad_sch", *plan.design.generator_inputs(),
                                                  open_single_pin_nets=True)
    except SynthesisError as e:
        raise HTTPException(422, {"message": f"Schematic synthesis failed: {e}", "history": plan.history})

    with timed("referee (final)"):
        findings = referee.audit(plan.design, unconnected_pins=synth.last_unconnected, extra_warnings=warnings,
                                 symbol_pins=synth.symbol_pins(plan.design.generator_inputs()[0]))
    with timed("kicad-cli ERC"):
        erc = run_erc(sch_path)
    with timed("report"):
        report_path = reporter.generate(name, findings, kind="generation", design=plan.design, history=plan.history,
                                        erc=erc, engine=llm.info(), files=[os.path.basename(sch_path)])
    return {
        "status": ("verified" if plan.passed and summarize(findings)["verdict"] != "FAIL"
                   and not (erc or {}).get("errors") else "needs_review"),
        "summary": summarize(findings),
        "prompt_processed": req.user_prompt,
        "components": ui_components(plan.design, store),
        "nets": ui_nets(plan.design),
        "design": plan.design.to_dict(),
        "findings": ui_findings(findings),
        "repair_history": plan.history,
        "block_plan": plan.blocks,
        "library_candidates": [{"lib_id": c["lib_id"], "block": c["block"], "desc": c["desc"]} for c in plan.candidates],
        "explanation": explanation(plan.history, plan.passed, llm.info(), plan.blocks),
        "kicad_erc": erc,
        "generated_file": os.path.basename(sch_path),
        "generated_report": os.path.basename(report_path),
        "schematic_content": read_text(sch_path),
        "report_content": read_text(report_path),
    }


# ------------------------------------------------------------------ generation jobs (poll instead of one long request)
JOBS = OrderedDict()   # job_id -> job dict, in memory only
JOBS_LOCK = threading.Lock()
MAX_JOBS = 50


def _job_view(job):
    end = job["finished"] or time.time()
    view = {"job_id": job["id"], "status": job["status"], "stage": job["stage"],
            "elapsed_s": round(end - job["created"], 1), "prompt": job["prompt"]}
    if job["status"] == "done":
        view["result"] = job["result"]
    if job["status"] == "error":
        view["error"] = job["error"]
    return view


def _run_job(job, req):
    def on_stage(stage):
        job["stage"] = stage

    try:
        with watching(on_stage):
            # Waiting here means another generation is running: the job stays "queued" until it gets its turn.
            with generation_lock:
                job["status"], job["stage"] = "running", "starting"
                with timed("generation total"):
                    result = _run_generation(req)
        job["result"], job["status"], job["stage"] = result, "done", "done"
    except HTTPException as e:
        job["error"], job["status"] = {"status_code": e.status_code, "detail": e.detail}, "error"
    except Exception as e:  # keep the error visible to the poller instead of losing it in the thread
        print(f"[jobs] {job['id']} crashed: {e!r}")
        job["error"], job["status"] = {"status_code": 500, "detail": f"Internal error: {e}"}, "error"
    finally:
        job["finished"] = time.time()


@app.post("/api/generate-jobs", status_code=202)
def start_generate_job(req: PromptGenRequest):
    """Start a generation in the background. Poll GET /api/generate-jobs/{job_id} for stage and result."""
    if planner is None:
        raise HTTPException(503, f"KiCad symbol libraries not available: {synth_error}")
    job = {"id": uuid.uuid4().hex[:12], "status": "queued", "stage": "queued", "prompt": req.user_prompt,
           "created": time.time(), "finished": None, "result": None, "error": None}
    with JOBS_LOCK:
        JOBS[job["id"]] = job
        finished = [k for k, j in JOBS.items() if j["finished"]]
        for k in finished[:max(0, len(JOBS) - MAX_JOBS)]:
            JOBS.pop(k)
    threading.Thread(target=_run_job, args=(job, req), daemon=True, name=f"job-{job['id']}").start()
    return _job_view(job)


@app.get("/api/generate-jobs/{job_id}")
def generate_job_status(job_id: str):
    """status: queued | running | done | error. 'result' (done) is the same JSON as /api/generate-from-prompt;
    'error' (error) has the status_code and detail that endpoint would have returned."""
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job id (jobs are kept in memory and reset when the server restarts)")
    return _job_view(job)


@app.post("/api/verify")
def verify(req: VerifyRequest):
    """Re-run the referee on the design currently shown in the studio."""
    try:
        design = parse_design(design_input_from_ui(req.components, req.nets))
    except DesignError as e:
        raise HTTPException(422, {"message": "Invalid design", "errors": e.errors})
    findings = referee.audit(design)
    return {"status": "success", "summary": summarize(findings), "findings": ui_findings(findings)}


@app.post("/api/audit-design")
def audit_design(req: DesignAuditRequest):
    try:
        design = parse_design(req.design)
    except DesignError as e:
        raise HTTPException(422, {"message": "Invalid design", "errors": e.errors})
    name = safe_name(req.project_name)
    findings = referee.audit(design)
    report_path = reporter.generate(name, findings, kind="audit", design=design)
    return {"summary": summarize(findings), "findings": ui_findings(findings),
            "components": ui_components(design, store), "nets": ui_nets(design),
            "generated_report": os.path.basename(report_path), "report_content": read_text(report_path)}


@app.post("/api/audit")
async def audit_schematic(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".kicad_sch"):
        raise HTTPException(400, "Upload a .kicad_sch file")
    name = safe_name(os.path.splitext(file.filename)[0])
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, f"{name}.kicad_sch")
        with open(path, "wb") as fh:
            shutil.copyfileobj(file.file, fh)
        try:
            design = scan_schematic(path, store, name)
        except KiCadCliError as e:
            raise HTTPException(503, str(e))
        erc = run_erc(path)
    findings = referee.audit(design)
    report_path = reporter.generate(name, findings, kind="audit", design=design, erc=erc)
    return {"summary": summarize(findings), "findings": ui_findings(findings),
            "components": ui_components(design, store), "nets": ui_nets(design), "kicad_erc": erc,
            "generated_report": os.path.basename(report_path), "report_content": read_text(report_path)}


@app.get("/api/symbols/search")
def symbol_search(q: str = Query(..., min_length=2), limit: int = Query(10, ge=1, le=50)):
    """Search every installed KiCad symbol by name, description and keywords."""
    if not symbol_index.ready:
        raise HTTPException(503, "KiCad symbol index is still building; try again in a minute.")
    return {"query": q, "results": symbol_index.search(q, k=limit)}


# ------------------------------------------------------------------ knowledge store views
@app.get("/api/parts")
def parts_catalog():
    return store.catalog()


@app.get("/api/components")
def components(search: str = "", category: str = "",
               limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    return store.list_parts(search=search, category=category, limit=limit, offset=offset)


@app.get("/api/components/categories")
def component_categories():
    return {"categories": store.categories()}


@app.get("/api/components/{component_id}")
def component_detail(component_id: str):
    part = store.get_part(component_id)
    if not part:
        raise HTTPException(404, "Component not found")
    return {"component_id": part["part_id"], **part}


@app.get("/api/recipes")
def recipes():
    """Reference designs that pass the referee (from backend/demo)."""
    import json
    out = []
    for i, path in enumerate(sorted(DEMO_DIR.glob("good_*.json")), 1):
        data = json.loads(read_text(path))
        out.append({"recipe_id": i, "interface_type": "I2C" if "i2c" in path.stem else "Reference",
                    "design_summary": data.get("project_name", path.stem), "circuit": data["design"]})
    return {"recipes": out}


@app.get("/api/mistakes")
def mistakes():
    return {"mistakes": store.list_mistakes()}


@app.post("/api/mistakes")
def log_mistake(req: MistakeLogRequest):
    store.log_mistake(req.violation_text, req.fix_text)
    return {"status": "success"}


@app.post("/api/scan-directory")
def scan_dir(req: ScanRequest):
    """Catalog KiCad schematics, restricted to folders inside CP_SCAN_ROOT (the repo by default)."""
    target = (SCAN_ROOT / (req.directory or ".")).resolve()
    if target != SCAN_ROOT and SCAN_ROOT not in target.parents:
        raise HTTPException(403, f"Only folders inside {SCAN_ROOT} can be scanned (set CP_SCAN_ROOT to change).")
    if not target.is_dir():
        raise HTTPException(404, f"Directory '{req.directory}' does not exist.")
    results = scan_directory(target, store)
    files = [{"path": str(p.relative_to(target)), "components_extracted": len(d.components) if d else 0,
              "error": err} for p, d, err in results]
    return {"status": "success", "scanned_directory": str(target), "schematic_files_found": len(files),
            "total_components_cataloged": sum(f["components_extracted"] for f in files), "files": files}


# ------------------------------------------------------------------ files
@app.get("/api/files/{filename}")
def get_file(filename: str):
    path = export_path(filename)
    return FileResponse(path, filename=path.name)


@app.get("/api/schematic/{filename}")
def get_schematic(filename: str, download: bool = False):
    path = export_path(filename)
    if download:
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")
    return PlainTextResponse(read_text(path))


@app.get("/api/report/{filename}")
def get_report(filename: str, download: bool = False):
    path = export_path(filename)
    if download:
        return FileResponse(path, filename=path.name, media_type="text/markdown")
    return PlainTextResponse(read_text(path))


# ------------------------------------------------------------------ local usage counters (in memory only)
USAGE = Counter()


@app.post("/api/analytics")
def record_event(ev: AnalyticsEvent):
    USAGE[ev.event_name[:64]] += 1
    return {"status": "ok"}


@app.get("/api/analytics")
def usage_summary():
    return {"total_events": sum(USAGE.values()), "breakdown": dict(USAGE)}


# ------------------------------------------------------------------ static site
PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>{title} | CircuitProof</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg"><style>
body{{background:#080B12;color:#F2F6FF;font:14px/1.6 system-ui,sans-serif;max-width:800px;margin:40px auto;padding:0 20px}}
h1,h2{{color:#31D8F5}} a{{color:#3984FF}} .box{{background:#121A2B;border:1px solid #26344A;border-radius:8px;padding:20px;margin-top:20px}}
</style></head><body><a href="/">← Back to CircuitProof Studio</a><h1>{title}</h1><div class="box">{body}</div></body></html>"""


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    where = ("on this computer (local model)" if llm.info().get("runs_locally")
             else f"to the configured model provider ({llm.provider}) over the internet")
    return PAGE.format(title="Privacy", body=f"""
<h2>What leaves your machine</h2><p>Design prompts are sent {where} to generate a design.
Schematic auditing, the rule referee, the knowledge store and generated files all stay on this machine.</p>
<h2>Stored data</h2><p>Parts, mistakes and scanned-part statistics are kept in a local SQLite file.
Generated schematics and reports are written to <code>backend/exports/</code>.</p>
<h2>Usage counters</h2><p>The studio counts button events in server memory only. Nothing is written to disk
or sent anywhere, and the counts reset when the server restarts.</p>""")


@app.get("/terms", response_class=HTMLResponse)
def terms():
    return PAGE.format(title="Terms", body="""
<h2>Engineering disclaimer</h2><p>CircuitProof is a hackathon prototype. Its rule checks cover a limited set of
electrical rules, and seed component data is marked as unverified until checked against datasheets.
Review every schematic and the official datasheets before building hardware.</p>
<h2>Your designs</h2><p>Schematics and reports you generate are yours.</p>""")


@app.get("/contact", response_class=HTMLResponse)
def contact():
    return PAGE.format(title="Project & Contact", body="""
<p>CircuitProof is a student hackathon project.</p>
<p>Source code: <a href="https://github.com/Lokesh-Tech09/Nvidia">github.com/Lokesh-Tech09/Nvidia</a></p>
<p>Questions and bug reports: open an issue on the repository.</p>""")


def _public(name, media):
    path = PUBLIC_DIR / name
    if path.is_file():
        return FileResponse(path, media_type=media)
    raise HTTPException(404, f"{name} not found")


@app.get("/favicon.svg")
def favicon_svg():
    return _public("favicon.svg", "image/svg+xml")


@app.get("/favicon.ico")
def favicon_ico():
    path = PUBLIC_DIR / "favicon.svg"
    return FileResponse(path, media_type="image/svg+xml") if path.is_file() else Response(status_code=204)


@app.get("/og-image.jpg")
def og_image():
    return _public("og-image.jpg", "image/jpeg")


@app.get("/robots.txt")
def robots():
    return _public("robots.txt", "text/plain")


@app.get("/sitemap.xml")
def sitemap():
    return _public("sitemap.xml", "application/xml")


@app.get("/", response_class=HTMLResponse)
def studio():
    index = FRONTEND_DIR / "index.html"
    if not index.is_file():
        return HTMLResponse("<h1>CircuitProof API is running</h1><p>frontend/index.html not found. "
                            "API docs: <a href='/docs'>/docs</a></p>")
    return HTMLResponse(read_text(index))


NOT_FOUND = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>404 | CircuitProof</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg"><style>
body{background:#080B12;color:#F2F6FF;font:14px/1.5 system-ui,sans-serif;display:grid;place-items:center;height:100vh;margin:0}
.c{text-align:center;background:#121A2B;border:1px solid #26344A;border-radius:12px;padding:40px;max-width:440px}
.n{font:700 64px ui-monospace,monospace;color:#31D8F5;margin:0} a{color:#3984FF}
</style></head><body><div class="c"><p class="n">404</p><h2>Open circuit</h2>
<p>This page does not exist.</p><a href="/">Return to the studio →</a></div></body></html>"""


@app.exception_handler(StarletteHTTPException)
async def http_errors(request: Request, exc: StarletteHTTPException):
    # API callers always get JSON; browsers get a branded page only for unknown pages.
    if exc.status_code == 404 and not request.url.path.startswith("/api/"):
        return HTMLResponse(NOT_FOUND, status_code=404)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.app.main:app", host="127.0.0.1", port=8000, reload=False)
