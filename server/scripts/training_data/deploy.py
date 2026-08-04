"""
Deployment — generates Ollama Modelfile and vLLM config for trained adapters.

Reference:
  ~/workspace/ai_project/ollama_baseline/  (Ollama deployment)
  ~/workspace/ai_project/LLMTrainPipeline/ (training, but vLLM not used there)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

# Config passed via function parameters

logger = logging.getLogger("training_data.deploy")


def generate_modelfile(deploy_cfg: dict, adapter_path: Path | None = None) -> Path:
    """Generate an Ollama Modelfile for the fine-tuned LoRA adapter.

    Reference: ~/workspace/ai_project/ollama_baseline/deployment/Modelfile.huanhuan
    """
    deploy_dir = Path(deploy_cfg.get("deploy_dir", "deploy"))
    deploy_dir.mkdir(parents=True, exist_ok=True)
    modelfile = deploy_dir / "Modelfile.aichat"
    base_model = deploy_cfg.get("ollama_base_model", "qwen2.5:0.5b")
    model_name = deploy_cfg.get("ollama_model_name", "aichat-sft")

    adapter_line = ""
    if adapter_path and adapter_path.exists():
        # Convert HF adapter → GGUF using llama.cpp
        adapter_line = f'ADAPTER {adapter_path.resolve()}/adapter_model.safetensors'

    content = f'''# Auto-generated Modelfile for AiChat fine-tuned model
# Build: ollama create {model_name} -f {modelfile.name}
FROM {base_model}
{adapter_line}
PARAMETER temperature 0.7
PARAMETER top_p 0.9
PARAMETER top_k 40
PARAMETER num_ctx 4096

TEMPLATE """{{- range .Messages }}
{{- if eq .Role "system" }}<|im_start|>system
{{ .Content }}<|im_end|>
{{- else if eq .Role "user" }}<|im_start|>user
{{ .Content }}<|im_end|>
{{- else if eq .Role "assistant" }}<|im_start|>assistant
{{ .Content }}<|im_end|>
{{- end }}
{{- end }}
{{- if .AddSystem }}<|im_start|>assistant
{{ .System }}<|im_end|>
{{- end }}"""
'''

    modelfile.write_text(content, encoding="utf-8")
    logger.info("Modelfile written: %s (base=%s, model=%s)",
                modelfile, base_model, model_name)
    return modelfile


def generate_vllm_config(deploy_cfg: dict, output_dir: str) -> Path:
    """Generate a vLLM serving configuration."""
    deploy_dir = Path(deploy_cfg.get("deploy_dir", "deploy"))
    deploy_dir.mkdir(parents=True, exist_ok=True)
    config_path = deploy_dir / "vllm_config.json"
    gpu_mem = deploy_cfg.get("vllm_gpu_memory", 0.9)
    max_len = deploy_cfg.get("vllm_max_model_len", 4096)

    # Detect base model from training outputs
    base_model = "Qwen/Qwen2.5-0.5B-Instruct"
    sft_train = Path(output_dir) / "sft_train.jsonl"
    if sft_train.exists():
        with open(sft_train, "r", encoding="utf-8") as f:
            first = json.loads(f.readline().strip())
            msgs = first.get("messages", [])
            # Heuristic: count total characters → scale model size
            total_chars = sum(len(m.get("content", "")) for m in msgs)
            if total_chars < 1000:
                base_model = "Qwen/Qwen2.5-0.5B-Instruct"
            else:
                base_model = "Qwen/Qwen2.5-1.5B-Instruct"

    config = {
        "model": base_model,
        "max_model_len": max_len,
        "gpu_memory_utilization": gpu_mem,
        "dtype": "auto",
        "trust_remote_code": True,
        "enable_lora": adapter_path is not None,
        "lora_modules": [
            {"name": "aichat-sft", "path": str(adapter_path.resolve())}
        ] if adapter_path else [],
        "chat_template": "chatml",
        "port": 8000,
    }

    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("vLLM config written: %s", config_path)
    return config_path


def generate_run_script(deploy_cfg: dict) -> Path:
    """Generate a convenience script for running the training → deploy pipeline."""
    deploy_dir = Path(deploy_cfg.get("deploy_dir", "deploy"))
    script_path = deploy_dir / "run.sh"
    model_name = deploy_cfg.get("ollama_model_name", "aichat-sft")
    script = f'''#!/bin/bash
# AiChat Training Pipeline — Run Script
# Usage: bash {script_path.name}

set -euo pipefail

echo "=== Step 1: Generate training data ==="
cd "$(dirname "$0")/.."
python -m scripts.training_data.pipeline --deploy

echo ""
echo "=== Step 2: Train (SFT) ==="
echo "Copy sft_train.jsonl and sft_val.jsonl to LLMTrainPipeline/data/"
echo "Then run: jupyter nbconvert --to notebook --execute 02_training_pipeline.ipynb"
echo ""
echo "=== Step 3: Deploy ==="
echo "Ollama:  ollama create {model_name} -f {deploy_dir / 'Modelfile.aichat'}"
echo "vLLM:    vllm serve Qwen/Qwen2.5-0.5B-Instruct --config {deploy_dir / 'vllm_config.json'}"
'''
'''
    script_path.write_text(script, encoding="utf-8")
    script_path.chmod(0o755)
    logger.info("Run script written: %s", script_path)
    return script_path
