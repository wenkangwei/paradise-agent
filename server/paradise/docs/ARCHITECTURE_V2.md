# Paradise Agent Framework — Architecture V2

> **版本**：v2（2026-08-13）
> **状态**：设计已拍板，待实施（Phase 2.6 → 2.13 + 6.1 → 6.6）
> **范围**：生产级 agent 编排架构（supervisor + subgraph + handoff + 监控）
> **正交**：与三大改造（Trace→Skill / 文件记忆 / 折叠上下文）独立，分开推进

---

## 1. 设计动机

**业务约束**：DAU < 1k，单机容器化，不上 K8s / vLLM / Kafka。

**架构目标**：
- 把现有 4 阶段线性循环升级为 **supervisor + subgraph** 二级结构
- 提供 **规范化的扩展点**（Handoff Protocol + Subgraph Registry + Pattern ABC）
- 保留 **接口预留**（7 个 multi-agent pattern stub + ATIF 训练开关）
- **实时可观测**（5 层监控栈）

**非目标**（明确划线）：
- ❌ 不实现 7 个 multi-agent pattern 的具体逻辑（仅 stub）
- ❌ 不做 dynamic plugin loading（启动时静态注册）
- ❌ 不做 ATIF 训练流水线（只产出 JSONL，训练走 W4 现有 SFT 管道）
- ❌ 不上 vLLM / K8s / Kafka / Elastic Stack

---

## 2. 整体架构

```
User Request
    ↓
┌─────────────────────────────────────────────┐
│  Supervisor Agent (depth=0)                 │
│  ┌──────────────────────────────────────┐   │
│  │ INTENT  = gather(intent, mode_judge) │   │
│  │ ROUTER  = registry.get(mode)         │   │
│  │ HANDOFF = invoke subgraph ────────────────┐
│  │ REFLECT = retry? accept?             │   │ │
│  │ CLEANUP = persist + atif + release   │   │ │
│  └──────────────────────────────────────┘   │ │
└─────────────────────────────────────────────┘ │
                                                  ▼
       ┌─────────────────────────────────────────────────┐
       │ Subgraph (depth=1)                              │
       │  ─ chat / tool_react / rag / plan_execute       │
       │  ─ + 7 multi-agent stub patterns                │
       │  ─ independent StateSchema + Checkpointer       │
       │  ─ may spawn nested subgraph (depth=2, MAX)     │
       │  ─ returns via HandoffResponse                  │
       └─────────────────────────────────────────────────┘
```

**关键约束**：子图 **不共享 supervisor state**，只通过 Handoff payload 通信。

---

## 3. SubgraphPattern ABC（11 个 pattern 的统一契约）

```python
# paradise/core/patterns/base.py
class SubgraphPattern(ABC):
    name: str                    # 注册键
    description: str             # 给 mode_judge LLM 的 prompt 描述
    category: str                # "basic" | "multi_agent"
    cost_budget_usd: float
    max_turns: int

    @abstractmethod
    def build(self, registry: "SubgraphRegistry") -> CompiledGraph:
        """构造子图。multi-agent pattern 可以引用基础 mode 作为内层 agent。"""

    def availability(self) -> bool:
        """是否可用。stub 返回 False，registry 在路由时自动 fallback。"""
        return True
```

### 3.1 基础 pattern（4 个，真实实现）

| Pattern | Category | 触发条件 | 执行体 |
|---|---|---|---|
| `chat` | basic | chitchat / 闲聊 / 简单 QA | 单次 LLM call |
| `tool_react` | basic | 工具意图 / 用户请求操作 | ReAct loop（max 3 轮） |
| `rag` | basic | 知识查询 / "文档/资料/手册" | 检索 + LLM 总结 |
| `plan_execute` | basic | 多步任务 / "先 X 再 Y" | Planner → 逐步执行 |

### 3.2 Multi-agent pattern（7 个，stub 预留）

