"""Model Router — detect model capabilities and route accordingly.

Three routing strategies based on model capabilities:

  1. Multimodal VL model (qwen2.5vl, llava, etc.):
     Images → sent directly inline in message content
     Tools  → if instruct, OpenAI function calling; else system-prompt ReAct

  2. Tool-calling model (instruct, function-calling fine-tuned):
     Images → dispatched to vision_analyze tool
     Tools  → OpenAI function calling

  3. Plain model (base qwen2.5, llama, etc.):
     Images → dispatched to vision_analyze tool
     Tools  → system-prompt ReAct (text-based tool parsing)

Also fixes VISION_MODEL default to qwen2.5vl:7b.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── Model capability patterns ──────────────────────────────────────

_VL_PATTERNS = [
    "vl", "vision", "llava", "bakllava", "cogvlm", "fuyu",
    "minicpm-v", "gemini", "gpt-4o", "claude-3", "molmo",
    "pixtral", "llama3.2-vision", "qwen-vl", "internvl",
]

_INSTRUCT_PATTERNS = [
    "instruct", "chat", "function-calling", "tool", "fc",
]


@dataclass
class ModelCap:
    """Detected model capabilities."""
    is_multimodal: bool = False
    has_tool_calling: bool = False
    vision_model: str = "qwen2.5vl:7b"

    @property
    def images_direct(self) -> bool:
        """Images go directly inline (no vision_analyze tool)."""
        return self.is_multimodal

    @property
    def tools_native(self) -> bool:
        """Use OpenAI function calling for tools."""
        return self.has_tool_calling

    @property
    def tools_prompt(self) -> bool:
        """Describe tools in system prompt (text-based ReAct)."""
        return not self.has_tool_calling


def detect_model(model_name: str) -> ModelCap:
    """Detect capabilities from model name.

    Args:
        model_name: e.g. "qwen2.5:7b-instruct", "qwen2.5vl:7b", "llava:7b"

    Returns:
        ModelCap with detected flags
    """
    lower = model_name.lower()

    is_multimodal = any(p in lower for p in _VL_PATTERNS)
    has_tool_calling = any(p in lower for p in _INSTRUCT_PATTERNS)

    # Vision model for tool-based image analysis
    vision_model = os.getenv("VISION_MODEL", "qwen2.5vl:7b")

    return ModelCap(
        is_multimodal=is_multimodal,
        has_tool_calling=has_tool_calling,
        vision_model=vision_model,
    )


# ── Routing logic ───────────────────────────────────────────────────

class ToolRouter:
    """Routes tool calls based on model capabilities.

    - tools_native: use OpenAI function calling → get_builtin_tool_definitions()
    - tools_prompt:  use system-prompt ReAct → _build_tool_prompt()
    """

    @staticmethod
    def needs_vision_tool(model_cap: ModelCap, has_attachments: bool) -> bool:
        """Whether to use vision_analyze tool for image processing.

        Only needed when the main model is NOT multimodal.
        Multimodal models receive images directly inline.
        """
        return has_attachments and not model_cap.is_multimodal


# ── System-prompt ReAct tool format ──────────────────────────────────

TOOL_SYSTEM_PROMPT_REACT = """You have access to the following tools. To use a tool, output:

[TOOL: tool_name]
arg1=value1
arg2=value2
[/TOOL]

You can use multiple tools sequentially. After getting results, respond to the user.

Available tools:
{tool_descriptions}
"""


def build_react_tool_prompt() -> str:
    """Build a system prompt describing all available tools for ReAct parsing.

    Use this when the model doesn't support native OpenAI function calling.
    """
    from paradise.tools.registry import registry

    tool_descs = []
    for name in sorted(registry.get_all_tool_names()):
        entry = registry.get_entry(name)
        if not entry:
            continue
        schema = entry.schema
        desc = schema.get("description", "")
        params = schema.get("parameters", {}).get("properties", {})
        param_strs = []
        for pname, pinfo in params.items():
            required = pname in schema.get("parameters", {}).get("required", [])
            req = " (required)" if required else ""
            param_strs.append(f"    {pname}: {pinfo.get('description', '')}{req}")

        tool_descs.append(f"### {name}\n{desc}\nParameters:\n" + "\n".join(param_strs))

    return TOOL_SYSTEM_PROMPT_REACT.format(tool_descriptions="\n\n".join(tool_descs))
