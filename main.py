#!/usr/bin/env python3
"""
main.py -- entry point for Ingestion Universal
================================================
Run:
    pip install -r requirements.txt
    python main.py                     # -> http://localhost:8000
    # or: uvicorn main:app --reload
"""
from __future__ import annotations

from src.app import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