| Pattern | Category | 拓扑骨架 | 状态 |
|---|---|---|---|
| `map_reduce` | multi_agent | fan-out N worker → aggregator | stub |
| `agent_team` | multi_agent | role-based agents + coordinator | stub |
| `chain_of_expert` | multi_agent | 串行专家链 | stub |
| `guardrail` | multi_agent | executor + critic 双 agent | stub |
| `hitl` | multi_agent | agent + human approval gate | stub |
| `debate` | multi_agent | N debater + judge | stub |
| `reflection` | multi_agent | generator + self-critic loop | stub |

### 3.3 Stub 实现策略

```python
# paradise/core/patterns/stub.py
class StubPattern(SubgraphPattern):
    """预留接口。availability()=False，路由器自动 fallback 到 chat。"""

    def __init__(self, name, description, fallback="chat"):
        self.name = name
        self.description = description
        self.category = "multi_agent"
        self.fallback = fallback

    def availability(self) -> bool:
        return False

    def build(self, registry):
        chat = registry.get(self.fallback)
        return chat.compiled

    def describe(self):
        return f"{self.description}（接口预留，未启用）"
```

`mode_judge` LLM 看到的 mode 列表自动过滤 `availability()=False` 的项，stub 不会被选到。

---

## 4. Handoff Protocol

```python
# paradise/core/handoff.py
@dataclass
class HandoffRequest:
    session_id: str
    user_id: str
    trace_id: str
    user_tier: str
    message: str
    context: dict              # 最小载荷：facts / last_output / artifacts
    depth: int
    parent_trace_id: str | None

@dataclass
class HandoffResponse:
    session_id: str
    trace_id: str
    output: str                # 主输出文本
    artifacts: dict            # 结构化结果（tool_results / plan_steps / ...）
    cost_incurred_usd: float
    turns_used: int
    status: str                # ok / cost_exceeded / max_turns / depth_capped / error
    error: str | None = None
```

**契约**：子图 entry node 接 `HandoffRequest`，terminal node 产 `HandoffResponse`。supervisor 不读子图内部 state。

**好处**：未来任意子图可替换/重写（如把 `tool_react` 从 LangGraph 改写成裸 asyncio loop），supervisor 端零修改。

---

## 5. Subgraph Registry

```python
# paradise/core/registry.py
@dataclass
class SubgraphSpec:
    name: str
    state_schema: type
    compiled: CompiledGraph    # 预编译 LangGraph
    cost_budget_usd: float
    max_turns: int
    description: str

class SubgraphRegistry:
    def register(self, spec: SubgraphSpec) -> None
    def get(self, name: str) -> SubgraphSpec
    def list_modes(self) -> list[str]
    def build_mode_prompt(self) -> str   # 自动从 description 聚合，喂给 mode_judge
```

**注册时声明**（启动时静态 populate）：

```python
# paradise/core/patterns/__init__.py
def register_all(registry: SubgraphRegistry):
    # 基础 4 个（真实实现）
    registry.register(ChatPattern())
    registry.register(ToolReactPattern())
    registry.register(RagPattern())
    registry.register(PlanExecutePattern())

    # Multi-agent 7 个（stub，availability=False）
    registry.register(StubPattern("map_reduce",      "并行扇出-汇总"))
    registry.register(StubPattern("agent_team",      "多角色协作"))
    registry.register(StubPattern("chain_of_expert", "专家链式咨询"))
    registry.register(StubPattern("guardrail",       "执行+批评双 agent"))
    registry.register(StubPattern("hitl",            "人工审批门"))
    registry.register(StubPattern("debate",          "多 agent 辩论"))
    registry.register(StubPattern("reflection",      "生成+自我批评"))
```

---

## 6. Supervisor Graph 拓扑

