"""YOLO Object Detection Skill — detect objects in images using YOLOv8.

Self-registers as tool "yolo_detect" in toolset "vision".
Uses ultralytics YOLOv8-nano (~6MB) for fast CPU inference.
First run downloads the model automatically.

Usage in agent TOOL phase:
  tool_call: yolo_detect(path="/tmp/image.jpg", confidence=0.5)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from paradise.tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

CONFIDENCE_DEFAULT = 0.5
MAX_OBJECTS_DEFAULT = 10

DETECT_EMOJI = "\U0001f50d"

YOLO_DETECT_SCHEMA = {
    "name": "yolo_detect",
    "description": (
        "Detect objects in an image using YOLOv8-nano. "
        "Returns a list of detected objects with labels, confidence scores, "
        "and bounding box coordinates. "
        "Supports 80 COCO classes: person, car, dog, cat, etc."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to the image file on disk",
            },
            "confidence": {
                "type": "number",
                "description": "Minimum confidence threshold (0.0-1.0, default 0.5)",
            },
            "max_objects": {
                "type": "integer",
                "description": "Maximum number of objects to return (default 10)",
            },
        },
        "required": ["path"],
    },
}

# Lazy-loaded model
_model = None


def _get_model():
    """Load YOLO model lazily — first call downloads ~6MB."""
    global _model
    if _model is None:
        try:
            from ultralytics import YOLO
            _model = YOLO("yolov8n.pt")
            logger.info("YOLOv8-nano loaded successfully")
        except ImportError:
            logger.warning("ultralytics not installed, trying onnxruntime...")
            return None
        except Exception as e:
            logger.warning("YOLO model load failed: %s", e)
            return None
    return _model


def _handle_yolo_detect(args: dict[str, Any]) -> str:
    """Detect objects in an image using YOLOv8."""
    path = args.get("path", "")
    confidence = float(args.get("confidence", CONFIDENCE_DEFAULT) or CONFIDENCE_DEFAULT)
    max_objects = int(args.get("max_objects", MAX_OBJECTS_DEFAULT) or MAX_OBJECTS_DEFAULT)

    confidence = max(0.1, min(confidence, 1.0))
    max_objects = max(1, min(max_objects, 50))

    if not path.strip():
        return tool_error("image path is required")

    filepath = Path(path)
    if not filepath.exists():
        return tool_error(f"image not found: {path}")

    ext = filepath.suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
        return tool_error(f"unsupported image type: {ext}")

    model = _get_model()
    if model is None:
        return tool_error(
            "YOLO model not available. Install with: pip install ultralytics"
        )

    try:
        results = model(str(filepath), conf=confidence, verbose=False)
    except Exception as e:
        return tool_error(f"YOLO inference failed: {e}")

    detections = []
    if results and len(results) > 0:
        result = results[0]
        boxes = result.boxes
        if boxes is not None:
            for i, box in enumerate(boxes):
                if i >= max_objects:
                    break
                cls_id = int(box.cls[0].item())
                conf = float(box.conf[0].item())
                xyxy = box.xyxy[0].tolist()
                label = result.names.get(cls_id, f"class_{cls_id}")
                detections.append({
                    "label": label,
                    "confidence": round(conf, 3),
                    "bbox": {
                        "x1": round(xyxy[0], 1),
                        "y1": round(xyxy[1], 1),
                        "x2": round(xyxy[2], 1),
                        "y2": round(xyxy[3], 1),
                    },
                })

    count = len(detections)
    logger.info("yolo_detect: %s → %d objects", filepath.name, count)

    if not detections:
        return tool_result({"objects": [], "message": "No objects detected above confidence threshold"})

    return tool_result({
        "objects": detections,
        "count": count,
        "model": "yolov8n",
    })


# ── Self-register ────────────────────────────────────────────────────

registry.register(
    name="yolo_detect",
    toolset="vision",
    schema=YOLO_DETECT_SCHEMA,
    handler=_handle_yolo_detect,
    is_async=False,
    description="Detect objects in images using YOLOv8-nano (80 COCO classes)",
    emoji=DETECT_EMOJI,
    max_result_size_chars=5000,
)
