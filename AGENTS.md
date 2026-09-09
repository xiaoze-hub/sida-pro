# Repository Guidelines

## Project Structure & Module Organization
- `src/agents/` — Agent implementations (business logic). Add new agents here.
- `src/collectors/` — Data collectors (quotes, kline, news, etc.).
- `src/core/` — Core utilities (AI client, notifier, scheduler helpers).
- `src/web/` — FastAPI app (models, API routes, DB setup).
- `frontend/` — React + TypeScript (Vite + Tailwind). UI lives in `frontend/src/`.
- `prompts/` — Prompt templates used by agents.
- `config/`, `data/` — Config files and runtime data (persisted at `DATA_DIR`).
- `server.py` — Backend entrypoint; also registers agents and data sources.
- `tests/` — Placeholder for backend tests.
- `build.sh`, `Dockerfile` — Build frontend and container images.

## Build, Test, and Development Commands
- Backend (dev): `make dev-api`（自动 venv+依赖+uvicorn reload，监听 `:8000`）；或手动 `python server.py`。
- Frontend (dev): `make dev-web`（自动 pnpm install+dev，served on `http://localhost:5183`）。
- Frontend (build): `cd frontend && pnpm install --frozen-lockfile && pnpm build`.
- Docker image: `./build.sh <version>` (copies `frontend/dist` to `./static` and builds image).
- Run via Docker: `docker run -d -p 8000:8000 -v panwatch_data:/app/data xiaoze-hub/stock-intelligent-data-analytics:latest`.
- Tests (backend): add pytest tests under `tests/` then run `pytest`.
- Development lifecycle: routine source changes should use hot reload or restart the affected service. Rebuild Docker images only for release builds or when changing content that is not mounted into the development container, such as packaged frontend assets, dependencies, Dockerfiles, or installed local packages.
- Docker cleanup: after every image build, first confirm the replacement containers are healthy, then remove obsolete PanWatch images and temporary validation images that are not referenced by any container. Never remove running images or data volumes, and do not broadly prune shared build caches without explicit approval.

## Coding Style & Naming Conventions
- Python: PEP 8, 4-space indent, type hints required for new code. Files `snake_case.py`, classes `PascalCase`, functions/vars `snake_case`.
- Agents: implement in `src/agents/*.py` with `@register_agent("<name>")` on the class (auto-discovered by `src/bootstrap/agents.py` since W3.2 — do NOT edit server.py or any manual registry); seed config rows via `AGENT_SEED_SPECS` in `src/core/agent_catalog.py`.
- Collectors: place in `src/collectors/`, keep stateless; return typed dataclasses.
- TypeScript: components `PascalCase.tsx` in `frontend/src/`, hooks `use-` prefix, utilities `camelCase.ts`.
- Prompts: one prompt file per agent in `prompts/` (e.g., `daily_report.txt`).

## Testing Guidelines
- Backend: structure tests as `tests/test_<module>.py`; prefer fast, isolated unit tests around agents, collectors, and core.
- Coverage: target meaningful coverage for new modules (no strict threshold yet, but include happy-path and error cases).
- Fixtures: use factory helpers for DB models; avoid network calls (mock collectors and AI clients).

## Commit & Pull Request Guidelines
- Commit format: `<type>: <subject>` where type ∈ `{feat, fix, update, refactor, docs, test, chore, style, perf}`.
- Keep the type prefix in English, and write the subject after the colon (plus any optional commit body) in Chinese.
  Example: `feat: 新增盘中监控 Agent`.
- Keep one logical, reviewable change per commit. Once a change is ready to record, commit it instead of accumulating unrelated work.
- Every commit must update `CHANGELOG.md` in the same commit. Add a concise entry under the current date with the heading `### <type>-<中文标题>` (matching the commit type; 2026-09-09 W4.1 统一: 此前 AGENTS 写 `feature/doc` 与实际 `feat/docs` 并存, 以实际主流写法为准).
- Do not create a code-only commit followed by a separate changelog commit; the change and its changelog entry are one atomic commit.
- Release steps: VERSION bump must go together with both README badges (`README.md` / `README.zh-CN.md` version line) in the release commit; `VERSION` file is the single source of truth.
- Pull Requests: include a clear description, linked issues, and screenshots/GIFs for UI changes. Update docs/prompts when applicable.
- CI hygiene: ensure backend runs (`python server.py`) and frontend builds (`pnpm build`). No secrets in commits; use `.env` or UI settings.