```python
# paradise/core/graph.py
"""
INTENT → ROUTE → HANDOFF → REFLECT → CLEANUP → END
         (conditional)
"""

async def intent_node(state):
    """并行运行 intent cascade + mode_judge"""
    msg = state["user_message"]
    intent_res, mode_res = await asyncio.gather(
        classifier.classify(msg),
        mode_judge.classify(msg),       # qwen2.5:0.5b, ~300ms
        return_exceptions=True,
    )
    mode = _fuse(intent_res, mode_res)  # mode_judge conf > 0.7 优先
    return {"intent": ..., "mode": mode, "events": [...]}

async def handoff_node(state):
    spec = registry.get(state["mode"])

    # 深度硬上限
    if state.get("depth", 0) >= MAX_DEPTH:    # MAX_DEPTH = 2
        return {"handoff_response": HandoffResponse(
            ..., status="depth_capped", output="达到嵌套上限，回退 chat"
        )}

    req = HandoffRequest(
        session_id=state["session_id"],
        trace_id=state["trace_id"],
        message=state["user_message"],
        context=_extract_minimal(state),
        depth=state.get("depth", 0) + 1,
        ...
    )

    sub_state = spec.state_schema(handoff=req)
    sub_config = {"configurable": {"thread_id": f"{req.trace_id}:{spec.name}"}}

    final = await spec.compiled.ainvoke(sub_state, config=sub_config)
    return {"handoff_response": final["handoff_response"]}

async def cleanup_node(state):
    """替换 reflect_node：reflection + cache + atif + 释放子 agent"""
    await agent._reflection_phase(ctx)
    await semantic_cache.writeback(state)

    if atif_exporter.enabled:
        await atif_exporter.export({...})    # fire-and-forget

    release_sub_agents(state)
    return {}
```

**深度上限 N=2** 的理由：
- N=1 等于无嵌套，没用
- N=2 覆盖 90% 场景（plan_execute 拆出几个 tool_react 子任务）
- N≥3 在 DAU 1k 规模下成本/延迟失控

---

## 7. ATIF 训练导出（开关）

ATIF = Agent Trace & Instruction Format。**默认关闭，需要时翻 flag 即可启用**。

### 7.1 配置

```python
# paradise/config.py
@dataclass
class AtifConfig:
    enabled: bool = False              # 默认关
    export_path: str = "data/atif"
    include_handoff: bool = True
    include_cost: bool = True
    redact_pii: bool = True
```

```yaml
# config.prod.yaml
atif:
  enabled: false                      # 开关：默认关
  export_path: data/atif
  include_handoff: true
  include_cost: true
  redact_pii: true
```

### 7.2 ATIF JSONL Schema

```json
{
  "session_id": "...",
  "trace_id": "...",
  "timestamp": "2026-08-13T12:34:56Z",
  "user_message": "...",
  "intent": {"label": "tool_search", "confidence": 0.92, "source": "rule"},
  "mode": "tool_react",
  "handoff_chain": [
    {"depth": 1, "mode": "tool_react", "request": {...}, "response": {...}}
  ],
  "turns": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "...", "tool_calls": [...]}
  ],
  "cost_usd": 0.0123,
  "latency_ms": 3400,
  "feedback": null
}
```

### 7.3 Exporter

```python
# paradise/observability/atif_exporter.py
class AtifExporter:
    def __init__(self, config: AtifConfig):
        self._cfg = config
        self._sink: asyncio.Queue | None = None

    async def export(self, turn_trace: dict) -> None:
        """Fire-and-forget 写盘。cleanup_node 调用。"""
        if not self._cfg.enabled:
            return
        if self._sink is None:
            self._sink = asyncio.Queue()
            asyncio.create_task(self._writer_loop())
        await self._sink.put(self._format(turn_trace))
```

**关时零开销**：`enabled=false` 时 `export()` 直接 return。

---

## 8. 实时监控栈（5 层）

| 层 | 关注点 | 技术 | 实时性 |
|---|---|---|---|
| **L1 基础设施** | 容器/主机 CPU/内存/磁盘 | cAdvisor + Node Exporter → Prometheus | 15s pull |
| **L2 应用指标** | QPS / latency / 错误率 / 业务 counter | OTel SDK + Prometheus | 15s pull |
| **L3 日志聚合** | structured log / 错误堆栈 | Promtail → Loki | 秒级 tail |
| **L4 分布式追踪** | supervisor→handoff→subgraph span 链 | OTel SDK → Tempo | 实时 push |
| **L5 告警** | 异常通知 | Alertmanager + Grafana | 15s 评估 |

**资源占用估算**：全部加起来约 1.5GB 内存 / 0.3 CPU。

