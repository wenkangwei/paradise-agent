# Server 模块与 aipet-social 同步待办

## 来源

`server/` 目录从 `/home/wwk/workspace/ai_project/aipet-social/` 迁移而来：

| 文件/目录 | aipet-social 路径 | aipet-social commit |
|---|---|---|
| `server/paradise/` | `src/backend/paradise/` | `a8063ea` feat: Paradise Agent Framework |
| `server/api/routes/openai_proxy.py` | `src/backend/api/routes/openai_proxy.py` | `b3ef59c` feat(openai_proxy) |

## 反向同步 checklist

android-app 这边的 agent 框架开发调试成熟后，以下需要同步回 aipet-social：

- [ ] `server/paradise/` 所有修改 — diff 后合入 `aipet-social/src/backend/paradise/`
- [ ] `server/api/routes/openai_proxy.py` 修改 — diff 后合入 `aipet-social/src/backend/api/routes/openai_proxy.py`
- [ ] `server/main.py` — aipet-social 的 main.py 包含 DB/agent runtime/frontend static，不需要同步，但新路由可能需要手动合入
- [ ] 新增的依赖写入 `aipet-social/src/backend/requirements.txt`
- [ ] 同步后跑 aipet-social 的测试确保不回归

## 不迁移的部分

- `aipet-social/src/backend/database.py` / `models/` / `services/` — aipet 的 DB 层，android-app 不需要
- `aipet-social/src/backend/api/routes/chat.py` / `agent.py` / `session.py` — aipet 业务路由
- `aipet-social/src/frontend/` — React 前端，aipet 专属
- `aipet-social/start_backend.sh` — aipet 的启动脚本（双 PYTHONPATH），android-app 有自己的 `server/start.sh`

## 开发待办

### P0 — Agent 功能增强
- [ ] **Web Search 工具** — paradise 新增 `web_search` tool，支持联网搜索
  - 工具注册到 `paradise/tools/builtin.py`
  - 搜索结果格式化后在 Android app 的 `ToolCard` 中展示
  - Android 端 `ToolCardRecognizer` 需识别 `search_results` 类型
  - 搜索结果卡片包含：标题、摘要、URL、相关性分数
  - 搜索来源：优先使用免费 API（DuckDuckGo / SearXNG），后续可接付费搜索 API

### P1 — 体验优化
- [ ] 多模态附件透传优化 — 大文件 base64 用 multipart/form-data 上传替代 JSON 嵌入
- [ ] 文件附件 read_tool 模式 — 非图片文件用 `read_file` tool 读取内容注入 context
- [ ] 训练数据质量过滤 — 去重、过滤过短/低质量对话、工具调用序列化
