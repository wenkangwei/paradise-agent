"""Proactive Agent Routes — polling endpoints for Android to receive
agent-initiated messages.

Endpoints:
  GET  /api/agent/proactive/poll?conv_id=xxx     Long-poll (30s) for next proactive message
  POST /api/agent/proactive/register              Register a conversation for proactive checks
  POST /api/agent/proactive/unregister            Stop proactive checks for a conversation
  GET  /api/agent/proactive/status                Get scheduler status
  POST /api/agent/proactive/activity               Notify that user sent a message (reset cooldown)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from proactive.scheduler import get_scheduler

logger = logging.getLogger("proactive_routes")
router = APIRouter(prefix="/api/agent/proactive", tags=["proactive"])


class RegisterRequest(BaseModel):
    conv_id: str


@router.post("/register")
async def register(body: RegisterRequest):
    """Register a conversation for periodic proactive checks."""
    scheduler = get_scheduler()
    scheduler.register(body.conv_id)
    return {"status": "ok", "conv_id": body.conv_id, "proactive_enabled": scheduler.enabled}


@router.post("/unregister")
async def unregister(body: RegisterRequest):
    """Stop proactive checks for a conversation."""
    scheduler = get_scheduler()
    scheduler.unregister(body.conv_id)
    return {"status": "ok", "conv_id": body.conv_id}


@router.get("/poll")
async def poll(request: Request, conv_id: str = ""):
    """Long-poll for the next proactive message.

    Blocks up to 30 seconds waiting for a message, then returns it.
    Returns {"message": null} if timeout expires with no message.
    """
    if not conv_id:
        return JSONResponse(status_code=400, content={"error": "conv_id required"})

    scheduler = get_scheduler()
    if not scheduler.enabled:
        return {"message": None, "reason": "proactive_disabled"}

    message = await scheduler.poll(conv_id, timeout=30.0)
    return {"message": message}


@router.get("/status")
async def status():
    """Get proactive scheduler status."""
    scheduler = get_scheduler()
    return scheduler.get_status()


@router.post("/activity")
async def activity(body: RegisterRequest):
    """Notify that user sent a message — resets the proactive cooldown."""
    scheduler = get_scheduler()
    scheduler.record_user_activity(body.conv_id)
    return {"status": "ok"}
