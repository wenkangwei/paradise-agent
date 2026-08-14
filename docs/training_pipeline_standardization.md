# 训练流水线标准化对照分析

> 参照 ATIF (Agent Trajectory Interchange Format) / 4-Stage Agent Training Pipeline
> 框架（基建 → 数据 → SFT → RL → 部署），盘点现有资产、差距与迁移路线。
>
> **结论先行**：现有 pipeline 的 SFT/DPO/GRPO 三格式和 5 阶段编排已经能跑，
> 离"标准"差 3 个硬伤 ——
> **(1) tool-call 轨迹被静默丢弃**（致命，无法训 agent）；
> **(2) 零评估基准**（无法度量进步）；
> **(3) 训练交接纯手工 cp**（无版本、无 lineage）。

---

## 0. 现状速查表（4-Stage 对照）

| Stage | Kimi 参考 | 现有实现 | 状态 |
|---|---|---|---|
| 0 基建 | ATIF + Docker tool-server + eval harness | 自定义 JSONL，无 tool-server 容器化 | 🟡 部分 |
| 1 数据 | ShareGPT 兼容 + 5 层过滤 + DVC | loader→join→convert→augment→deploy | 🟡 部分 |
| 2 SFT | LLaMA-Factory + DeepSpeed ZeRO-3 | LLMTrainPipeline nb02 (Qwen2.5 LoRA) | 🟢 已有 |
| 3 RL | verl-agent + GRPO/GiGPO + 课程学习 | nb08 GRPO（单机自写） → 走 `trl.GRPOTrainer` | 🟡 部分 |
| 4 部署 | vLLM/SGLang + 安全 eval + 在线 RL | deploy.py 生成 Modelfile + 手工 cp | 🔴 缺口大 |
| **评估** | BFCL/τ-bench/SWE-bench | **rule-based + LLM-as-judge（双轨）** | 🔴 待建 |

---

## 1. 数据格式：当前 vs ATIF/ShareGPT

### 1.1 当前 SFT 输出（`converter.py:write_sft`）

```json
{
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "source": "aichat",
  "session_id": "...",
  "label": "like|dislike|retry|unmarked"
}
```

**问题**：纯聊天格式，**完全没有 tool / trajectory 字段**。`turn_logger.py:156`
显式 `if self._tool_calls_count > 0: return None` —— 一旦有 tool-call，整条
turn 不进 dataset。这意味着 paradise agent loop 产生的所有 trajectory **都没
被采集**，目前只能用来训闲聊模型。

### 1.2 ATIF / ShareGPT 标准目标格式

```json
{
  "conversation_id": "...",
  "turn_id": 0,
  "messages": [
    {"role": "system", "content": "...", "tools": [...]},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "...",
     "tool_calls": [{"id": "call_1", "name": "search", "arguments": {...}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "..."},
    {"role": "assistant", "content": "final answer"}
  ],
  "trajectory": [
    {"action": "tool_call", "name": "search", "result": "...", "reward": 0.0}
  ],
  "metadata": {
    "duration_ms": 11045,
    "total_tokens": 24,
    "tools_used": ["search"],
    "has_attachments": false,
    "environment": "aichat-prod",
    "schema_version": "atif-1.0"
  },
  "labels": {"thumb": "like", "retry": false, "score": 3}
}
```

### 1.3 Gap 项

| 字段 | 当前 | 目标 | 迁移动作 |
|---|---|---|---|
| `messages[].tool_calls` | ❌ 被丢弃 | ✅ 完整保留 | 删 `turn_logger.py:156` 的 early return；tool 结果以 `role=tool` 入 messages |
| `messages[].role=tool` | ❌ 不存在 | ✅ 必须有 | `turn_logger.py` 新增 tool result 序列化分支 |
| `trajectory[]` | ❌ 无 | ✅ action/reward 序列 | paradise loop 输出附 `trajectory` 字段 |
| `metadata.schema_version` | ❌ 无 | ✅ `atif-1.0` | 所有 exporter 加 `schema_version` |
| `metadata.environment` | ❌ 无 | ✅ `aichat-prod/dev` | 从 env var 注入 |
| `labels.score` | 🟡 用 `like/dislike` 字符串 | ✅ 数字 score | 已有评分逻辑（converter.py:30-36: 0-4 分），统一写入 labels |