### 8.1 Agent 特有的业务指标（14 个）

| 指标 | 类型 | 含义 |
|---|---|---|
| `paradise_intent_confidence_bucket` | histogram | intent 分类置信度分布 |
| `paradise_intent_source_total{tier}` | counter | 各 cascade 层命中数 |
| `paradise_mode_selected_total{mode}` | counter | 各 pattern 选中次数 |
| `paradise_subgraph_invocation_total{pattern}` | counter | 子图执行次数 |
| `paradise_handoff_depth_bucket` | histogram | handoff 嵌套深度 |
| `paradise_handoff_status_total{status}` | counter | handoff 结果分布 |
| `paradise_ttft_ms_bucket` | histogram | time to first token |
| `paradise_turn_duration_ms_bucket{mode}` | histogram | 每种 mode 端到端耗时 |
| `paradise_tool_call_duration_ms{tool}` | histogram | 单工具耗时 |
| `paradise_cost_per_request_usd_bucket` | histogram | 单请求成本 |
| `paradise_cache_hit_total{layer}` | counter | 缓存命中 |
| `paradise_circuit_state{transport}` | gauge | 熔断器状态 |
| `paradise_atif_export_queue_depth` | gauge | ATIF 导出队列积压 |
| `paradise_llm_token_total{type,transport}` | counter | token 消耗 |

### 8.2 Span 链路示例

```
trace_id: abc123
└── supervisor.turn                    3.2s
    ├── supervisor.intent              420ms
    │   ├── intent.rule                  2ms  → chitchat 0.95
    │   ├── intent.embedding            48ms  → miss
    │   └── intent.llm                 370ms  → chitchat 0.88
    ├── supervisor.mode_judge          380ms  → mode=chat (conf 0.91)
    ├── supervisor.handoff              2.3s
    │   └── subgraph.chat              2.3s
    │       └── llm.complete           2.1s  transport=ollama
    ├── supervisor.reflect              50ms
    └── supervisor.cleanup              30ms
        └── atif.export                  5ms  (fire-and-forget)
```

### 8.3 告警规则（5 条）

| 规则 | 触发条件 | 持续 |
|---|---|---|
| `HighErrorRate` | turn error rate > 5% | 5m |
| `HighLatencyP95` | p95 turn latency > 10s | 5m |
| `CircuitBreakerOpen` | 任一 transport circuit breaker open | 1m |
| `OllamaUnhealthy` | ollama container down | 1m |
| `DepthCapTriggered` | depth_capped 频率 > 0.5/min | 5m |

### 8.4 Grafana Dashboard（5 个面板）

1. **Overview**：QPS / p95 latency / error rate / cache hit rate
2. **Intent Cascade**：各 tier 命中率 + 平均置信度
3. **Mode Distribution**：11 个 pattern 的选中次数 + 平均耗时
4. **Subgraph Performance**：每个 pattern 的 p50/p95/p99 + depth 分布 + cost 累计
5. **LLM Health**：每个 transport 的 circuit state / retry count / token 消耗 / cost per request

---

## 9. 实施路径

### 9.1 Agent 架构改造（Phase 2.6 - 2.13）

| Phase | 内容 | 估时 | 依赖 |
|---|---|---|---|
| **2.6** | `handoff.py` + `registry.py` + `patterns/base.py` + `patterns/stub.py` + 单测 | 4h | - |
| **2.7** | 注册 11 个 pattern（4 真 + 7 stub）；mode_judge prompt 自动过滤 unavailable | 2h | 2.6 |
| **2.8** | `mode_judge.py` + intent_node 并行化 | 2h | - |
| **2.9** | supervisor graph 骨架（INTENT→ROUTE→HANDOFF→REFLECT→CLEANUP，stub subgraph 跑通） | 4h | 2.6, 2.8 |
| **2.10** | 实现 `chat` + `tool_react` pattern | 4h | 2.9 |
| **2.11** | 实现 `rag` + `plan_execute` pattern | 6h | 2.10 |
| **2.12** | depth cap + 嵌套（plan_execute 拆步调用 tool_react） | 4h | 2.11 |
| **2.13** | `atif_exporter.py` + cleanup_node 接入 + 配置 flag | 3h | 2.9 |

