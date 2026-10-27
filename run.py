"""
CircuitProof launcher (run.py)
Starts the FastAPI backend, which also serves the Web Studio, at http://127.0.0.1:8000
"""

import sys
from pathlib import Path

import uvicorn

ROOT_DIR = Path(__file__).resolve().parent
for p in (ROOT_DIR, ROOT_DIR / "backend" / "app"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

if __name__ == "__main__":
    print("=" * 60)
    print("  CircuitProof")
    print("  Studio:   http://127.0.0.1:8000")
    print("  API docs: http://127.0.0.1:8000/docs")
    print("  Health:   http://127.0.0.1:8000/api/health")
    print("=" * 60)
    uvicorn.run("backend.app.main:app", host="127.0.0.1", port=8000, reload=False)