## 分支工作流（多人+多 AI 协作必读）
- `main` 是唯一长期分支, 永远保持可发版。**禁止直接 push main 做功能开发**。
- 新活从 main 拉分支: `git checkout -b feat/<事>-<日期> main`(功能) / `fix/<事>`(修 bug) / `hotfix/<事>`(生产急修)。
- 在分支上 commit(每个 commit 照旧带 CHANGELOG entry), push 分支, 自测通过后合回 main:
  `git checkout main && git pull --rebase origin main && git merge --no-ff feat/xxx -m "merge: ..."` 然后 `git push origin main`, 删分支。
- 合并前必做: `git log origin/main..HEAD` 确认只含自己的活; 有冲突先在分支上解, 不污染 main。
- 发版(tag)只从 main 打。生产急修走 `hotfix/*`, 合 main 后立即打 tag 发版。
- 接手别人的活: 先读 `CHANGELOG.md` 最近 3 个日期段 + `git log --oneline -10`, 再 `git show <hash>` 看细节, 不要猜。

## Security & Configuration Tips
- Secrets: do not commit API keys; configure via UI or env vars (`.env`, `AUTH_USERNAME`, `AUTH_PASSWORD`, `JWT_SECRET`, `DATA_DIR`).
- Network/SSL: optional corporate CA via `data/ca-bundle.pem` is auto-managed; respect `HTTP(S)_PROXY`/app proxy settings.
- Playwright: in Docker, browsers install under `DATA_DIR/playwright` automatically; local dev uses system install.


## SIDA 业务硬约束（LLM 编码智能体必读）
- 单位约定：金额=元，成交量=股；`vol × price == amt` 必须精确匹配，对不上先怀疑单位换算。
- 方向位编码、竞价 M 标记等字段语义见 `a-share-main-force-intent` skill，禁止按直觉猜。
- 数据缺失必须显式标注"无数据"，**禁止 LLM 推测或编造数字**；基准日滞后要在 UI 显式标注。
- 主力意图识别必须走 `get_main_intent`（腾讯逐笔口径），**禁用 `get_capital_flow`
  做主力意图/方向性判定**（东财方向位会反）。可执行规则（B3/3.4，契约代码
  `src/core/caliber.py`，口径矩阵 `docs/_frozen/caliber_matrix.md`）：
  1. 用途映射：主力意图/吸筹派发 → 仅 `get_main_intent`（tick 逐笔）；
     「资金流向/主力净流入多少/超大单大单」→ `get_capital_flow`（eastmoney4
     按单金额四档归类）；L2 主力净流入 → `get_decision_pioneer`（TQ 口径）。
  2. 口径类型：资金类指标返回值必须带 `caliber`（tick/eastmoney4/ths/unknown）
     + `direction_semantics`（`src/core/caliber.py`）；下游拿不到标签按 unknown
     处理，一律不得用于方向性判定。
  3. 冲突裁决：两口径方向冲突时必须说明差异（逐笔主动买卖 vs 按单金额归类）
     并**一律优先采信逐笔**；代码出口用 `require_directional()` 校验
     （非 tick 做方向判定直接抛 `CaliberViolationError`）。
  4. UI 标注：资金面展示处必须可见口径标签（Dashboard 大盘资金流、暗盘资金
     TOP 榜、chat 工具文本均已带）。
- 缓存一律走 `src/web/cache/biz_cache.py`（L1 内存 + L2 Redis，key 前缀 `biz:`），禁止业务代码裸连 Redis。
- 多用户：任何接口/数据改动考虑 user_id 隔离（4 账号并存），不能只验自己账号。
- K线读取走 PG hypertable 优先（`get_klines()`），补数用 klines_ingestor。
- 数据库分层（W3.1/D2）：PG 为唯一生产口径，SQLite 仅本地开发/单测；方言判定与
  引擎构造只走 `src/db/dialect.py`（`is_postgres()/declared_backend()/
  upsert_sql()/insert_ignore_sql()`），业务代码禁止出现 `IS_PG`（CI 门禁
  `scripts/check_is_pg_scope.py`）；schema 变更唯一入口是 `src/web/migrations.py`
  版本化迁移（含收编的历史 A 层 143-148），禁止运行时建表/加列。

## Codex 协作补充
- 本文件与全局 `~/.codex/AGENTS.md` 同时生效；冲突时以本文件为准。
- 动态踩坑知识不在本文件维护——先用 MCP 工具 `tdai_search` 检索历史记忆，重要结论用 `tdai_remember` 写回。
