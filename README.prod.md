# Paradise Agent — Production Branch (`prod`)

This branch hardens paradise-agent for single-server production deployment.
The original `main` branch stays in dev/single-machine-test mode.

## Architecture (5 layers, single-server adapted)

| Layer | Component | Status |
|---|---|---|
| L1 Ingress | FastAPI middleware (auth + rate limit + guardrails) | ✅ Phase 1 |
| L2 Orchestration | LangGraph StateGraph (4-phase: TOOL→THINK→RESPOND→REFLECT) | ✅ Phase 2 |
| L3 Inference | Existing 3 transports + ResilientTransport wrapper | ✅ Phase 3 |
| L4 Tools | ToolRegistry + privilege rings + subprocess isolation | ✅ Phase 4 |
| L5 Memory | Redis (L1 state) + Qdrant (L2 semantic) + SQLite (checkpoint) | ✅ Phase 5 |
| Sidecar | OTel tracing + metrics + /metrics + structured logging | ✅ Phase 6 |

## Branch boundary (critical)

`main` branch code paths must remain unaffected. All prod changes are:

1. **Additive** — new files only (docker-compose, middleware, factory, graph, resilient, executor, redis/qdrant providers, observability)
2. **Flag-gated** — minimal edits to existing files use `if config.mode == "prod"` (or `PARADISE_MODE` env var)
3. **Dependency-isolated** — new deps only in `requirements-prod.txt`, original `requirements.txt` unchanged

### Modified files (5, all flag-gated)

| File | Change | Default behaviour |
|---|---|---|
| `server/main.py` | +Prod middleware registration + /metrics endpoint | Skipped when `PARADISE_MODE != prod` |
| `server/agent_handler.py` | +Graph routing via `PARADISE_MODE` env var | Falls through to `handle_message` |
| `server/paradise/config.py` | +`mode: str = "dev"`, +`langgraph_enabled: bool = False` | Both default to dev values |
| `server/paradise/core/agent.py` | +`run_via_graph()` method | `handle_message()` unchanged |
| `server/paradise/tools/registry.py` | +`privilege: str = "read"` field on ToolEntry | Default "read" = no behavior change |

## Quick start

```bash
# 1. Switch to prod branch
git checkout prod

# 2. Configure API key
export PARADISE_API_KEYS="your-secret-key-here"

# 3. Bring up the stack
docker compose up -d --build

# 4. Wait for healthy
docker compose ps  # All services should show healthy

# 5. Run smoke test
./scripts/smoke_prod.sh
# Expected: 7/7 PASS
```

## Configuration

All prod config lives in `server/paradise/config.prod.yaml`. Override via env vars:

| Env var | Default | Purpose |
|---|---|---|
| `PARADISE_MODE` | `dev` | Set to `prod` to enable middleware chain |
| `PARADISE_API_KEYS` | `dev-key-please-rotate` | Comma-sep valid API keys |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection (L1 state) |
| `QDRANT_URL` | `http://qdrant:6333` | Qdrant connection (L2 semantic) |
| `POSTGRES_DSN` | `postgresql://paradise:paradise@postgres:5432/paradise` | LangGraph checkpointer |
| `PARADISE_ENABLE_ADMIN_TOOLS` | unset | Set `1` to allow admin-privilege tools |
| `OLLAMA_API_URL` | `http://host.docker.internal:11434` | LLM backend (uses host Ollama) |

## Branch workflow (dev → prod)

```
main      ← original dev / prototype branch (kept for historical reasons)
dev       ← active development branch (cut from prod; all new work lands here)
prod      ← protected release branch (only accepts PR merge from dev)
```

**Iron rule: nothing lands on `prod` without first landing on `dev` and passing
the smoke suite.** `prod` should always be deployable as-is.

```bash
# 1. Day-to-day work on dev
git checkout dev
# ... edit, commit, push ...
git push origin dev

# 2. Validate before promotion
docker compose up -d --build
./scripts/smoke_prod.sh                                   # expect 5/7+ PASS
docker compose exec api PARADISE_MODE=dev pytest server/paradise/tests/  # 59/59 PASS

# 3. Promote to prod via PR (GitHub UI protects prod from direct push)
gh pr create --base prod --head dev --title "release: <summary>"
# Review, CI green, merge → prod is now at the new commit

# 4. Sync main if needed (rare; only when main's dev-mode path needs an update)
git checkout main
git merge dev --no-ff
# Conflicts only in flag blocks — keep dev's side
```

### GitHub-side protection (one-time setup)

On GitHub → Settings → Branches → Add rule for `prod`:
- ✅ Require a pull request before merging (1 approval)
- ✅ Require status checks to pass (select `smoke_prod` once CI is wired)
- ✅ Require branches to be up to date before merging
- ❌ Do NOT allow force pushes
- ❌ Do NOT allow deletions

After this, direct `git push origin prod` will be rejected by GitHub.
```

## Legacy sync workflow (main → prod)

The original single-branch model (commit on main, merge into prod) still works
for historical context, but new work should use the **dev → prod** flow above.

```bash
# Bugfix on main
git checkout main
# ... edit, commit ...

# Sync to prod (now requires PR if prod branch protection is enabled)
git checkout prod
git merge main
# Conflicts expected only in flag blocks — keep prod's side

# Verify both paths still work
docker compose up -d --build
./scripts/smoke_prod.sh                              # prod path
docker compose exec api PARADISE_MODE=dev pytest server/paradise/tests/test_smoke.py  # dev path
```

## Architecture decisions (rationale)

- **LangGraph StateGraph (not Supervisor)**: paradise is currently single-agent; StateGraph maps 1:1 to its existing 4-phase loop. Supervisor deferred to W2.
- **ResilientTransport (decorator)**: proxy pattern, doesn't modify the 3 existing transport classes.
- **In-process rate limiter**: single-server scope. For multi-node, swap for Redis-backed.
- **MemorySaver default for LangGraph**: no external dep. PostgresSaver available for cross-restart continuity.
- **Regex PII (not Presidio)**: lighter, sufficient for MVP. Upgrade path is drop-in.

## Test suite

```bash
cd server
python -m pytest paradise/tests/ middleware/tests_middleware.py -v
# Expected: 59/59 PASS
```

## Deferred to follow-up (W-series)

- W1: 三大改造 (Trace→Skill / 文件记忆 / 折叠上下文) — separate track
- W2: Multi-agent LangGraph Supervisor topology (current is single-agent StateGraph)
- W3: LangSmith tracing integration (currently OTel → local collector)
- W4: Data flywheel (aichat pipeline → semantic cache)
- W4': Pinecone + Feast (L3 episodic + real-time features)
- W5: Multi-node K8s + vLLM (when DAU > 10k)
