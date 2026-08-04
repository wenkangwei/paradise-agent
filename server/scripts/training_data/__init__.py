"""
Training Data Pipeline

Converts AiChat conversation logs into LLM fine-tuning training data.

Quick start:
    python -m scripts.training_data.pipeline                     # basic conversion
    python -m scripts.training_data.pipeline --augment           # + LLM augmentation
    python -m scripts.training_data.pipeline --deploy            # + deployment artifacts

Data flow:
    logs/{user}/sessions/{sid}/{sub_id}/{upload_id}.jsonl  ─┐
    logs/{user}/feedback/{sid}/{date}/behavior_{time}.jsonl ─┤
                                                               ├─ loader.join() ─┐
                                                               │                  │
    labelled sessions ─────────────────────────────────────────┘                  │
                                                                                  │
    converter: ─→ sft_train.jsonl    SFT (messages format)                       │
               ─→ dpo_train.jsonl    DPO (prompt/chosen/rejected)                │
               ─→ grpo_train.jsonl   GRPO (prompt only)                          │
                                                                                  │
    augmenter:  ─→ sft_train_augmented.jsonl   (optional: LLM rewrites)          │
                                                                                  │
    deploy:     ─→ Modelfile.aichat             (Ollama)                          │
               ─→ vllm_config.json             (vLLM)                            │

Reference projects:
    ~/workspace/ai_project/LLMTrainPipeline/   Fine-tuning notebooks (SFT/DPO/ORPO/GRPO)
    ~/workspace/ai_project/ollama_baseline/    Ollama deployment + GGUF conversion
"""
