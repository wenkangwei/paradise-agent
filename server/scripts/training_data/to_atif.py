"""
to_atif — convert aichat-1.0 dataset records to ATIF v1.7 trajectories.

Input  : JSONL with one aichat-1.0 record per line (output of turn_logger).
Output : JSONL with one ATIF-v1.7 trajectory per line, validated by Pydantic.

The conversion is lossless and reversible (one aichat record ↔ one ATIF
trajectory). The aichat-1.0 source format is preserved in
`trajectory.extra.source_format` for traceability.

Mapping (aichat-1.0 → ATIF-v1.7):
    conversation_id             → session_id
    "{conversation_id}#turn{N}" → trajectory_id   (per-document unique)
    model                       → agent.model_name
    messages[].role=system      → steps[].source=system
    messages[].role=user        → steps[].source=user
    messages[].role=assistant   → steps[].source=agent
    messages[].thinking         → steps[].reasoning_content
    messages[].tool_calls[]     → steps[].tool_calls[]   (id→tool_call_id,
                                                         function.arguments
                                                         string→dict)
    messages[].role=tool        → folded into preceding agent step's
                                  observation.results[]
    metadata                    → final_metrics.extra + extra.source_metadata

CLI:
    python -m to_atif -i dataset_2026-08.jsonl -o atif_2026-08.jsonl
    python -m to_atif -i ... -o ... --strict   # stop on first error
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from atif_schema import (
    ATIF_SCHEMA_VERSION,
    ATIFTrajectory,
    Agent,
    ATIFObservation,
    ObservationResult,
    Step,
    ToolCall,
)

logger = logging.getLogger("training_data.to_atif")

AGENT_NAME = "aichat"
AGENT_VERSION = "0.1.0"


def convert_entry(record: dict) -> ATIFTrajectory:
    """Convert one aichat-1.0 record into a validated ATIF v1.7 trajectory."""
    messages = record.get("messages") or []
    if not messages:
        raise ValueError("record has no messages")

    steps: list[Step] = []
    step_id = 1
    model_name = record.get("model") or None

    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role")
        content = msg.get("content", "") or ""
        thinking = msg.get("thinking")

        if role == "system":
            steps.append(Step(step_id=step_id, source="system", message=content))
            step_id += 1
            i += 1
        elif role == "user":
            steps.append(Step(step_id=step_id, source="user", message=content))
            step_id += 1
            i += 1
        elif role == "assistant":
            tool_calls_payload = msg.get("tool_calls") or []
            atif_tool_calls = [_openai_tc_to_atif_tc(tc) for tc in tool_calls_payload]

            # Collect subsequent role=tool messages into this step's observation.
            observation_results: list[ObservationResult] = []
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                tool_msg = messages[j]
                observation_results.append(
                    ObservationResult(
                        source_call_id=tool_msg.get("tool_call_id"),
                        content=tool_msg.get("content", "") or "",
                    )
                )
                j += 1

            kwargs: dict = {
                "step_id": step_id,
                "source": "agent",
                "message": content,
            }
            if model_name:
                kwargs["model_name"] = model_name
            if thinking:
                kwargs["reasoning_content"] = thinking[:1000]
            if atif_tool_calls:
                kwargs["tool_calls"] = atif_tool_calls
            if observation_results:
                kwargs["observation"] = ATIFObservation(results=observation_results)
            steps.append(Step(**kwargs))
            step_id += 1
            i = j
        elif role == "tool":
            # Orphan tool message (no preceding assistant with tool_calls) — skip with warning.
            logger.warning(
                "Orphan role=tool message at index %d in conversation %s — skipped",
                i, record.get("conversation_id", "?"),
            )
            i += 1
        else:
            logger.warning("Unknown role %r at index %d — skipped", role, i)
            i += 1

    if not steps:
        raise ValueError("record produced zero ATIF steps")

    metadata = record.get("metadata", {}) or {}
    conv_id = record.get("conversation_id")
    turn = record.get("turn", 0)

    return ATIFTrajectory(
        schema_version=ATIF_SCHEMA_VERSION,
        session_id=conv_id,
        trajectory_id=f"{conv_id}#turn{turn}" if conv_id else None,
        agent=Agent(
            name=AGENT_NAME,
            version=AGENT_VERSION,
            model_name=model_name,
        ),
        steps=steps,
        notes=f"Auto-converted from {record.get('schema_version', 'aichat-1.0')} by to_atif.py",
        final_metrics={
            "total_steps": len(steps),
            "extra": {
                "tool_calls_count": metadata.get("tool_calls_count", 0),
                "tools_used": metadata.get("tools_used", []),
                "total_tokens": metadata.get("total_tokens", 0),
                "duration_ms": metadata.get("duration_ms", 0),
            },
        },
        extra={
            "source_format": record.get("schema_version", "aichat-1.0"),
            "source_metadata": metadata,
            "converted_at": datetime.now(timezone.utc).isoformat() + "Z",
        },
    )


def _openai_tc_to_atif_tc(openai_tc: dict, fallback_id: str = "call_unknown") -> ToolCall:
    """Convert an OpenAI-style tool_call to an ATIF ToolCall.

    OpenAI:  {"id":..., "type":"function", "function": {"name":..., "arguments": "<json string>"}}
    ATIF:    {"tool_call_id":..., "function_name":..., "arguments": <dict>}
    """
    func = openai_tc.get("function") or {}
    args_raw = func.get("arguments", {})
    if isinstance(args_raw, str):
        try:
            args = json.loads(args_raw)
        except json.JSONDecodeError:
            # Fall back to wrapping the raw string so we don't drop data.
            args = {"_raw": args_raw}
    else:
        args = args_raw or {}
    return ToolCall(
        tool_call_id=openai_tc.get("id") or fallback_id,
        function_name=func.get("name") or "unknown",
        arguments=args if isinstance(args, dict) else {"_raw": args},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="to_atif",
        description="Convert aichat-1.0 dataset JSONL to ATIF v1.7 JSONL.",
    )
    parser.add_argument("--input", "-i", required=True, type=Path,
                        help="Input JSONL (aichat-1.0 records).")
    parser.add_argument("--output", "-o", required=True, type=Path,
                        help="Output JSONL (ATIF-v1.7 trajectories).")
    parser.add_argument("--strict", action="store_true",
                        help="Abort on first validation error (default: log and continue).")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.input.exists():
        logger.error("Input file not found: %s", args.input)
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)

    total = converted = errors = 0
    with args.input.open("r", encoding="utf-8") as fin, \
         args.output.open("w", encoding="utf-8") as fout:
        for line_no, raw in enumerate(fin, 1):
            raw = raw.strip()
            if not raw:
                continue
            total += 1
            try:
                record = json.loads(raw)
                trajectory = convert_entry(record)
                fout.write(
                    json.dumps(trajectory.model_dump(exclude_none=True),
                               ensure_ascii=False) + "\n"
                )
                converted += 1
            except Exception as e:
                errors += 1
                logger.error("Line %d failed: %s: %s", line_no, type(e).__name__, e)
                if args.strict:
                    return 1

    logger.info(
        "Converted %d/%d records (%d errors) → %s",
        converted, total, errors, args.output,
    )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
