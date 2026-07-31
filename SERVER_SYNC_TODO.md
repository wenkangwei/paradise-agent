# AiChat Agent Server — 开发待办与架构路线图

> 2026-07-31 整理，分支 `feat/v4.3-model-router` commit `fbfb4d6`

---

## P0 — 当前紧急

### 1. 搜索功能深度优化
**现状**：Bing 搜索 + query rewriting + web_fetch + RAG 摘要已可用，但质量差
- [ ] 搜索准确率低 — 需要更好的内容提取（readability 算法）、死链处理、重试
- [ ] 搜索量少 — 目前去重后 3-5 条，应扩展到多源搜索（Bing + Brave + SearXNG 合并）
- [ ] **用户兴趣话题挖掘与本地缓存** — 自动从对话中提取用户兴趣标签，建立本地知识库。搜索时优先检索本地缓存话题内容，减少外部搜索延迟
- [ ] Query rewriting 需要结合更多上下文（对话主题、用户画像、历史搜索词）

### 2. TTS/STT 接入
**现状**：Whisper 服务端 STT + Vosk 离线兜底工作，Android 内置 TTS 可用
- [ ] FastSpeech2 / Piper 作为备选 TTS 引擎（比系统 TTS 质量好）

---

## P1 — 核心架构升级

### 3. Proactive Agent 框架
**现状**：agent 仅有被动响应能力（用户发消息 → agent 回复）
- [ ] Agent 主动推送 — 基于心跳机制 + 用户兴趣话题，agent 自动发起对话
- [ ] 推送通知 — Android `NotificationService` + FCM/SSE 反向通道
- [ ] 触发策略：定时（cron）、用户画像变化、外部事件（新闻/天气/股价）
- [ ] 参考：hermes heartbeat 机制（GATHER→THINK→ACT）

### 4. Agent Planning + Multi-Agent + 异步训练 架构

**核心定位**：这是一个**完全本地可运行、可本地训练的 agent 框架**。
不用依赖云端 API，本地 LLM(Qwen) + LoRA 微调实现持续学习和个性化。

**架构总览**：
```
用户输入
  ↓
[Planning Skill] ← 仅复杂/抽象/模糊任务触发
  │  拆分任务 → 子任务序列 + 每个子任务的验收标准
  ↓
[LLM Router] ← 按子任务难度选模型
  │  简单 → qwen2.5:3b | 搜索/工具 → qwen2.5:7b-instruct | 复杂推理 → API
  ↓
[Agent Team] ← 多个子 agent 并行处理
  │
  ├─ Top-Down (分发):
  │   Parent Agent 把 plan + 上下文 + 任务细节 + 输入输出要求 + 校验标准
  │   写成文档/内存变量 → 分发给各 Sub-Agent
  │   通信方式: 文件 + 内存变量 (不通过 LLM context 传递，节省 token)
  │
  ├─ Sub-Agent 执行:
  │   每个 Sub-Agent 独立运行 (独立 context, 独立工具)
  │   读取父 agent 分发的任务文档 → 执行 → 写入结果文件
  │
  └─ Bottom-Up (聚合 + 校验):
       Sub-Agent 返回结果路径 → 校验函数打分评估 (0-1 score)
       → 失败: 记录失败原因 + 重试或降级
       → 成功: 通过文档/内存变量递归向上层返回摘要
       → 递归到根节点时: 只传递结构化摘要(不传全文，节省上下文窗口)
       → 所有操作(成功/失败) + label + score 记录为训练样本
```

**4a. Planning Skill**
- [ ] 注册为 paradise tool: `plan_task(goal, context) → [{subtask, requirements, validation_criteria}]`
- [ ] 仅当用户输入复杂/模糊/多步骤时自动触发
- [ ] 输出包含: 子任务描述、输入输出规范、通过标准、预估难度
- [ ] 简单任务直接跳过 planning

**4b. Sub-Agent 通信机制**
- [ ] **文件通信**: Parent → 写入 `workspace/{task_id}/subtasks/{id}.json` → SubAgent 读取
- [ ] **内存变量**: 小数据(score, status, summary)通过内存传递，大数据(完整结果)存文件
- [ ] **上下文窗口保护**: 向上层返回时只传摘要，原始数据保留在文件中按需读取
- [ ] SubAgent 类型: search_agent, code_agent, vision_agent, data_agent, chat_agent

**4c. 校验与反馈系统**
- [ ] 每个子任务定义 `validation_criteria`
- [ ] 校验函数对结果打分 (0-1): 相关性、完整性、准确性
- [ ] 失败任务: 记录 (input, output, score, error_type) → 重试或降级到更强大模型
- [ ] 成功/失败样本持久化到 `workspace/training_samples/`

