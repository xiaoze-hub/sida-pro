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
- Agents: implement in `src/agents/*.py`, register in `server.py` (`AGENT_REGISTRY`) and seed config in `seed_agents()`.
- Collectors: place in `src/collectors/`, keep stateless; return typed dataclasses.
- TypeScript: components `PascalCase.tsx` in `frontend/src/`, hooks `use-` prefix, utilities `camelCase.ts`.
- Prompts: one prompt file per agent in `prompts/` (e.g., `daily_report.txt`).

## Testing Guidelines
- Backend: structure tests as `tests/test_<module>.py`; prefer fast, isolated unit tests around agents, collectors, and core.
- Coverage: target meaningful coverage for new modules (no strict threshold yet, but include happy-path and error cases).
- Fixtures: use factory helpers for DB models; avoid network calls (mock collectors and AI clients).

## Commit & Pull Request Guidelines
- Commit format: `<type>: <subject>` where type ∈ `{fix, feature, update, doc}`.
- Keep the type prefix in English, and write the subject after the colon (plus any optional commit body) in Chinese.
  Example: `feature: 新增盘中监控 Agent`.
- Keep one logical, reviewable change per commit. Once a change is ready to record, commit it instead of accumulating unrelated work.
- Every commit must update `CHANGELOG.md` in the same commit. Add a concise entry under the current date and one of these headings:
  - `fix` — bug fixes and regression corrections.
  - `feature` — new user-facing or developer-facing capabilities.
  - `update` — changes to existing behavior, dependencies, configuration, refactors, tests, or operations.
  - `doc` — documentation and development-process changes.
- Do not create a code-only commit followed by a separate changelog commit; the change and its changelog entry are one atomic commit.
- Pull Requests: include a clear description, linked issues, and screenshots/GIFs for UI changes. Update docs/prompts when applicable.
- CI hygiene: ensure backend runs (`python server.py`) and frontend builds (`pnpm build`). No secrets in commits; use `.env` or UI settings.

## Security & Configuration Tips
- Secrets: do not commit API keys; configure via UI or env vars (`.env`, `AUTH_USERNAME`, `AUTH_PASSWORD`, `JWT_SECRET`, `DATA_DIR`).
- Network/SSL: optional corporate CA via `data/ca-bundle.pem` is auto-managed; respect `HTTP(S)_PROXY`/app proxy settings.
- Playwright: in Docker, browsers install under `DATA_DIR/playwright` automatically; local dev uses system install.


## SIDA 业务硬约束（LLM 编码智能体必读）
- 单位约定：金额=元，成交量=股；`vol × price == amt` 必须精确匹配，对不上先怀疑单位换算。
- 方向位编码、竞价 M 标记等字段语义见 `a-share-main-force-intent` skill，禁止按直觉猜。
- 数据缺失必须显式标注"无数据"，**禁止 LLM 推测或编造数字**；基准日滞后要在 UI 显式标注。
- 主力意图识别必须走 `get_main_intent`（逐笔口径），禁用 `get_capital_flow`（东财方向位会反）。
- 缓存一律走 `src/web/cache/biz_cache.py`（L1 内存 + L2 Redis，key 前缀 `biz:`），禁止业务代码裸连 Redis。
- 多用户：任何接口/数据改动考虑 user_id 隔离（4 账号并存），不能只验自己账号。
- K线读取走 PG hypertable 优先（`get_klines()`），补数用 klines_ingestor。

## Codex 协作补充
- 本文件与全局 `~/.codex/AGENTS.md` 同时生效；冲突时以本文件为准。
- 动态踩坑知识不在本文件维护——先用 MCP 工具 `tdai_search` 检索历史记忆，重要结论用 `tdai_remember` 写回。