---

## 2. 数据处理：5 层过滤对照

### 2.1 当前处理流程（`pipeline.py`）

```
load → join(sessions+behaviors) → convert(SFT/DPO/GRPO) → augment(可选) → deploy(可选)
```

过滤仅 `loader.py` 里检查"非空 + role 合法"，无质控层。

### 2.2 Kimi 参考 5 层过滤

1. **格式校验** — JSON schema 合法
2. **长度截断** — 超长 turn 拒收 / 截断
3. **毒性 / 安全** — perspective API / 本地敏感词
4. **重复 / 近似** — MinHash 去重
5. **轮次质量** — 单轮回复过短 / 全是 emoji / 拒答模板

### 2.3 迁移建议（按 ROI 排序）

| 层 | 当前状态 | 实施成本 | 价值 |
|---|---|---|---|
| 1 格式 | 部分（loader.py 有基础校验） | 低 — 加 jsonschema | 中 |
| 2 长度 | ❌ 无 | 低 | 中（防爆 token） |
| 3 毒性 | ❌ 无 | 中 — 接 LLM 敏感词 | 高（合规） |
| 4 去重 | ❌ 无 | 低 — MinHash | 高（提质量） |
| 5 质量 | ❌ 无 | 中 — 规则 + LLM 评分 | 高（最直接提升） |

**实施建议**：新建 `training_data/filters.py`，5 个独立 filter 函数，pipeline.py
里串行调用。先做 1+2+4（成本低），3+5 后置。

---

## 3. 评估指标：**完全空白，最大优先级**

### 3.1 现状

agent 报告里搜过 `benchmark|eval|BFCL|tau-bench` —— **0 hit**。
唯一评估在 LLMTrainPipeline `nb05_eval_sft_vs_base.ipynb`（训练侧的 acc/loss
对比），不是回复质量评估。

### 3.2 双轨评估：Rule-based + LLM-as-Judge

**不走 BFCL/τ-bench 这类重 benchmark** —— 我们是单机训练、陪伴聊天场景，
function-call 准确率和 agent trajectory 成功率不是当前主诉求。先做能覆盖
80% 场景的轻量双轨：

| 轨 | 目的 | 怎么算 | 成本 |
|---|---|---|---|
| **Rule-based** | 客观可量化指标 | 长度 / 重复率 / 拒答模板 / 格式合规 / 敏感词 / 中英文混杂 / 标点 | 极低（纯 Python） |
| **LLM-as-Judge** | 主观质量打分 | 让强模型（Claude / GPT-4 / DeepSeek）按 5 维度 rubric 给 1-5 分 | 中（API 调用） |

**5 维度 rubric**（LLM-as-Judge 用）：
1. **Helpfulness** — 是否回答了用户问题
2. **Coherence** — 中文是否通顺、逻辑连贯
3. **Safety** — 是否有敏感/有害内容
4. **Persona Consistency** — 是否保持"陪伴 AI"人设
5. **Engagement** — 是否促进对话延续（陪伴场景特有）

### 3.3 评估数据集

从生产 dataset 抽样 N=200 条作为 **held-out eval set**：
- 按 `label` 分层抽样（like / dislike / retry / unmarked 各 50 条）
- 写入 `server/evaluation/eval_set.jsonl` 固化（不要每次重抽）
- 这份 eval set 也加 DVC 版本控制

### 3.4 落地位置

新建 `server/evaluation/`：
- `rule_based.py` — 纯规则打分，输出每条 0-1 分 + 违规原因
- `llm_judge.py` — 调强模型 API（兼容 OpenAI 格式，DeepSeek/Claude 都行），
  prompt 里塞 5 维度 rubric，要求返回严格 JSON `{"scores": {...}, "reason": "..."}`
