"""AiChat Agent Server — FastAPI entry point.

Minimal server for the Phase 1 PC-agent bridge: OpenAI-compatible proxy
to local Ollama. No DB, no agent runtimes, no frontend static files.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import openai_proxy

app = FastAPI(
    title="AiChat Agent Server",
    description="OpenAI-compatible proxy bridging Android AiChat to inference backends",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(openai_proxy.router, prefix="/v1")


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}
