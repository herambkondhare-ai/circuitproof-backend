"""
Audit report exporter (report_exporter.py)

Builds the Markdown report from referee findings. Every row states exactly what was checked;
nothing is reported as verified unless a rule actually checked it.
"""

import os
from datetime import datetime

from circuitproof_core import FAIL, PASS, UNVERIFIED, WARN, summarize

ICONS = {FAIL: "❌ FAIL", WARN: "⚠️ WARN", UNVERIFIED: "❔ UNVERIFIED", PASS: "✅ PASS"}


def _cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


class ReportExporter:
    def __init__(self, output_dir="."):
        self.output_dir = output_dir

    def generate(self, project_name, findings, *, kind="audit", design=None, history=None,
                 erc=None, engine=None, files=None):
        summary = summarize(findings)
        path = os.path.join(self.output_dir, f"{project_name}_audit_report.md")
        L = [f"# CircuitProof Audit Report: {project_name}", "",
             f"**Generated:** {datetime.now():%Y-%m-%d %H:%M:%S}  ",
             f"**Mode:** {'Generated from prompt' if kind == 'generation' else 'Audit of existing schematic'}  "]
        if engine:
            where = "local" if engine.get("runs_locally") else "remote"
            L.append(f"**Planner model:** {engine.get('model')} via {engine.get('provider')} ({where})  ")
        L += ["**Checker:** CircuitProof deterministic referee", "",
              f"## Verdict: {summary['verdict']}", "",
              " · ".join(f"{ICONS[s]}: {n}" for s, n in summary["counts"].items()), ""]

        L += ["## Findings", "", "| Status | Rule | Finding | Fix | Data |", "| --- | --- | --- | --- | --- |"]
        for f in findings:
            data = {True: "datasheet-verified", False: "seed data, not yet verified", None: "—"}[f.data_verified]
            L.append(f"| {ICONS[f.status]} | {_cell(f.rule)} | {_cell(f.message)} | {_cell(f.fix or '—')} | {data} |")
        if not findings:
            L.append("| — | — | No rules applied to this design | — | — |")

        if history:
            L += ["", "## Generate → verify → repair history", "", "| Round | Result | Problems sent back |",
                  "| --- | --- | --- |"]
            for h in history:
                probs = h["schema_errors"] + h["violations"]
                L.append(f"| {h['round']} | {h['status']} | {_cell('; '.join(probs) or 'none')} |")

        if erc is not None:
            L += ["", "## KiCad ERC (kicad-cli)", "", f"Errors: {erc['errors']} · Warnings: {erc['warnings']}", ""]
            for item in erc["items"][:30]:
                pins = "; ".join(item.get("pins") or [])
                L.append(f"- **{item['severity']}** {_cell(item['type'])}: {_cell(item['description'])}"
                         + (f" ({_cell(pins)})" if pins else ""))

        if design is not None:
            L += ["", "## Components", "", "| Ref | Symbol | Value |", "| --- | --- | --- |"]
            for c in design.components:
                L.append(f"| {c.reference} | {_cell(c.lib_id)} | {_cell(c.value or '—')} |")

        if files:
            L += ["", "## Files", ""] + [f"- `{f}`" for f in files]

        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(L) + "\n")
        return path