总计约 29h。

### 9.2 监控栈（Phase 6.1 - 6.6）

| Phase | 内容 | 估时 |
|---|---|---|
| **6.1** | `docker-compose.monitoring.yml` + 启动监控栈（不带业务埋点） | 2h |
| **6.2** | OTel SDK 接入 + FastAPI auto-instrument + 全链路 span 打通 | 3h |
| **6.3** | `observability/metrics.py` 集中声明 14 个业务指标 + 各节点埋点 | 4h |
| **6.4** | structlog JSON formatter + Promtail 接 Loki | 2h |
| **6.5** | Grafana 5 个 dashboard JSON（provisioning 自动导入） | 3h |
| **6.6** | 5 条告警规则 + Alertmanager + 测试触发 | 2h |

总计约 16h。

### 9.3 并行节奏

| 周 | Agent 改造 | 监控 |
|---|---|---|
| W1 | Phase 2.6-2.9（骨架） | Phase 6.1-6.2（栈起立） |
| W2 | Phase 2.10-2.13（pattern 填充） | Phase 6.3-6.4（业务埋点） |
| W3 | - | Phase 6.5-6.6（dashboard + 告警）+ Phase 7（smoke） |

监控与 agent 改造 **并行推进**，互不阻塞。

---

## 10. 落地后的资产

| 资产 | 状态 |
|---|---|
| 4 个基础 mode | 真实可用 |
| 7 个 multi-agent pattern | 注册可见、stub 可 fallback、待实现时填 build() |
| Handoff Protocol | 数据契约完备 |
| Subgraph Registry | 11 项已注册 |
| Supervisor graph | 完整执行链路 |
| ATIF 导出 | 开关可控、关时零开销 |
| 深度护栏 | MAX_DEPTH=2 |
| 规范化扩展点 | 加新 pattern = 新增 1 个 class + 1 行 register |
| 5 层监控栈 | Prometheus + Loki + Tempo + Grafana + Alertmanager |
| 14 个业务指标 | 全部埋点 |
| 5 个 Grafana dashboard | 自动 provisioning |
| 5 条告警规则 | 单机内网 + 可选 webhook |

---

## 11. 与原 Plan 的关系

本文档 **取代** 原 plan（`/home/wwk/.claude/plans/immutable-percolating-snail.md`）中：

- Phase 2 内容（LangGraph 编排替换 → 升级为 supervisor + subgraph）
- Phase 6 内容（Observability → 升级为 5 层监控栈）

原 plan 的 Phase 0/1/3/4/5/7 继续有效：

| 原 Phase | 状态 | 说明 |
|---|---|---|
| Phase 0（分支与基线） | ✅ 已完成 | prod 分支已建立，docker-compose 起立 |
| Phase 1（Ingress 加固） | ⏸ 待推 | API Key + rate limit + guardrails |
| Phase 2（LangGraph 编排） | 🔄 升级为 2.6-2.13 | 见本文档 §9.1 |
| Phase 3（Transport 健壮性） | ⏸ 待推 | ResilientTransport 包装 |
| Phase 4（Tool Sandbox） | ⏸ 待推 | privilege rings + subprocess 隔离 |
| Phase 5（Memory Stack） | ⏸ 待推 | Redis + Qdrant + checkpointer |
| Phase 6（Observability） | 🔄 升级为 6.1-6.6 | 见本文档 §9.2 |
| Phase 7（一键部署） | ⏸ 待推 | smoke_prod.sh + README.prod.md |

---

## 12. 三大改造的关系（W1，分开推进）

本文档 **不含** 三大改造：
- W1-A：Trace→Workflow→Skill
- W1-B：Claude Code 风格文件记忆
- W1-C：折叠式上下文

三大改造的进度见 memory：`paradise_evolution_design.md`。

**架构层面的关系**：
- 本文档的 supervisor + subgraph 是 **执行骨架**
- 三大改造是 **能力增强**（在 subgraph 内部或 supervisor 反思环节注入）
- 两者独立演进，互不阻塞