- `compare.py` — 输入两个模型的回复，跑 rule + judge，输出 markdown 对比表：

  ```
  | 维度       | base | sft | Δ    |
  |------------|------|-----|------|
  | length_med | 42   | 68  | +26  |
  | repeat_rt  | 0.12 | 0.05| -0.07|
  | judge.help | 3.2  | 4.1 | +0.9 |
  | judge.cohe | 3.5  | 4.3 | +0.8 |
  ```

### 3.5 LLM-as-Judge 降本

- **缓存**：同一 (prompt, model_a, model_b, question) 组合的 judge 结果写
  SQLite，下次复用（参照 memoryforge 的真向量缓存模式）
- **批量**：judge 调用走 batch API（DeepSeek/Glint 便宜）
- **抽样 judge**：rule-based 全跑，LLM judge 只在 rule 分数差异大的样本上跑

---

## 4. 模型训练流程：现状 + 标准化

### 4.1 现状

LLMTrainPipeline（独立项目）已有：
- nb01 数据处理
- nb02 SFT pipeline ✅ 跑通
- nb03 LoRA 合并
- nb04 Ollama 部署
- nb05 SFT vs base 评估
- nb06 DPO ✅ 训练成功
- nb07 ORPO ✅ 训练成功（acc=0.96 最优）
- nb08 GRPO ⏳ 待推进

**断点**：android-app `deploy.py` 只生成 Modelfile 和提示信息，需要手工 cp
数据到 LLMTrainPipeline，跑完 notebook 再手工 cp 回 Ollama 模型目录。

### 4.2 标准化建议

#### 阶段 2 SFT
- **当前**：LLaMA-Factory 风格的 notebook，未用 DeepSpeed
- **建议**：保留 notebook，但把 hyperparam 抽到 `sft_config.yaml`（已在
  `training_config.yaml` 里有雏形），让训练可复现
- **必加**：SFT 后自动触发 BFCL 评估，<阈值则不发版

#### 阶段 3 RL
- **当前**：nb08 GRPO 待跑（单机）
- **不接 verl**：verl 是为分布式设计的，单机用反而复杂。继续走自写 GRPO 或
  `trl.GRPOTrainer`（HuggingFace 官方，单机成熟）
- **奖励信号**（优先级）：
  1. paradise loop 的 `like/dislike` → verifiable reward（已有数据）
  2. rule-based 评分 → 稠密 reward（免费、即时）
  3. LLM-judge 抽样评分 → 离线稀疏 reward（贵但准）

#### 阶段 4 部署
- **当前**：手工 cp 到 Ollama
- **建议**：写一个 `deploy_and_eval.py`：
  1. 训练完成 → 合并 LoRA → 转 GGUF
  2. Ollama 加载新模型
  3. 自动跑 BFCL + τ-bench
  4. 通过则切 active model；不通过则回滚
  5. 发版本 tag 到 `models/registry.json`（数据 lineage）

---

## 5. Lineage / 版本控制：DVC 接入

### 5.1 现状

数据集文件名 `dataset_2026-08.jsonl` —— **按月切，无版本，无 hash**。
训练完了不知道是哪个数据版本训出来的。

### 5.2 建议接入 DVC

```bash
cd /home/wwk/workspace/ai_project/android-app/server
dvc init
echo "logs/training/" >> .gitignore
echo "logs/training/" >> .dvcignore
dvc add logs/training/dataset_2026-08.jsonl
git add logs/training/dataset_2026-08.jsonl.dvc .gitignore
```

每次 `TrainingExporter` 写完新版本，触发 `dvc add` 生成新 hash。模型训
练时记录 `dataset_dvc_hash` 进 metadata，做到 "训出来的模型 → 当时的数据"。

---

## 6. 优先级路线图（2 周内闭环）

