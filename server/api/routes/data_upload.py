"""Data Upload Route — unified endpoint for Android to upload session dialogues,
behavior events, and user profiles.

Single endpoint handles all data types via `data_type` discriminator:

  POST /api/data/upload
  {
    "upload_id": "batch_uuid",
    "data_type": "session_dialogue" | "behavior_events" | "user_profile",
    "payload": { ... type-specific data ... },
    "context": { "timestamp", "ip", "geo", "locale", "timezone" },
    "client_info": { "user_id", "device_id", "app_version" }
  }

The protocol is designed for extensibility:
  - New data_type values can be added without breaking existing clients
  - event_type in behavior_events is an open enum (new actions added as app grows)
  - page/scene field allows tracking beyond chat (future pages)
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from data_store import session_store, behavior_store, profile_store

logger = logging.getLogger("data_upload")
router = APIRouter(prefix="/api/data", tags=["data"])


class ContextInfo(BaseModel):
    timestamp: int | None = None
    ip: str | None = None
    geo: dict | None = None       # {lat, lng, city} — reserved, not collected yet
    locale: str | None = None
    timezone: str | None = None


class ClientInfo(BaseModel):
    user_id: str = ""
    device_id: str = ""
    app_version: str = ""
    page: str = "chat"             # future: "chat" | "settings" | "discover" | ...
    scene: str = "default"         # future: sub-scene within a page


class UploadRequest(BaseModel):
    upload_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    data_type: str                 # "session_dialogue" | "behavior_events" | "user_profile"
    payload: dict[str, Any]
    context: ContextInfo | None = None
    client_info: ClientInfo | None = None

    model_config = {"extra": "allow"}


@router.post("/upload")
async def upload_data(request: Request, body: UploadRequest):
    """Unified data upload endpoint.

    Routes payload to the appropriate store based on data_type.
    All stores are append-only / idempotent — safe to retry.
    """
    # Extract client IP (not sent by client, taken from request)
    client_host = request.client.host if request.client else ""
    ctx_dict = body.context.model_dump() if body.context else {}
    if client_host and not ctx_dict.get("ip"):
        ctx_dict["ip"] = client_host
    ci_dict = body.client_info.model_dump() if body.client_info else {}

    data_type = body.data_type
    payload = body.payload

    try:
        if data_type == "session_dialogue":
            return _handle_session(body.upload_id, payload, ctx_dict, ci_dict)
        elif data_type == "behavior_events":
            return _handle_behavior(body.upload_id, payload, ctx_dict, ci_dict)
        elif data_type == "user_profile":
            return _handle_profile(body.upload_id, payload, ctx_dict, ci_dict)
        else:
            return JSONResponse(
                status_code=400,
                content={
                    "upload_id": body.upload_id,
                    "status": "error",
                    "error": f"unknown data_type: {data_type}",
                },
            )
    except Exception as e:
        logger.error("Upload failed (type=%s): %s", data_type, e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "upload_id": body.upload_id,
                "status": "error",
                "error": str(e),
            },
        )


def _handle_session(upload_id: str, payload: dict, ctx: dict, ci: dict) -> JSONResponse:
    """Handle session_dialogue data type.

    Expected payload:
      session_id, sub_session_id, session_span {enter_timestamp, exit_timestamp, exit_reason},
      incremental_messages [{message_id, role, content, timestamp, model?, ...}]
    """
    session_id = payload.get("session_id", "")
    sub_session_id = payload.get("sub_session_id", "")
    batch_upload_id = payload.get("upload_id", upload_id)  # prefer payload.upload_id for filename
    span = payload.get("session_span", {})
    messages = payload.get("incremental_messages", [])

    if not session_id or not sub_session_id:
        return JSONResponse(
            status_code=400,
            content={"upload_id": upload_id, "status": "error", "error": "session_id and sub_session_id required"},
        )

    user_id = ci.get("user_id", "unknown")
    result = session_store.append(
        user_id=user_id,
        session_id=session_id,
        sub_session_id=sub_session_id,
        upload_id=batch_upload_id,
        enter_timestamp=span.get("enter_timestamp", 0),
        exit_timestamp=span.get("exit_timestamp", 0),
        exit_reason=span.get("exit_reason", "unknown"),
        incremental_messages=messages,
        context=ctx,
        client_info=ci,
    )

    return JSONResponse(content={
        "upload_id": upload_id,
        "status": "ok",
        "stored": {"session_messages": result["stored_messages"]},
    })


def _handle_behavior(upload_id: str, payload: dict, ctx: dict, ci: dict) -> JSONResponse:
    """Handle behavior_events data type.

    Expected payload:
      events [{event_id, message_id, session_id, event_type, event_data, client_timestamp}]

    Open event_type enum:
      "reaction" | "share" | "retry" | "tts_playback" | future types...
    """
    user_id = ci.get("user_id", "unknown")
    session_id = payload.get("session_id", "unknown")
    events = payload.get("events", [])
    if not events:
        return JSONResponse(content={
            "upload_id": upload_id,
            "status": "ok",
            "stored": {"behavior_events": 0},
        })

    result = behavior_store.append_batch(
        user_id=user_id, session_id=session_id,
        events=events, context=ctx, client_info=ci
    )

    return JSONResponse(content={
        "upload_id": upload_id,
        "status": "ok",
        "stored": {"behavior_events": result["stored_events"]},
    })


def _handle_profile(upload_id: str, payload: dict, ctx: dict, ci: dict) -> JSONResponse:
    """Handle user_profile data type.

    Expected payload:
      profile_type: "basic" | "full"
      profile: {user_id, device_id, nickname, description?, preferences?}
      updated_fields: [field names that changed] — None means full replace
    """
    user_id = payload.get("user_id") or ci.get("user_id", "")
    if not user_id:
        return JSONResponse(
            status_code=400,
            content={"upload_id": upload_id, "status": "error", "error": "user_id required"},
        )

    profile = payload.get("profile", {})
    profile.setdefault("user_id", user_id)
    updated_fields = payload.get("updated_fields")

    result = profile_store.update(
        user_id=user_id,
        profile=profile,
        updated_fields=updated_fields,
        context=ctx,
        client_info=ci,
    )

    return JSONResponse(content={
        "upload_id": upload_id,
        "status": "ok",
        "stored": {"profile_updated": True, "fields": result.get("fields_updated")},
    })


@router.get("/health")
async def data_health():
    """Health check for the data collection subsystem."""
    return {"status": "ok", "endpoint": "/api/data/upload"}