**4d. 异步本地训练 (LoRA)**
- [ ] 收集用户历史行为: 点赞/点踩、修改回复、追问模式
- [ ] 用户空闲时(GPU 空闲检测)自动触发 LoRA 微调
- [ ] 使用 qwen2.5 + LoRA，单卡 8GB VRAM 可训练 7B 模型
- [ ] 训练目标: 个性化回复风格、领域知识记忆、工具调用偏好
- [ ] 训练数据: 对话日志 + 校验反馈 + 用户行为 label
- [ ] 模型版本管理: 保留基座模型，LoRA adapter 独立存储，支持回滚

**4e. LLM Router**
- [ ] 模型池: 本地 (qwen2.5:0.5b / 3b / 7b / 7b-instruct) + 云端 (OpenAI/Claude 可选)
- [ ] 路由规则: task_difficulty(0-1) → 选模型，cost_budget → 降级策略
- [ ] Token 预算管理: 日/月额度，超预算自动降级
- [ ] 配置: `agent_config.json` → `llm_router` 段

### 6. 上下文树结构优化
**现状**：线性 messages 列表
- [ ] **Top-Down 树结构**：
  ```
  用户输入 → [Planning] → 子任务1 → sub-agent → 工具调用 → 结果
                        → 子任务2 → sub-agent → 工具调用 → 结果
                        → 子任务3 → sub-agent → 直接回答
  ```
- [ ] **Bottom-Up 聚合**：子 agent 返回结果文件路径 → parent agent 读取合并
- [ ] 存储：内存 + 磁盘文件混合，文件路径记录在树节点中
- [ ] 每个树节点：{id, type, status, result_path, children}

### 7. 知识图谱 Memory 存储
**现状**：对话历史线性存储，无结构化记忆
- [ ] 长对话解析 → 实体抽取 → 关系构建 → 知识图谱
- [ ] 存储优化：ID 树结构 + ID-实体 token 映射（节省存储空间）
- [ ] 支持多图结构：用户画像图、领域知识图、对话关系图
- [ ] Memory 随对话增量更新，支持查询和回溯

---

## P2 — 多模态与端侧能力

### 8. 多模态能力接口
**现状**：vision_analyze（图片）已可用，缺少其他模态
- [ ] 视频理解 — frame extraction + vision model
- [ ] 音频理解 — 除了 STT，加音频分类/情感识别
- [ ] 文档理解 — PDF/DOCX/PPT 的布局感知解析（表格、图表）
- [ ] 统一的多模态接口：`multimodal_analyze(type, path) → structured result`

### 9. Proactive Agent 视觉交互
- [ ] **角色脸部动画** — 2D Live2D 或简单的表情动画（Android Compose Canvas）
- [ ] **摄像头实时 OCR** — 相机预览 + ML Kit 实时文字识别
- [ ] **实时物体检测** — 相机预览 + YOLO 实时检测
- [ ] **VLM 主动理解** — 摄像头画面定期采样 → vision model 理解 → agent 主动评论
- [ ] **主动消息机制** — agent 检测到有趣画面时主动发送消息给用户

---

## P3 — 产品化

### 10. Android App 页面完善
- [ ] 用户注册/登录页面（账号系统）
- [ ] 个人资料页面 ✅ (UserProfilePage 已完成)
- [ ] 主题切换（深色/浅色/跟随系统）
- [ ] 数据统计页面（对话量、token 消耗、热门话题）
- [ ] 通知设置页面
- [ ] 帮助/反馈页面

### 11. API 网关与多端支持
- [ ] Web 端（React/Vue）复用 server API
- [ ] API 认证（Bearer token + API key）
- [ ] 速率限制

---

## 反向同步

android-app 这边的 agent 框架开发调试成熟后，以下需要同步回 aipet-social：

| 文件/目录 | aipet-social 路径 | aipet-social commit |
|---|---|---|
| `server/paradise/` | `src/backend/paradise/` | `a8063ea` |
| `server/api/routes/openai_proxy.py` | `src/backend/api/routes/openai_proxy.py` | `b3ef59c` |

## 不迁移的部分

- `aipet-social/src/backend/database.py` / `models/` / `services/` — aipet 的 DB 层
- `aipet-social/src/backend/api/routes/chat.py` / `agent.py` / `session.py` — aipet 业务路由
- `aipet-social/src/frontend/` — React 前端，aipet 专属
