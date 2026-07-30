#!/bin/bash
# Start the AiChat Agent Server
# Assumes Ollama is already running on localhost:11434.
# Set INFER_BASE_URL to override (e.g., ORPO serve_orpo.py on :8001).

cd "$(dirname "$0")"
PYTHONPATH=server NO_PROXY='*' exec python3 -m uvicorn main:app --host 0.0.0.0 --port 8000