| P | 任务 | 工作量 | 阻塞 |
|---|---|---|---|
| **P0** | 修 `turn_logger.py:156`：保留 tool-call turn（写入 atif schema） | 半天 | 阻塞所有 agent 训练 |
| **P0** | `evaluation/rule_based.py` + `eval_set.jsonl` 200 条抽样 | 半天 | 没度量就没法验证进步 |
| **P0** | `evaluation/llm_judge.py` + SQLite 缓存 | 1 天 | |
| **P0** | `evaluation/compare.py` 跑 base vs sft 对比报告 | 半天 | |
| **P1** | `training_data/filters.py`（1+2+4 层过滤） | 半天 | |
| **P1** | `deploy_and_eval.py` 自动化（训完→rule 评估→对比→发版） | 1 天 | |
| **P2** | DVC 接入数据版本控制 | 半天 | |
| **P2** | trl GRPOTrainer 替换 nb08 自写 GRPO | 2-3 天 | 阻塞 RL 进展 |
| **P3** | MinHash 去重 + LLM 评分过滤 | 1 天 | | |

---

## 7. 立刻可做（不阻塞）

### 7.1 Schema 版本字段（10 分钟改）

所有 `converter.py` 的 `write_*` 函数，输出 JSON 加：

```python
record["schema_version"] = "aichat-1.0"  # 后续升 atif-1.0
record["metadata"] = {
    "environment": os.getenv("APP_ENV", "dev"),
    "exported_at": datetime.utcnow().isoformat() + "Z",
}
```

### 7.2 Tool-call turn 保留（P0 的最小改）

```python
# turn_logger.py 第 156 行附近
# 原:
if self._tool_calls_count > 0:
    return None
# 改:
tool_calls_payload = [...]
return {
    **base_record,
    "messages": self._messages,  # 含 tool_calls / tool role
    "metadata": {**base_record["metadata"], "tools_used": [...]},
}
```

### 7.3 Rule-based + LLM-Judge MVP

```bash
# 1. 抽样 200 条 eval set
python -m server.evaluation.sample_eval_set --n 200 --out eval_set.jsonl

# 2. rule-based 全跑（秒级）
python -m server.evaluation.rule_based --model base --eval-set eval_set.jsonl
python -m server.evaluation.rule_based --model sft  --eval-set eval_set.jsonl

# 3. LLM-judge 跑差异大的子集（缓存命中后续免费）
python -m server.evaluation.llm_judge --model-a base --model-b sft \
    --judge deepseek-chat --rubric 5dim

# 4. 输出对比表
python -m server.evaluation.compare --out report.md
```

---

## 8. 不该做的事

- ❌ **不要现在就全面切 ATIF**：我们已有 SFT/DPO/GRPO 数据，激进换格式会
  断已有训练。增量加 `schema_version` 字段，老数据保持兼容。
- ❌ **不要现在就上 verl**：单机训练，trl.GRPOTrainer 或自写 GRPO 够用；
  verl 是分布式场景的优化，引入复杂度高于收益。
- ❌ **不要接 BFCL/τ-bench**：我们的场景是陪伴聊天，function-call 准确率
  不是核心指标。rule-based + LLM-judge 更贴合实际质量。
- ❌ **不要把评估做成交付物**：评估是开发期工具，每次训练后跑；不要做成
  app 内功能。

---

## 9. 总结：2 周目标

| 时间 | 交付 |
|---|---|
| W1 前半 | turn_logger 保留 tool-call；数据加 schema_version |
| W1 后半 | rule_based + llm_judge + compare 跑通；base vs sft 报告 |
| W2 前半 | filters.py 1+2+4 层；deploy_and_eval.py 自动化 |
| W2 后半 | trl GRPOTrainer 接入；DVC 数据版本控制 |

做完这 2 周，pipeline 从"能跑"升级到"可度量、可追溯、可回滚"。
后续如果数据量上来 / 多机训练，再考虑 verl + BFCL/τ-bench 重 benchmark。
