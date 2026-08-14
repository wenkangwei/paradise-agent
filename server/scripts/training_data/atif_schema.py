"""
ATIF v1.7 schema — vendored from neulab/agent-data-protocol (schema/atif.py).

Source: https://github.com/neulab/agent-data-protocol/blob/main/schema/atif.py
License: MIT (see upstream repo).

This file is intentionally kept verbatim from upstream so we can sync with
`git diff` when ATIF revs (e.g. v1.8). Local modifications belong in
`to_atif.py` adapters, not here.

Only one dependency: pydantic>=2.0 (already in server/requirements.txt).
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any, Literal, Union, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ATIF_SCHEMA_VERSION = "ATIF-v1.7"


class ImageSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_type: str | None = None
    path: str


class ContentPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text", "image"]
    text: str | None = None
    source: ImageSource | None = None
    extra: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_content_shape(self):
        if self.type == "text" and self.text is None:
            raise ValueError("text content parts require text")
        if self.type == "image" and self.source is None:
            raise ValueError("image content parts require source")
        return self


ATIFContent = Union[str, list[ContentPart]]


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call_id: str
    function_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    extra: dict[str, Any] | None = None


class ObservationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_call_id: str | None = None
    content: ATIFContent
    subagent_trajectory_ref: list[dict[str, Any]] | None = None
    extra: dict[str, Any] | None = None


class ATIFObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[ObservationResult] = Field(default_factory=list)
    extra: dict[str, Any] | None = None


class Metrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    cost: float | None = None
    prompt_token_ids: list[int] | None = None
    completion_token_ids: list[int] | None = None
    logprobs: list[Any] | None = None
    extra: dict[str, Any] | None = None


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: int
    timestamp: str | None = None
    source: Literal["system", "user", "agent"]
    message: ATIFContent = ""
    model_name: str | None = None
    reasoning_effort: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[ToolCall] | None = None
    observation: ATIFObservation | None = None
    metrics: Metrics | None = None
    llm_call_count: int | None = None
    is_copied_context: bool | None = None
    extra: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_agent_fields_and_links(self):
        if self.source != "agent":
            if self.tool_calls:
                raise ValueError("tool_calls are only valid on agent steps")
            if self.reasoning_content is not None or self.reasoning_effort is not None:
                raise ValueError("reasoning fields are only valid on agent steps")

        return self


class Agent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "atif"
    version: str = "unknown"
    model_name: str | None = None
    tool_definitions: list[dict[str, Any]] | None = None
    extra: dict[str, Any] | None = None


class ATIFTrajectory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = ATIF_SCHEMA_VERSION
    session_id: str | None = None
    trajectory_id: str | None = None
    agent: Agent = Field(default_factory=Agent)
    steps: list[Step] = Field(..., min_length=1)
    notes: str | None = None
    final_metrics: dict[str, Any] | None = None
    continued_trajectory_ref: str | None = None
    extra: dict[str, Any] | None = None
    subagent_trajectories: list[dict[str, Any]] | None = None

    @field_validator("schema_version")
    def validate_schema_version(cls, value: str) -> str:
        if value != ATIF_SCHEMA_VERSION:
            raise ValueError(f"Unsupported ATIF schema_version {value!r}")
        return value

    @model_validator(mode="after")
    def validate_sequential_step_ids(self):
        step_ids = [step.step_id for step in self.steps]
        expected = list(range(1, len(self.steps) + 1))
        if step_ids != expected:
            raise ValueError(f"ATIF step_id values must be sequential starting at 1: {step_ids}")
        return self

    @model_validator(mode="after")
    def validate_observation_links(self):
        for step in self.steps:
            if not step.observation or not step.tool_calls:
                continue
            tool_call_ids = {tool_call.tool_call_id for tool_call in step.tool_calls}
            for result in step.observation.results:
                if result.source_call_id is not None and result.source_call_id not in tool_call_ids:
                    raise ValueError(
                        "observation source_call_id must reference a tool_call_id "
                        "from the same step when that step contains tool calls"
                    )
        return self


def content_to_text(content: ATIFContent) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if part.type == "text" and part.text is not None:
            parts.append(part.text)
        elif part.type == "image" and part.source is not None:
            parts.append(f"[Image: {part.source.path}]")
    return "\n".join(parts)
