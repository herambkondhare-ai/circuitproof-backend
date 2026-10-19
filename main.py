"""
CircuitProof API (main.py)
Run:  uvicorn main:app --reload --port 8000      Docs: http://127.0.0.1:8000/docs
"""

import os
import re
import shutil
import tempfile

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from circuitproof_core import CircuitReferee, summarize
from design import DesignError, parse_design
from generator import SchematicSynthesizer, SynthesisError
from knowledge_store import KnowledgeStore
from llm_client import LLMClient, LLMError
from planner import DesignPlanner, PlanningError
from report_exporter import ReportExporter
from scan_project import KiCadCliError, run_erc, scan_schematic

OUTPUT_DIR = os.path.abspath(os.getenv("CP_OUTPUT_DIR", "output"))
os.makedirs(OUTPUT_DIR, exist_ok=True)

store = KnowledgeStore(os.getenv("CP_DB", "circuitproof_notebook.db"))
referee = CircuitReferee(store)
reporter = ReportExporter(OUTPUT_DIR)
llm = LLMClient()
try:
    synth = SchematicSynthesizer(OUTPUT_DIR, symbol_dirs=[os.getenv("CP_SYMBOL_DIR")])
    synth_error = None
except SynthesisError as e:
    synth, synth_error = None, str(e)
planner = DesignPlanner(llm, store, synth, referee) if synth else None

app = FastAPI(title="CircuitProof", version="0.2.0",
              description="Compiler and linter for hardware schematics")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CP_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173,"
                            "http://localhost:3000,http://127.0.0.1:3000").split(","),
    allow_methods=["*"], allow_headers=["*"])


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9_-]", "_", name).strip("_")[:60] or "design"


def file_url(path):
    return f"/api/files/{os.path.basename(path)}"


class PromptGenRequest(BaseModel):
    user_prompt: str = Field(min_length=3, examples=["STM32F103 with a BNO085 IMU over I2C, 3.3 V"])
    project_name: str = "circuitproof_design"
    max_repair_rounds: int = Field(3, ge=0, le=6)


class DesignAuditRequest(BaseModel):
    project_name: str = "design"
    design: dict


@app.get("/api/health")
def health():
    return {"status": "ok", "llm": llm.info(), "symbols_ready": synth is not None,
            "symbols_error": synth_error, "parts_in_catalog": len(store.catalog())}


@app.get("/api/parts")
def parts():
    return store.catalog()


@app.post("/api/generate-from-prompt")
def generate_from_prompt(req: PromptGenRequest):
    if planner is None:
        raise HTTPException(503, f"KiCad symbol libraries not available: {synth_error}")
    name = safe_name(req.project_name)
    try:
        plan = planner.run(req.user_prompt, req.max_repair_rounds)
    except LLMError as e:
        raise HTTPException(502, str(e))
    except PlanningError as e:
        raise HTTPException(422, {"message": str(e), "history": e.history})

    try:
        sch_path, warnings = synth.synthesize(f"{name}.kicad_sch", *plan.design.generator_inputs())
    except SynthesisError as e:
        raise HTTPException(422, {"message": f"Schematic synthesis failed: {e}", "history": plan.history})

    findings = referee.audit(plan.design, unconnected_pins=synth.last_unconnected, extra_warnings=warnings)
    erc = run_erc(sch_path)
    report = reporter.generate(name, findings, kind="generation", design=plan.design, history=plan.history,
                               erc=erc, engine=llm.info(), files=[os.path.basename(sch_path)])
    return {
        "status": "verified" if plan.passed else "needs_review",
        "summary": summarize(findings),
        "design": plan.design.to_dict(),
        "findings": [f.to_dict() for f in findings],
        "repair_history": plan.history,
        "kicad_erc": erc,
        "files": {"schematic": file_url(sch_path), "report": file_url(report)},
    }


@app.post("/api/audit")
async def audit_schematic(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".kicad_sch"):
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
    report = reporter.generate(name, findings, kind="audit", design=design, erc=erc)
    return {"summary": summarize(findings), "design": design.to_dict(),
            "findings": [f.to_dict() for f in findings], "kicad_erc": erc,
            "files": {"report": file_url(report)}}


@app.post("/api/audit-design")
def audit_design(req: DesignAuditRequest):
    try:
        design = parse_design(req.design)
    except DesignError as e:
        raise HTTPException(422, {"message": "Invalid design", "errors": e.errors})
    name = safe_name(req.project_name)
    findings = referee.audit(design)
    report = reporter.generate(name, findings, kind="audit", design=design)
    return {"summary": summarize(findings), "findings": [f.to_dict() for f in findings],
            "files": {"report": file_url(report)}}


@app.get("/api/files/{filename}")
def get_file(filename: str):
    path = os.path.join(OUTPUT_DIR, os.path.basename(filename))
    if not os.path.isfile(path):
        raise HTTPException(404, "File not found")
    return FileResponse(path, filename=os.path.basename(path))
