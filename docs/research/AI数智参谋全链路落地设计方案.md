# SIDA-Pro AI 数智参谋（操盘手副驾驶）全链路架构与落地实施方案

> **文档定位**：将 SIDA-Pro 已沉淀的 24+ 通达信接口、CYQ 筹码峰、决策先锋三指标共振、威科夫量价结构、9:25 竞价量比与妖股因子等高阶能力全面接入 AI 大脑，构建主动参谋、全量数据打通与高可见度 Copilot 的完整实战方案。
> **归档路径**：`/home/ubuntu/sida-pro/docs/research/AI数智参谋全链路落地设计方案.md`
> **初版**：2026-10-18（v1，战略层：三阵地 + 三批次）
> **本版**：2026-10-18（v2，工程层：逐字段契约 / 逐工具伪码 / 逐组件 TSX / 门禁与回滚）

---

## 目录

| 章 | 内容 | 读者 |
| :--- | :--- | :--- |
| 0 | 修订记录 + 三条硬约束 | 全员 |
| 1 | 代码事实核对、3 处纠偏、**P0 缺陷**、合规红线、工程约定 | 后端 / 前端 |
| 2 | 三层架构与三条数据流 | 全员 |
| 3 | 每个字段的唯一取数入口与口径串 | 后端 |
| 4 | 端点、Pydantic/TS 双写契约、缺失语义矩阵、缓存、判定引擎真值表 | 后端 / 前端 |
| 5 | 5 个工具的 schema + 可直接直译的 handler 伪码 + 单测点 | 后端 |
| 6 | 路由矩阵、阶段文案、回答模板、防幻觉约束 | 后端 / Prompt |
| 7 | 注册 anchor、四道门禁命令、合规契约测试 | 后端 / 前端 |
| 8 | 目录、hook、主卡与子块 TSX、胶囊、一级页 | 前端 |
| 9 | 三处入口、App.tsx 改动、分组数取舍、快捷键 | 前端 / 产品 |
| 10 | 令牌、密度、三态、可访问性 | 前端 / 设计 |
| 11 | 预算表、埋点、告警阈值 | 全员 |
| 12 | 后端 10 + 工具 8 + 前端 6 + 真机验收 | 全员 |
| 13 | P0 与 5 个批次，逐文件 + 回滚方案 | 全员 |
| 14 | 风险、需拍板的 4 项、明确不做 | 产品 / 老板 |
| 15 | 可直接勾选的 checklist | 全员 |
| 附录 A | v1 战略原文存档 | 追溯 |

---

## 0. 修订记录与本版增补范围

| 版本 | 增补内容 | 状态 |
| :--- | :--- | :--- |
| v1 | 三阵地战略（工作台参谋卡 / chat 工具链 / 独立入口）、5 工具清单、3 批次路线图 | 保留 |
| **v2（本版）** | ① 以真实代码为准的事实核对与 3 处方案纠偏；② 发现并登记 1 个 **P0 数据缺陷**（CYQ 120日模型当前恒为 null）；③ `GET /api/copilot/brief/{symbol}` 全字段契约（Pydantic + TS 双写）；④ 5 个新增工具可直接照抄的 handler 伪码 + schema + 口径串 + 单测点；⑤ 确定性判定引擎（意图/防守位/预案）的真值表与伪码；⑥ 参谋卡 / 胶囊 / 一级页的 TSX 组件骨架；⑦ 注册顺序、inventory guard、覆盖率棘轮、合规词表四道门禁的逐条动作；⑧ 测试计划与回滚方案 | 新增 |

**v2 的三条硬约束（全文所有设计都必须满足）**

1. **不重复造数**：所有数值必须来自既有 `src/core/*` 单一事实源，参谋层只做「取数 → 规则判定 → 措辞」。禁止在参谋层重算筹码/共振/撤单率。
2. **不重复注册**：`src/agents/chat/registry.py` 对同名工具会 `raise ValueError("chat 工具重复注册")`；现有 49 个工具里已包含 `get_decision_pioneer / get_stock_chips / get_auction_data / get_orderbook`，因此 v1 清单里的 5 个名字必须重新划线（见 §1.2）。
3. **不越层**：`src/core/**` 不得 `import src.web`（B4.1 门禁，`tests/test_w41_core_web_dependency.py`，正则含 `^\s*(from|import)`，把 import 挪进函数体也拦）。参谋引擎放 `src/core/`，API 只做薄壳。

---

## 1. 以代码为准的事实核对（先纠偏，再施工）

### 1.1 仓库与基线

| 项 | 实测值 | 取证命令 |
| :--- | :--- | :--- |
| 生产仓 | `/home/ubuntu/sida-pro`（另一仓 `sida-src` 为开源冻结版，**不要改**） | `git remote -v` |
| 当前基线 | `dbdda12 fix(test): align l2-tab vitest mock contract (v0.14.8)` | `git log --oneline -1` |
| chat 工具总数 | **49** = 31 core + 1 handler-only + 11 thsdk + 6 tq | `tests/test_w33_chat_registry.py::test_registry_integrity`（`assert len(CHAT_TOOL_REGISTRY) == 49`） |
| 导出顺序断言 | `EXPECTED_CORE_ORDER` 31 项，必须是导出清单的**连续前缀** | 同上 `test_exported_tool_order` |
| 工作台建议标签 | `frontend/src/pages/workbench/tabs/SuggestTab.tsx`（174 行，已含 `ThreeTierVerdictRow`） | — |
| 全局聊天 | `frontend/src/components/ChatWidget.tsx`（893 行），在 `App.tsx:720` 挂载，FAB 在 `bottom-20 right-4 md:bottom-5` | — |
| 一级导航 | `frontend/src/App.tsx:89` `navItems` + `:137` `desktopNavGroups`（现有 6 组：market/opportunity/decision/research/mine/system） | — |

### 1.2 纠偏一：5 个工具不能照 v1 的名字直接注册

v1 列出的 `get_decision_resonance` 与已存在的 `get_decision_pioneer` 语义完全重叠；`get_auction_anomaly`（个股竞价快照）与已存在的 `get_auction_data`（竞价池场景）重叠；`get_chip_accumulation` 若命名为筹码类，会与 `get_stock_chips`（客户端公式口径 SSRP/CYC/CYW）被模型混用。

**定案：5 个新增工具按「现有工具的能力盲区」重新划线，并各自在 description 里写明与邻居的分工。**

| 新工具 | 补的是什么盲区 | 与之划清界限的既有工具 |
| :--- | :--- | :--- |
| `get_chip_accumulation` | **自算 CYQ** 的 120 日波段底仓模型（`main_cost` / `cost_70_low|high` / `profit_ratio` / `concentration` / `pattern`） | `get_stock_chips` = 客户端**公式**口径（SSRP/CYC/CYW），两者数值必然不同，description 必须写「口径不同，勿混算」 |
| `get_cancel_order_intent` | TQ 会话级**买卖双向撤单率 + 撤单偏向 + 意图定性** | `get_orderbook` = 十档队列/托压单（静态挂单），无撤单行为 |
| `get_wyckoff_structure` | 日 K 量价结构（弹簧针/放量滞涨/缩量沉淀/放量突破）+ Phase A–E | `get_kline_patterns` = K 线形态（金针探底等 29 种），非量价结构 |
| `get_demon_factor` | 妖股六维因子分 + 分级 + 连板身位 + 是否 Top50 | `get_hot_stocks` / `get_opportunities` = 当日热度榜，非事件驱动因子库 |
| `scan_auction_anomaly` | **全市场竞价异动五态扫描**（爆量抢筹/弱转强/诱多试盘/恐慌砸盘/涨停抢筹），可按 `verdict_level` 与竞价金额过滤 | `get_auction_data` = 竞价池固定 scene（overview/strongest/theme…），无五态筛选与金额阈值 |

**第 6 项（不计入 5 个新增）**：`get_decision_pioneer` **原地增强**——`decision_pioneer_text()` 当前只输出「机构活跃度 / GS / L2 净流入」三段，**没有输出 `resonance_state` 五态结论**（`src/core/decision_pioneer.py::decision_pioneer_text()`（尾段 `return " | ".join(parts)` 之前））。补一行 `共振：{resonance_state} · {resonance_action}`，零新增工具、零 inventory 变更，收益最高。

### 1.3 纠偏二：P0 数据缺陷 —— CYQ 120 日模型当前恒为 null（必须先修）

**现象**：`/klines/{symbol}/summary` 的 `main_accumulation_chips` 字段与 K 线横向筹码山峰图层，在现网**长期为空**。

**根因（已实测复现）**：

- `compute_chips()` / `compute_main_accumulation_chips()` 以**属性访问**读取 bar（`k.low` / `k.volume` / `k.close`，见 `src/core/chip_distribution.py:340-365`）；
- 而 `src/core/decision_pioneer.py:413 fetch_bars()` 明确返回 **`list[dict]`**（`{date,open,high,low,close,volume}`）；
- `src/web/api/klines.py:1174` 取 `bars = fetch_bars(...)` → `:1103` 直接 `compute_main_accumulation_chips(bars)` → `AttributeError: 'dict' object has no attribute 'low'` → 被 `:1107` 的 `except` 吞掉 → 字段直接**不赋值**（连 `None` 都不写），前端判定为无数据。

**实测证据**：

```bash
python3 - <<'EOF'
from src.core.chip_distribution import compute_main_accumulation_chips
bars=[{"date":"2026-01-01","open":10,"high":10.2,"low":9.8,"close":10.1,"volume":1e6} for _ in range(60)]
try: print(compute_main_accumulation_chips(bars))
except Exception as e: print("FAIL", type(e).__name__, e)
EOF
# → FAIL AttributeError 'dict' object has no attribute 'low'
```

**修复（3 行适配器，放在 `src/core/chip_distribution.py` 末尾，供所有调用方复用）**：

```python
class _BarView:
    """把 dict bar 适配成 compute_chips 需要的属性视图(单一适配点, 勿在各调用方各写一份)。"""
    __slots__ = ("date", "open", "high", "low", "close", "volume", "symbol")
    def __init__(self, b: dict, symbol: str | None = None):
        self.date = str(b.get("date") or "")[:10]
        self.open = float(b.get("open") or 0.0)
        self.high = float(b.get("high") or 0.0)
        self.low = float(b.get("low") or 0.0)
        self.close = float(b.get("close") or 0.0)
        self.volume = float(b.get("volume") or 0.0)
        self.symbol = symbol


def as_bar_views(bars: list, symbol: str | None = None) -> list:
    """dict/对象混入的 bars → 属性视图列表(已是对象则原样返回)。"""
    return [b if hasattr(b, "low") else _BarView(b, symbol) for b in (bars or [])]
```

并在 `compute_main_accumulation_chips()` 首行加 `klines = as_bar_views(klines)`（**函数内自愈，所有调用方一次性受益**），再补回归测试：

```python
# tests/test_chip_accumulation_bars.py
def test_dict_bars_accepted():           # 本 P0 的锁定用例
    r = compute_main_accumulation_chips([dict(...) for _ in range(60)], lookback_days=60)
    assert r and r["main_cost"] > 0
def test_object_bars_unchanged():        # 老路径不许回归
    ...
def test_short_series_returns_none():    # <30 根 → None, 不猜
    ...
```

> **施工顺序**：P0 修复独立成一个 commit（`fix(chips): adapt dict bars in CYQ accumulation model`），**先于**参谋卡上线。否则参谋卡的「底仓成本」永远显示 `--`，整个产品的第一卖点失效。

### 1.4 纠偏三：合规词表是硬门禁，v1 的示例文案会踩线

`trading` 相关合同与前端契约测试约定，全站禁止出现 `建议买入 | 建议卖出 | 主力建仓 | 主力出货` 等单点交易/主力动作定性词（`sida-pro-dev` 已登记为硬约束）。v1 §二 的示例文案「主力在 35.20 附近沉淀重仓底仓」本身可用，但同一段里若写成「主力建仓成本」「主力出货」即违规。

**参谋卡文案替换表（施工时逐条对照，CI 用正则拦）**：

| 禁用 | 允许（客观事实口径） |
| :--- | :--- |
| 主力建仓 / 建仓成本 | **底仓成本** / **筹码沉淀区** / `🔴 主力成本` |
| 主力出货 / 派发 | **高位筹码兑现压力** / **获利盘密集** |
| 建议买入 / 建议卖出 | **加仓观察位** / **减仓防守位** / **观察不动** |
| 必涨 / 稳赚 / 抄底必赚 | （删除，不得出现收益承诺） |
| 满仓 / 梭哈 | **分批** / **控制单笔仓位** |

每张卡片底部固定一行：`以上为盘口与筹码数据的客观推演，不构成投资建议。`

### 1.5 其他必须遵守的既有工程约定（避免"写完了但发不出去"）

| 约定 | 具体要求 | 出处 |
| :--- | :--- | :--- |
| 前端格式化 | **禁止裸 `.toFixed()`**（ui-rules R6 棘轮），一律走 `@/lib/format` 的 `safeFixed/safeNum/safeMoney`，非数返回 `--` | `scripts/check_ui_rules.mjs` |
| `.tsx` 行尾 | 仓库 `.tsx` 为 **CRLF**，新增/改写必须同款行尾 | `sida-pro-dev` |
| API 响应信封 | 后端返回 `{"code":0,"success":true,"data":{...}}`；前端 `fetchAPI` 已解包，脚本验证须先取 `data` | 同上 |
| 权限 | 权限点 `view_quotes`（基础）/ `view_forecast`（三指标）/ `view_dark`（暗盘）/ `view_auction`（竞价池）；**非抛错式判断用 `has_perm(user, perm)`**，抛错式用 `enforce_perm` | `src/core/permissions.py:242/250` |
| 新路由挂载 | `src/web/app.py` 显式 `include_router`，路由真实路径 = 挂载前缀 + 装饰器路径；验证用 `app.openapi()["paths"]`，不要看 `app.routes` | 同上 |
| 覆盖率棘轮 | 新增 `src/` 模块必须自带测试，否则总覆盖率跌破 50.0% 基线 | `scripts/check_coverage_ratchet.py` |

---

## 2. 总体架构：证据层 → 判定层 → 触点层

```
┌──────────────────────────── 触点层 Surface ────────────────────────────┐
│  /ai-copilot 一级工作台   │  工作台【多维数智参谋】卡  │  右下角唤醒胶囊  │
│  AiCopilotPage           │  CopilotBriefCard        │  CopilotCapsule  │
└───────────────▲──────────────────▲──────────────────────────▲──────────┘
                │ 同一份 CopilotBrief（同源同口径，禁止各页面各算一份）
┌───────────────┴──────────────────┴──────────────────────────┴──────────┐
│                        判定层 Verdict（确定性，可单测）                  │
│  src/core/copilot_brief.py                                              │
│   build_copilot_brief(symbol, market, perms, now) -> dict               │
│   ├─ INTENT_RULES   意图定性（规则优先级，非 LLM）                       │
│   ├─ DEFENSE_LINES  防守位取数优先级 + 上下关系判定                       │
│   ├─ PLAN_MATRIX    预案真值表（tone × 持仓状态）                        │
│   └─ COPY_DICT      措辞模板（过合规词表门禁）                            │
└───────────────▲─────────────────────────────────────────────────────────┘
                │ 只读调用，不重算
┌───────────────┴─────────────────────────────────────────────────────────┐
│                     证据层 Evidence（既有单一事实源）                    │
│  chip_distribution.compute_main_accumulation_chips  → 底仓/70%带/获利盘  │
│  decision_pioneer.fetch_decision_pioneer            → GS/活跃度/L2/五态  │
│  cancel_order_intent.calc_cancel_order_intent       → 买撤/卖撤/意图     │
│  wyckoff.analyze_wyckoff                            → Phase + 4 类标记   │
│  auction_pool.fetch_auction_anomaly                 → 五态竞价+金额+量比 │
│  demon_factors.load_factor_pool                     → 六维因子/分级      │
│  market_mainline.aggregate_mainline (cache)         → 主线身位          │
└───────────────▲─────────────────────────────────────────────────────────┘
                │
        PG(klines/chip_daily/demon_factors) + TQ 网关 + thsdk
```

**三条数据流**：

1. **卡片流**（页面首屏）：`CopilotBriefCard` → `GET /api/copilot/brief/{symbol}` → `build_copilot_brief()` → 多源并发取数（`ThreadPoolExecutor`，各自独立降级）→ 一份 JSON。
2. **对话流**（聊天）：`ChatWidget / CopilotDrawer` → `POST /api/chat/conversations/{id}/messages/stream` → LLM 自主选工具 → 5 个新工具 → 同一批 core 函数（**口径与卡片完全一致**）。
3. **预注入流**（胶囊零输入）：`createConversation({initial_context})` → 后端把快照拼进 system prompt（`chat.py:929`）→ 用户点快捷气泡直接发问。

**关键不变量**：卡片与对话对同一指标必须给出同一个数字。落地手段是「同一函数 + 同一 `caliber` 字符串」，并在后端单测里断言 `brief["evidence"]["chips"]["main_cost"] == tool_payload["main_cost"]`（同一 symbol，同一次调用内取值）。

---

## 3. 单一事实源映射表（施工时照此 import，禁止另写算法）

| 参谋字段 | 唯一取数入口 | 关键返回键 | 口径串（caliber，前端与工具共用） |
| :--- | :--- | :--- | :--- |
| `evidence.chips` | `src.core.chip_distribution.compute_main_accumulation_chips(bars, lookback_days=120)` | `main_cost/peak_price/peak_ratio/cost_70_low/cost_70_high/cost_90_low/cost_90_high/profit_ratio/concentration/pattern/status_label/signal_level/departure_pct/profit_state` | `自算CYQ(120日·三角分布+换手衰减)，底仓成本=COST(50)` |
| `evidence.resonance` | `src.core.decision_pioneer.fetch_decision_pioneer(symbol, market)` | `institution_activity{activity,level,above_strong,streak_days,ma5}` / `gs{zone,signal,signal_date,pending,...}` / `l2{available,zjl_hb(万元),direction,cancel_rate,cancel_bias}` / `resonance_state/action/warning` | `GS+AI机构活跃度+L2主力净额(TQ口径)，五态状态机` |
| `evidence.microstructure` | `src.core.cancel_order_intent.calc_cancel_order_intent(more_info, pct_chg)` | `b_cancel_ratio/s_cancel_ratio`（**0~1**）/`cancel_bias`（-1~1）/`activity_score`/`intent_tag`/`intent_desc`/`warning_level` | `TQ会话撤单量 BCancel/(TotalBVol+BCancel)；收盘回退最近快照` |
| `evidence.wyckoff` | `src.core.wyckoff.analyze_wyckoff(bars, lookback=60)` | `verdict{stage,stage_label,effort_result,markers_count}` / `markers[]{code,name,date,direction,basis,definition}` | `日K量价结构(放量1.8×/缩量0.6×/弹簧回收≥1%)，不含逐笔` |
| `evidence.auction` | `src.core.auction_pool.fetch_auction_anomaly("CN")` 后按 symbol 命中 | `gap_pct/open_amount(元)/volume_ratio/withdraw_rate(%)/open_zt_buy/verdict_tag/verdict_level/verdict_action` | `9:25 竞价异动池 + TQ OpenAmo/OpenVolPre1/BCancel` |
| `evidence.demon` | `src.core.demon_factors.load_factor_pool(topn=200)` 后按 symbol 命中 | `total/grade/max_streak/n_events/n_sealed/participation/dims/flags` | `妖股六维因子库(涨停事件驱动)Top50 内/外` |
| `evidence.theme` | `src.core.market_mainline.aggregate_mainline()`（60s 缓存）→ 扫 `ranked_groups[].constituents` | `group.name/rank/limit_up_count/score/leader` | `主线 Top20 分组身位（涨停池驱动）` |
| `evidence.quote` | `src.core.marketdata_client.md_quote_rows` / `get_quotes` | `close/change_pct/prev_close/open/high/low/volume` | `腾讯/TQ 实时快照` |
| `defense_lines[].price` | 上述证据的组合，**不引入新数据源** | 见 §4.6 | — |

**并发与降级要求**：7 个证据块用 `ThreadPoolExecutor(max_workers=7)` 并发，每块 `try/except` 独立降级为 `None` + `degraded_reason`，**任何一块失败不得让整个 brief 500**。TQ/thsdk 侧沿用既有熔断（`src/core/thsdk_breaker.call_with_hard_timeout`），硬超时 **≤2.5s/块**，整体响应 P95 ≤ 2.8s（首屏与胶囊都依赖它）。

---

## 4. 后端接口契约：`GET /api/copilot/brief/{symbol}`

### 4.1 路由与挂载

```python
# src/web/api/copilot.py  (新增, 薄壳: 只做鉴权/缓存/序列化, 不含算法)
router = APIRouter(tags=["copilot"])

@router.get("/brief/{symbol}", response_model=CopilotBrief)
def get_copilot_brief(
    symbol: str,
    market: str = Query("CN", pattern="^(CN|HK|US)$"),
    refresh: bool = Query(False, description="true=跳过进程缓存(仅前端手动刷新用)"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
): ...

@router.get("/presets", response_model=list[CopilotPreset])
def get_presets(user: User = Depends(get_current_user)): ...
```

```python
# src/web/app.py (照抄相邻 include_router 的写法)
from src.web.api import copilot
app.include_router(
    copilot.router, prefix="/api/copilot", tags=["copilot"], dependencies=protected
)
```

**验证挂载**（`app.routes` 不可用，必须查 openapi）：

```python
from src.web.app import app
assert "/api/copilot/brief/{symbol}" in app.openapi()["paths"]
```

**非 A 股**：`market != "CN"` 时仍返回 200，`evidence` 各块 `available=false` + `degraded_reason="仅 A 股支持盘口/筹码/共振证据"`，`verdict` 只给 `neutral` 与 `defense_lines=[]`（不伪装）。

### 4.2 顶层响应（Pydantic，逐字段）

```python
# src/web/api/copilot.py
from typing import Literal
from pydantic import BaseModel, Field

Tone = Literal["opportunity", "neutral", "caution", "risk"]

class SessionInfo(BaseModel):
    phase: Literal["pre_open", "auction", "intraday", "closed", "non_trading"]
    is_trading: bool
    stale: bool = Field(..., description="底层证据是否含收盘快照/落库旧值")
    age_s: int | None = Field(None, description="证据最旧一块的年龄(秒)")
    trade_date: str | None = Field(None, description="YYYY-MM-DD，证据所属交易日")

class QuoteBlock(BaseModel):
    available: bool
    close: float | None = None
    change_pct: float | None = None
    prev_close: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    source: str | None = Field(None, description="tq / tencent / snapshot")
    as_of: str | None = None
    degraded_reason: str | None = None
```

```python
class CopilotBrief(BaseModel):
    symbol: str
    market: str
    name: str | None
    as_of: str                      # ISO8601 +08:00，生成本次 brief 的时刻
    trade_date: str | None
    session: SessionInfo
    quote: QuoteBlock
    evidence: "EvidenceBundle"
    verdict: "CopilotVerdict"
    cache: dict                     # {"hit": bool, "ttl_s": int, "key": "600111:CN:intraday"}
    version: str = "copilot-brief/1"   # 契约版本，前端按此做兼容分支
```

### 4.3 证据块（`EvidenceBundle`）—— 7 块，结构固定、可空

```python
class EvidenceBase(BaseModel):
    available: bool
    caliber: str                    # 口径串，前端原样显示在卡片脚注
    as_of: str | None = None
    degraded_reason: str | None = None
    gated: bool = Field(False, description="True=权限不足，非数据缺失")

class ChipEvidence(EvidenceBase):
    main_cost: float | None         # 底仓成本 = COST(50)
    peak_price: float | None        # 筹码峰价位
    peak_ratio: float | None        # 峰占比(%) 0-100
    cost_70_low: float | None       # 70% 筹码带下沿 = COST(15)
    cost_70_high: float | None      # 70% 筹码带上沿 = COST(85)
    cost_90_low: float | None       # COST(10)
    cost_90_high: float | None      # COST(90)
    profit_ratio: float | None      # 获利盘(%) 0-100 ← 后端已 ×100
    profit_state: str | None        # 高位获利盘密集 / 主升获利良好 / 成本区震荡洗盘 / 深套
    concentration: float | None     # 集中度 (c90-c10)/c50
    pattern: str | None             # 单峰密集 / 缓步沉淀 / 筹码发散
    status_label: str | None        # 主力高度控盘锁仓 / 建仓区蓄势 / 多空博弈发散
    signal_level: Literal["bullish", "neutral", "warning"] | None
    departure_pct: float | None     # (收盘-底仓成本)/底仓成本×100
    lookback_days: int | None

class ResonanceEvidence(EvidenceBase):
    gs_zone: str | None             # G / S / 中性
    gs_signal: Literal["G", "S"] | None
    gs_signal_date: str | None
    gs_pending: bool | None         # 末根疑似交叉(未确认)
    activity: float | None          # AI 机构活跃度
    activity_level: str | None      # 弱 / 生命 / 强势 / 大牛 / 更佳
    activity_above_strong: bool | None
    activity_streak_days: int | None
    activity_ma5: float | None
    l2_net_wan: float | None        # L2 主力净额(万元！TQ 原始单位)
    l2_direction: str | None        # 净流入 / 净流出 / 平衡
    l2_cancel_rate: float | None    # 撤单率(%) L2 口径
    l2_cancel_bias: float | None    # 撤单偏向(%) >0 买方撤多
    resonance_state: str | None     # 🔥首次共振 / ⭐共振向好 / ⚠️全线走坏 / ⚡警惕背离 / 👀潜伏吸筹
    resonance_action: str | None
    resonance_warning: Literal["opportunity", "safe", "caution", "warning"] | None

class MicroEvidence(EvidenceBase):
    b_cancel_ratio: float | None    # 0~1（前端 ×100 显示）
    s_cancel_ratio: float | None    # 0~1
    cancel_bias: float | None       # -1~1
    activity_score: float | None    # 0~1
    intent_tag: str | None          # ⚠️托单诱多 / 🛡️压盘吸筹 / 买单虚脱 / 抛压减退 / 频繁试盘 / 多空均衡
    intent_desc: str | None
    warning_level: Literal["safe", "caution", "warning"] | None
    volume_ratio: float | None
    turnover_rate: float | None
    session_fallback: bool = Field(False, description="True=盘中会话值为 0，已回退收盘快照")

class WyckoffEvidence(EvidenceBase):
    stage: str | None               # A / B / C / D/E / unknown
    stage_label: str | None
    effort_result: Literal["divergence", "match", "unknown"] | None
    markers_count: int | None
    recent: list[dict]              # [{code,name,date,direction,basis,definition}] 最多 5 条
    note: str | None                # 「仅日K量价结构，非方向结论」

class AuctionEvidence(EvidenceBase):
    verdict_tag: str | None         # 👑涨停抢筹 / 🔥爆量抢筹 / 🛡️弱转强 / ⚠️诱多试盘 / ⚡恐慌砸盘 / 常态试盘
    verdict_level: Literal["strong_buy", "buy", "warning", "risk", "neutral"] | None
    verdict_action: str | None
    gap_pct: float | None           # 竞价涨幅(%)
    open_amount: float | None       # 竞价成交额(元)
    volume_ratio: float | None      # OpenVolPre1
    withdraw_rate: float | None     # 竞价真实撤单率(%)
    open_zt_buy: float | None       # 涨停委买额(元)

class DemonEvidence(EvidenceBase):
    total: float | None             # 六维综合分
    grade: str | None               # 极妖 / 妖 / 活跃 / 普通
    max_streak: int | None
    n_events: int | None            # 近 260 日涨停事件数
    n_sealed: int | None
    participation: float | None
    dims: dict                      # {维度: 分值}
    flags: list[str]
    in_top50: bool | None
    rank: int | None

class ThemeEvidence(EvidenceBase):
    group: str | None
    rank: int | None                # 主线排名 1..20
    limit_up_count: int | None
    leader: str | None
    is_top5: bool | None

class EvidenceBundle(BaseModel):
    chips: ChipEvidence
    resonance: ResonanceEvidence
    microstructure: MicroEvidence
    wyckoff: WyckoffEvidence
    auction: AuctionEvidence
    demon: DemonEvidence
    theme: ThemeEvidence
```

### 4.4 判定结果（`CopilotVerdict`）

```python
class DefenseLine(BaseModel):
    slot: Literal["first", "core", "probe", "cost", "cap"]
    label: str                  # 「第一防守」「核心生命线」「弹簧针低点」「底仓成本」「强压制」
    price: float
    basis: str                  # 人话依据，含来源与日期
    source: Literal["intraday_low", "main_cost", "cost_70_low", "cost_70_high", "spring_low"]
    side: Literal["support", "resistance"]
    distance_pct: float         # 相对现价 %，负数=在下方

class PlanTrigger(BaseModel):
    price: float | None
    kind: Literal["defend", "reduce", "entry", "invalidate"]
    note: str

class ActionPlan(BaseModel):
    audience: Literal["holder", "empty"]
    action: Literal["reduce", "hold", "defend", "wait", "probe", "avoid"]
    title: str                  # 「持仓者」「空仓者」
    text: str                   # 单句可执行，含价位
    triggers: list[PlanTrigger]

class CopilotVerdict(BaseModel):
    intent_label: str           # 例：「托单诱多 · 高位兑现压力」
    intent_tone: Tone
    confidence: float           # 0~1
    evidence_used: list[str]    # ["chips","microstructure","resonance"]
    evidence_missing: list[str]
    narrative: str              # 确定性模板生成，2~4 句，含具体数字
    defense_lines: list[DefenseLine]
    action_plans: list[ActionPlan]
    risk_flags: list[str]       # ["high_profit_ratio","fake_bid","g_divergence","resonance_break"]
    caliber_notes: list[str]    # 各证据口径串，供展开「证据链」抽屉
    disclaimer: str             # 固定合规句
    rule_id: str                # "R1"…"R9"，便于审计与回放（每条结论可追溯到规则）
```

### 4.5 前端 TS 类型（`frontend/src/lib/copilot/types.ts`，与后端一一对应）

```ts
export type Tone = 'opportunity' | 'neutral' | 'caution' | 'risk'
export type DefenseSlot = 'first' | 'core' | 'probe' | 'cost' | 'cap'

export interface EvidenceBase {
  available: boolean
  caliber: string
  as_of: string | null
  degraded_reason: string | null
  gated: boolean
}

export interface ChipEvidence extends EvidenceBase {
  main_cost: number | null
  peak_price: number | null
  peak_ratio: number | null
  cost_70_low: number | null
  cost_70_high: number | null
  cost_90_low: number | null
  cost_90_high: number | null
  profit_ratio: number | null      // 0-100
  profit_state: string | null
  concentration: number | null
  pattern: string | null
  status_label: string | null
  signal_level: 'bullish' | 'neutral' | 'warning' | null
  departure_pct: number | null
  lookback_days: number | null
}
/* … Resonance/Micro/Wyckoff/Auction/Demon/Theme 同构，逐字段镜像 Pydantic … */

export interface DefenseLine {
  slot: DefenseSlot
  label: string
  price: number
  basis: string
  source: 'intraday_low' | 'main_cost' | 'cost_70_low' | 'cost_70_high' | 'spring_low'
  side: 'support' | 'resistance'
  distance_pct: number
}

export interface ActionPlan {
  audience: 'holder' | 'empty'
  action: 'reduce' | 'hold' | 'defend' | 'wait' | 'probe' | 'avoid'
  title: string
  text: string
  triggers: { price: number | null; kind: 'defend' | 'reduce' | 'entry' | 'invalidate'; note: string }[]
}

export interface CopilotVerdict {
  intent_label: string
  intent_tone: Tone
  confidence: number
  evidence_used: string[]
  evidence_missing: string[]
  narrative: string
  defense_lines: DefenseLine[]
  action_plans: ActionPlan[]
  risk_flags: string[]
  caliber_notes: string[]
  disclaimer: string
  rule_id: string
}

export interface CopilotBrief {
  symbol: string
  market: string
  name: string | null
  as_of: string
  trade_date: string | null
  session: { phase: 'pre_open' | 'auction' | 'intraday' | 'closed' | 'non_trading'; is_trading: boolean; stale: boolean; age_s: number | null; trade_date: string | null }
  quote: { available: boolean; close: number | null; change_pct: number | null; prev_close: number | null; open: number | null; high: number | null; low: number | null; source: string | null; as_of: string | null; degraded_reason: string | null }
  evidence: {
    chips: ChipEvidence
    resonance: ResonanceEvidence
    microstructure: MicroEvidence
    wyckoff: WyckoffEvidence
    auction: AuctionEvidence
    demon: DemonEvidence
    theme: ThemeEvidence
  }
  verdict: CopilotVerdict
  cache: { hit: boolean; ttl_s: number; key: string }
  version: string
}
```

### 4.6 缺失语义矩阵（前端必须逐格实现，禁止一律 `--` 了事）

| 场景 | `available` | `gated` | `degraded_reason` | 前端表现 |
| :--- | :--- | :--- | :--- | :--- |
| 数据正常 | true | false | null | 正常渲染数值 |
| 权限不足（如 member 看三指标） | false | **true** | `需要「数智决策」权限` | 该行显示「升级解锁」按钮，**不显示 `--`**（`--` 是"无数据"，两者语义不同） |
| 数据源全断 | false | false | `TQ 网关不可用` | `--` + 灰字原因 |
| 收盘后会话值全 0 | true | false | null（`session_fallback=true`） | 正常渲染 + 角标「收盘快照 10-17」 |
| 上市不足 30 根 K 线 | false | false | `日K 不足(仅 18 根)，无法建模` | `--` + 原因（不猜） |
| 非 A 股 | false | false | `仅 A 股支持` | 整块折叠 |
| 竞价非 9:15–9:25 且无落库 | false | false | `今日无竞价异动记录` | 折叠，不占位 |

### 4.7 缓存策略（对齐 `sida-pro-dev` 的既有范式）

```python
# 进程内 LRU + 单飞 + 分级 TTL；绝不缓存"全降级"结果
_TTL = {"intraday": 20, "closed": 300, "non_trading": 1800}

def _phase_ttl(session) -> int:
    if session["is_trading"]: return _TTL["intraday"]
    if session["phase"] == "closed": return _TTL["closed"]
    return _TTL["non_trading"]

def get_copilot_brief(...):
    key = f"{symbol}:{market}:{phase_key}"
    hit = _cache.get(key)
    if hit and not refresh and time.time() - hit.ts < hit.ttl:
        return {**hit.payload, "cache": {"hit": True, "ttl_s": hit.ttl, "key": key}}
    # 单飞：同一 key 并发只算一次
    with _flight_lock(key):
        ...
        brief = build_copilot_brief(...)
        # 时间字段绝不进缓存体
        brief.pop("as_of", None)
        if len(brief["verdict"]["evidence_used"]) >= 2:   # worth-check：全降级不缓存
            _cache.set(key, ...)
    return {**brief, "as_of": now_iso(), "cache": {"hit": False, ...}}
```

> 三个必踩坑（已写入 `sida-pro-dev`）：① `as_of` 必须每次现算，缓存体会让 UI 的「更新于 HH:MM:SS」显示缓存填充时刻；② 全降级结果不得入缓存，否则一次抖动被钉死整个 TTL；③ 缓存要在测试里 autouse 清理，否则后跑的用例读到前一个用例的回填数据。

### 4.8 `GET /api/copilot/presets`（一级页左侧指令池）

```python
class CopilotPreset(BaseModel):
    id: str
    icon: str            # emoji，前端直接渲染
    label: str
    prompt: str          # 点击后原样发给对话的 user content
    group: Literal["auction", "chip", "resonance", "demon", "defense"]
    requires_perm: str | None = None   # null=登录即可
```

```json
[
  {"id":"auction_strong","icon":"🔥","label":"扫描今日 9:25 爆量抢筹","prompt":"扫描今天集合竞价里爆量抢筹(竞价金额>2000万且量比>2.5)的股票，按竞价金额降序给出前 10 只，并标注每只的五态判定与竞价撤单率。","group":"auction","requires_perm":"view_auction"},
  {"id":"chip_cost","icon":"🎯","label":"诊断持仓股底仓成本与获利盘","prompt":"用我的持仓列表，逐只给出底仓成本(COST50)、70%筹码区间与当前获利盘比例，标出已跌破 70% 筹码下沿的票。","group":"chip","requires_perm":null},
  {"id":"resonance_first","icon":"🛡️","label":"筛三指标首次共振股","prompt":"筛出今天三指标首次共振(🔥首次共振·机会挖掘)的股票，并按机构活跃度降序排列，说明每只的 GS 区间与 L2 主力净额。","group":"resonance","requires_perm":"view_forecast"},
  {"id":"demon_mainline","icon":"👑","label":"检索主线中的妖股候选","prompt":"找出当前主线 Top5 板块里、同时进入妖股因子库 Top50 的个股，给出因子分、分级与连板身位。","group":"demon","requires_perm":"view_opportunities"},
  {"id":"defense_check","icon":"🧯","label":"检查持仓的防守位是否破位","prompt":"对每只持仓，给出第一防守位与核心生命线(70%筹码下沿)，并明确哪些已经破位、哪些还在上方。","group":"defense","requires_perm":null}
]
```

### 4.9 判定引擎（`src/core/copilot_brief.py`）—— 规则优先，LLM 只负责措辞

**设计原则**：`intent_label` / `defense_lines` / `action_plans` 全部由**确定性规则**产出，因为它们是"会被用户当交易依据"的结论，必须可单测、可回放、可审计（`rule_id`）。LLM 只在对话里把这些结论讲得更顺，**不得改变任何数字或结论方向**（system prompt 硬约束，见 §6.3）。

#### 4.9.1 意图判定真值表（首条命中即停）

| 序 | 条件（全部 AND） | `intent_label` | `tone` | `risk_flags` |
| :--- | :--- | :--- | :--- | :--- |
| R1 | `chips.profit_ratio ≥ 85` 且 `micro.warning_level == "warning"` | 托单诱多 · 高位兑现压力 | risk | high_profit_ratio, fake_bid |
| R2 | `resonance.resonance_warning == "warning"`（⚠️全线走坏） | 共振转空 · 资金与机构双撤 | risk | resonance_break |
| R3 | `wyckoff.effort_result == "divergence"` 且 `chips.profit_ratio ≥ 70` | 放量滞涨 · 上方有供应 | risk | effort_divergence |
| R4 | `wyckoff.stage == "C"` 且 `micro.warning_level != "warning"` | 弹簧试探 · 洗盘待确认 | opportunity | spring_probe |
| R5 | `resonance.resonance_state` 以 `🔥` 开头 | 三指标首次共振 · 趋势待启动 | opportunity | resonance_first |
| R6 | `chips.pattern == "单峰密集"` 且 `20 ≤ profit_ratio ≤ 60` 且 `gs_zone == "G"` | 低位单峰 · 筹码锁定良好 | opportunity | single_peak |
| R7 | `gs_zone == "G"` 且 `l2_net_wan < 0` | G 区资金背离 · 提防假突破 | caution | g_divergence |
| R8 | `micro.intent_tag ∈ {⚠️托单诱多, 频繁试盘, 买单虚脱}` | 盘口撤单异常 · 支撑虚虚实实 | caution | cancel_anomaly |
| R9 | 兜底 | 多空均衡 · 区间待方向 | neutral | — |

**置信度公式**（可审计、不拍脑袋）：

```
confidence = clamp(0.35 + 0.09 × len(evidence_used) - 0.05 × len(evidence_missing), 0.20, 0.95)
```

- `evidence_used` = `available == true` 且真正参与命中的证据块；
- `evidence_missing` = `available == false` 的块（权限不足的 `gated` 也算 missing）；
- 若 `chips` 缺失，置信度上限压到 **0.55**（因为防守位与预案都依赖筹码带）——这条要在单测里钉住。

#### 4.9.2 防守位判定（取数优先级 + 去重 + 上下关系）

```python
DEFENSE_ORDER = [
    # (slot, label, source_getter, requires)
    ("first", "第一防守",  lambda q, c, w: q.low,                                   "intraday_low"),
    ("core",  "核心生命线", lambda q, c, w: c.cost_70_low,                          "cost_70_low"),
    ("probe", "弹簧针低点", lambda q, c, w: w.spring_low,                           "spring_low"),
    ("cost",  "底仓成本",   lambda q, c, w: c.main_cost,                            "main_cost"),
    ("cap",   "强压制",     lambda q, c, w: c.cost_70_high,                         "cost_70_high"),
]
```

规则：

1. 逐条取值，`None`/`≤0`/与现价偏离 `>25%` 的一律丢弃（远离现价 25% 的"防守位"没有实战意义，只会误导）；
2. 与已保留价位相差 `<0.6%` 视为同一条，保留**优先级更高**的（`first` 最优先），并把被合并的来源写进 `basis`（例：「第一防守（今日探底，与底仓成本重合）」）；
3. `side = "support" if price < close else "resistance"`；`distance_pct = (price - close) / close * 100`；
4. `support` 按价格**从高到低**（最近的第一条在最上），`resistance` 按价格从低到高；
5. 最多输出 **4 条**（1 支撑优先位 + 1 核心 + 1 压制 + 1 备选），超出按优先级截断。卡片空间有限，超过 4 条等于没有重点。

`spring_low` 取法：`wyckoff.recent` 中最近一条 `code == "spring_probe"` 的 `index` → `bars[index]["low"]`（由 core 层回填到 `WyckoffEvidence` 的内部字段 `spring_low`，不对外暴露 bars）。

#### 4.9.3 预案真值表（`tone × 持仓状态`，8 条文案，逐条过合规表）

| tone | audience=holder | audience=empty |
| :--- | :--- | :--- |
| **risk** | **减仓防守**：现价附近分批减至半仓以下；跌破 `first` 清仓离场，不做补仓摊薄。 | **不参与**：等 `core` 被重新收复且 GS 回到 G 区再看，期间不猜底。 |
| **caution** | **持有观察**：不加仓；把止损挂在 `first` 下方 1%，破位先减半。 | **等待确认**：等站回 `cost`（底仓成本）且盘口撤单恢复常态再评估。 |
| **neutral** | **按兵不动**：区间内不动，跌破 `core` 一律减半。 | **观察为主**：等放量站上 `cap` 或回踩 `core` 不破再谈介入。 |
| **opportunity** | **持有待涨**：不追高；回踩 `core` 不破可加一次，跌破 `first` 减半。 | **分批试探**：`core` 附近小仓试错，跌破 `first` 无条件退出。 |

生成规则（伪码）：

```python
def build_plans(tone, lines, close, has_position) -> list[ActionPlan]:
    first  = _pick(lines, "first") or _pick(lines, "core")
    core   = _pick(lines, "core")
    cap    = _pick(lines, "cap")
    # 缺关键位时的诚实降级：不编价位，只给条件句
    if first is None or core is None:
        return [_plan("holder" if has_position else "empty", "wait",
                      "关键价位缺失", "筹码/盘口数据不完整，本次不给具体价位；以分时均线与当日低点为参考，等数据恢复后再评估。", [])]
    ...
```

> **红线**：任何 `text` 里不得出现「建议买入/卖出/主力建仓/主力出货/必涨/稳赚」。`COPY_DICT` 的键在 CI 里被正则扫描（§10.4）。

#### 4.9.4 `narrative` 模板（3 段，共 2–4 句，数字全部来自证据）

```python
NARRATIVE_TMPL = (
    "{chip_sentence}{micro_sentence}{resonance_sentence}"
)
# chip_sentence:
#   有筹码: "底仓成本 {main_cost} 元，70% 筹码集中在 {low}–{high} 元，当前获利盘 {profit}%。"
#   无筹码: "筹码模型暂无数据（{reason}）。"
# micro_sentence:
#   有撤单: "盘口买撤 {b}%／卖撤 {s}%，判定为{intent_tag}。"
#   无:     "盘口撤单数据不可用。"
# resonance_sentence:
#   有五态: "三指标当前为{resonance_state}，机构活跃度 {act}（{level}），L2 主力净额 {net} 万元。"
```

**禁止**在 narrative 里出现"我认为/可能大概/或将"这类模糊词——结论由 `intent_label` 承担，narrative 只陈述证据之间的关系。

### 4.10 错误码与失败语义

| HTTP | 场景 | body |
| :--- | :--- | :--- |
| 200 | 正常（含部分降级） | `CopilotBrief` |
| 401 | 未登录 | 标准信封 |
| 404 | symbol 不存在于证券主表 | `{"detail":"未知标的"}` |
| 429 | 触发限流（member 每分钟 30 次） | `{"detail":"请求过于频繁"}` |
| 500 | **仅**在 `build_copilot_brief` 自身抛未捕获异常时（应通过全量 try 避免） | 标准信封 |

**永不返回 500 的部分失败**：任一证据块异常 → 该块 `available=false`，整体 200。这条要在 `tests/test_copilot_brief.py::test_partial_failure_still_200` 里钉住（用 monkeypatch 让 5 个证据源全抛异常，断言仍 200 且 `verdict.rule_id == "R9"`、`evidence_used == []`）。

---

## 5. 五个新增 AI 工具：契约、伪码、口径、单测

**统一约定（5 个工具全部遵守）**

- 注册位置：`src/agents/chat/registry.py`，**必须插在** `# ──────────────── thsdk 11 个高价值工具` 这行注释**之前**（core 工具必须是导出清单的连续前缀，见 §7.1）。
- handler 签名：`async def _tool_xxx(db: Session, args: dict, user: User | None = None) -> str`。
- 返回**永远是人话字符串**（不是 dict）——chat 的 `_execute_tool` 直接把返回值喂给 LLM。
- 同步网络/DB 调用一律 `asyncio.to_thread`，禁止阻塞事件循环。
- 任何异常**不得上抛**：`except Exception` → 返回 `"xxx 获取失败: ..."`（截断 100 字），并把 `logger.warning` 打全。
- 首行固定 `[数据源: <口径>]`，让模型知道该在回答里怎么标注口径。
- 无数据一律说「不猜 / 未伪造」，**绝不返回 0 冒充真实值**。

### 5.1 `get_chip_accumulation` —— 120 日波段底仓筹码沉淀

```python
@register_chat_tool(
    "get_chip_accumulation",
    schema={
        "type": "function",
        "function": {
            "name": "get_chip_accumulation",
            "description": (
                "个股【底仓筹码沉淀模型】(自算 CYQ, 120 日窗口): 底仓成本 COST(50)、筹码峰价位与占比、"
                "70%/90% 筹码带、获利盘比例、集中度、形态(单峰密集/缓步沉淀/筹码发散)、现价相对底仓成本的脱离度。"
                "用户问「主力底仓在什么位置」「底仓成本多少」「筹码集中不集中」「获利盘多少」「跌到哪算破位」时调用。"
                "⚠️ 本工具是**自算 CYQ 口径**，与 get_stock_chips 的客户端公式口径(SSRP/CYC/CYW)不同，"
                "两者数字不可互相校验；同一问题只用一个口径回答并标注。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "6 位股票代码，如 600111 或 600111.SH"},
                    "market": {"type": "string", "description": "市场，仅 CN", "default": "CN"},
                    "lookback_days": {"type": "integer", "description": "回溯交易日数，默认 120（可 60~250）", "default": 120},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="自算CYQ(120日·三角分布+换手衰减)，底仓成本=COST(50)；与客户端公式口径(SSRP/CYC)不可混算",
)
async def _tool_get_chip_accumulation(db: Session, args: dict, user: User | None = None) -> str:
    from src.core.permissions import PERM_VIEW_QUOTE

    denied = _perm_denied(user, db, PERM_VIEW_QUOTE)
    if denied:
        return denied

    symbol = str(args.get("symbol") or args.get("code") or "").strip()
    if not symbol:
        return "请提供股票代码(symbol)，如 600111 或 600111.SH。"
    market = str(args.get("market") or "CN").upper()
    if market != "CN":
        return "底仓筹码沉淀模型仅支持 A 股(CN)。"
    lookback = max(60, min(int(args.get("lookback_days") or 120), 250))

    try:
        from src.core.chip_distribution import compute_main_accumulation_chips
        from src.core.decision_pioneer import fetch_bars

        # 多取 40 根: 建仓波段定位需要前置窗口(算法内部会再截 lookback 根)
        bars = await asyncio.to_thread(fetch_bars, symbol, market, lookback + 40)
        if not bars:
            return f"[底仓筹码] {symbol} 取不到日K（多源均失败），无法建模 —— 不猜数据。"
        if len(bars) < 30:
            return f"[底仓筹码] {symbol} 日K 仅 {len(bars)} 根（<30），样本不足，不做建模。"

        # 关键: compute_* 用属性访问读 bar; bar 是 dict 时必须先适配(P0 缺陷, 见 §1.3)
        # 修复落在 compute_main_accumulation_chips 内部(as_bar_views), 此处无需再包一层。
        r = await asyncio.to_thread(
            compute_main_accumulation_chips, bars, None, lookback
        )
        if not r:
            return f"[底仓筹码] {symbol} 筹码模型返回空（价格网格异常/成交量为 0），本次不给数值。"

        close = float(r.get("last_close") or 0.0)
        lines = [
            f"[数据源: 自算CYQ·{r['lookback_days']}日底仓沉淀] {symbol} 现价 {close:.2f}",
            f"  底仓成本(COST50)：{r['main_cost']:.2f} 元 ｜ 筹码峰：{r['peak_price']:.2f} 元（占比 {r['peak_ratio']:.1f}%）",
            f"  70% 筹码带：{r['cost_70_low']:.2f} – {r['cost_70_high']:.2f} 元 ｜ 90% 筹码带：{r['cost_90_low']:.2f} – {r['cost_90_high']:.2f} 元",
            f"  获利盘：{r['profit_ratio']:.1f}%（{r['profit_state']}） ｜ 集中度：{r['concentration']:.3f}（{r['pattern']} / {r['status_label']}）",
            f"  现价相对底仓成本：{r['departure_pct']:+.2f}%",
            f"  模型结论：{r['action_advice']}",
            "  ⚠️ 口径：自算 CYQ（三角分布 + 换手率衰减），与 get_stock_chips 的客户端公式口径不同，数字不可互相校验。",
        ]
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        logger.warning("get_chip_accumulation 工具失败 [%s]: %s", symbol, e)
        return f"底仓筹码获取失败: {str(e)[:100]}"
```

**示例输出（北方稀土）**

```
[数据源: 自算CYQ·120日底仓沉淀] 600111 现价 35.32
  底仓成本(COST50)：35.20 元 ｜ 筹码峰：35.10 元（占比 8.4%）
  70% 筹码带：33.44 – 36.96 元 ｜ 90% 筹码带：31.02 – 39.80 元
  获利盘：99.0%（高位获利盘密集 (警惕兑现出货)） ｜ 集中度：0.082（单峰密集 / 主力高度控盘锁仓）
  现价相对底仓成本：+0.34%
  模型结论：筹码高度集中于 35.2 元附近，主力控盘良好，关注突破加仓机会
  ⚠️ 口径：…
```

**单测点**（`tests/test_chat_tools_copilot.py`）

| 用例 | 断言 |
| :--- | :--- |
| dict bars 能吃 | `_tool_get_chip_accumulation(None, {"symbol":"600111"})` 含「底仓成本」，**不**含「失败」（直接锁定 §1.3 的 P0） |
| 空 symbol | 返回提示语，不抛异常 |
| 非 CN | 返回「仅支持 A 股」 |
| K 线不足 | monkeypatch `fetch_bars→[20 根]`，返回「样本不足」，**不**出现数字 |
| 权限不足 | `user=member` 且 perm 被降级 → 返回 `_perm_denied` 文案 |

### 5.2 `get_cancel_order_intent` —— 盘口撤单意图（TQ 会话值）

```python
@register_chat_tool(
    "get_cancel_order_intent",
    schema={
        "type": "function",
        "function": {
            "name": "get_cancel_order_intent",
            "description": (
                "个股【盘口撤单意图】: 买盘撤单率、卖盘撤单率、撤单偏向(-1~1)、撤单活跃度，"
                "并按广发金工口径定性为「⚠️托单诱多 / 🛡️压盘吸筹 / 买单虚脱 / 抛压减退 / 频繁试盘 / 多空均衡」。"
                "用户问「买方是不是在挂假单/托单出货」「卖压重不重」「盘口撤单多不多」「挂单是真的吗」时调用。"
                "收盘后会话值归零 → 自动回退最近收盘快照并标注日期。"
                "⚠️ 与 get_orderbook(十档挂单队列)不同: 本工具看的是**撤单行为**，不是静态挂单。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "6 位股票代码，如 600111"},
                    "market": {"type": "string", "description": "市场，仅 CN", "default": "CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="TQ会话撤单量 BCancel/(TotalBVol+BCancel)，盘中实时；收盘回退最近快照",
)
async def _tool_get_cancel_order_intent(db: Session, args: dict, user: User | None = None) -> str:
    from src.core.permissions import PERM_VIEW_L2

    denied = _perm_denied(user, db, PERM_VIEW_L2)
    if denied:
        return denied

    symbol = str(args.get("symbol") or "").strip()
    if not symbol:
        return "请提供股票代码(symbol)，如 600111。"
    market = str(args.get("market") or "CN").upper()
    if market != "CN":
        return "盘口撤单意图仅支持 A 股(CN)。"

    try:
        from src.core.cancel_order_intent import calc_cancel_order_intent
        from src.core.l2_fund_snapshot import FIELDS, has_live_values, latest_snapshot
        from src.core.marketdata_client import md_more_info

        rows = await asyncio.to_thread(md_more_info, [symbol], market)
        row = (rows[0] if rows else None) or {}
        source = "盘中实时会话值"

        # 会话值全 0(收盘/周末/停牌) → 回退最近收盘快照，绝不把 0 当真实值
        live = {f: row.get(f) for f in FIELDS}
        if not row or not has_live_values(live):
            snap = await asyncio.to_thread(latest_snapshot, symbol, market)
            if not snap:
                return (
                    f"[盘口撤单] {symbol} 当前无会话值（非交易时段）且无收盘快照 —— "
                    "无数据，不伪造。"
                )
            row = snap
            source = f"收盘快照 {snap.get('trade_date') or '未知日期'}"

        # 字段映射: calc_cancel_order_intent 吃 TQ 原始键名(BCancel/TotalBVol…)，
        # md_more_info 归一化后是小写键 —— 必须显式搬一次（这是最容易漏的一步）。
        mi = {
            "BCancel": row.get("cancel_buy"),
            "SCancel": row.get("cancel_sell"),
            "TotalBVol": row.get("total_buy_vol"),
            "TotalSVol": row.get("total_sell_vol"),
            "L2OrderNum": row.get("l2_order_num"),
            "L2TicNum": row.get("l2_tick_num"),
        }
        pct_chg = row.get("change_pct")
        r = calc_cancel_order_intent(mi, pct_chg=pct_chg)
        if not r.get("available"):
            return f"[盘口撤单] {symbol} 当日无有效撤单记录（{r.get('intent_desc')}）。"

        b = r["b_cancel_ratio"] * 100
        s = r["s_cancel_ratio"] * 100
        return "\n".join([
            f"[数据源: TQ 撤单意图 · {source}] {symbol}",
            f"  买盘撤单率：{b:.1f}% ｜ 卖盘撤单率：{s:.1f}% ｜ 撤单偏向：{r['cancel_bias']:+.2f}"
            f"（{'买方撤多' if r['cancel_bias'] > 0 else '卖方撤多'}）",
            f"  撤单活跃度：{r['activity_score']:.2f} ｜ 意图判定：{r['intent_tag']}（{r['warning_level']}）",
            f"  说明：{r['intent_desc']}",
            "  ⚠️ 口径：撤单量 / (委托量 + 撤单量)，TQ 会话累计值；收盘后为最近快照，不代表次日盘口。",
        ])
    except Exception as e:  # noqa: BLE001
        logger.warning("get_cancel_order_intent 工具失败 [%s]: %s", symbol, e)
        return f"盘口撤单意图获取失败: {str(e)[:100]}"
```

**注意**：撤单率源数据是 **0~1** 小数（`cancel_order_intent` 返回 `round(b_ratio,4)`），卡片与工具都必须在**展示层** ×100，且只乘一次。前端 `ThreeTierVerdictRow` 现有实现即 `safeFixed(ci.b_cancel_ratio * 100, 0)`，照抄。

### 5.3 `get_wyckoff_structure` —— 威科夫量价结构

```python
@register_chat_tool(
    "get_wyckoff_structure",
    schema={
        "type": "function",
        "function": {
            "name": "get_wyckoff_structure",
            "description": (
                "个股【威科夫量价结构】: 阶段判定(A 止跌构建 / B 筹码沉淀 / C 终极测试 / D-E 突破趋势 / unknown 区间)"
                "与四类结构标记——放量滞涨(effort_divergence)、弹簧试探(spring_probe)、缩量沉淀(dry_up)、放量突破(markup_break)，"
                "含每条的触发依据与日期。用户问「现在是洗盘还是出货」「有没有弹簧针/终极洗盘」「放量滞涨了吗」"
                "「缩量沉淀到位没」「突破有效吗」时调用。"
                "⚠️ 只描述日K量价结构，**不是**方向结论；方向请配合 get_main_intent / get_decision_pioneer。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "6 位股票代码，如 600111"},
                    "market": {"type": "string", "description": "市场，仅 CN", "default": "CN"},
                    "lookback": {"type": "integer", "description": "回看 bar 数，默认 60", "default": 60},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="日K量价结构(放量1.8×/突破1.5×/缩量0.6×/弹簧回收≥1%)，不含逐笔，非方向结论",
)
async def _tool_get_wyckoff_structure(db: Session, args: dict, user: User | None = None) -> str:
    from src.core.permissions import PERM_VIEW_QUOTE

    denied = _perm_denied(user, db, PERM_VIEW_QUOTE)
    if denied:
        return denied

    symbol = str(args.get("symbol") or "").strip()
    if not symbol:
        return "请提供股票代码(symbol)，如 600111。"
    market = str(args.get("market") or "CN").upper()
    if market != "CN":
        return "威科夫量价结构仅支持 A 股(CN)。"
    lookback = max(30, min(int(args.get("lookback") or 60), 180))

    try:
        from src.core.decision_pioneer import fetch_bars
        from src.core.wyckoff import analyze_wyckoff

        bars = await asyncio.to_thread(fetch_bars, symbol, market, lookback + 30)
        if not bars:
            return f"[量价结构] {symbol} 取不到日K，无法识别结构（不猜）。"
        res = await asyncio.to_thread(analyze_wyckoff, bars, lookback)
        v = res.get("verdict") or {}
        marks = res.get("markers") or []

        if v.get("stage") == "unknown" and not marks:
            return (
                f"[数据源: 日K量价结构] {symbol}：未识别到有效结构（{v.get('stage_label')}）。"
                f"{v.get('note') or ''}"
            )

        lines = [
            f"[数据源: 日K量价结构·威科夫] {symbol}",
            f"  阶段：{v.get('stage')} · {v.get('stage_label')}"
            f" ｜ 努力-结果：{v.get('effort_result')} ｜ 标记数：{v.get('markers_count')}",
        ]
        tail = marks[-5:]
        if tail:
            lines.append("  最近结构标记（新→旧）:")
            for m in reversed(tail):
                basis = "、".join(str(x) for x in (m.get("basis") or [])[:3])
                lines.append(f"    {m.get('date')} {m.get('name')}（{m.get('direction')}）依据: {basis}")
                # 只对最后 2 条附定义，避免刷屏
        lines.append(f"  说明：{v.get('note') or '仅日K量价结构描述，不含逐笔数据，不得作为买卖结论。'}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        logger.warning("get_wyckoff_structure 工具失败 [%s]: %s", symbol, e)
        return f"量价结构获取失败: {str(e)[:100]}"
```

**关键细节**：

- `analyze_wyckoff` 是**鸭子类型**（`_get(bar, key)` 支持 dict 与对象），所以这里可以直接喂 `fetch_bars` 的 dict，**不需要** `_BarView` 适配（与筹码工具不同，别照搬）。
- 弹簧针「待确认」语义必须原样保留：`spring_probe` 的 definition 明确要求「次日缩量企稳(LPS)才确认」，工具输出里**不得**把 spring 说成"见底"。

### 5.4 `get_demon_factor` —— 妖股六维因子与主线身位

```python
@register_chat_tool(
    "get_demon_factor",
    schema={
        "type": "function",
        "function": {
            "name": "get_demon_factor",
            "description": (
                "个股【妖股因子】: 六维综合分(total)、分级(极妖/妖/活跃)、连板身位(max_streak)、"
                "近 260 日涨停事件数/封板数、资金参与度、各维度分项(dims)与标签(flags)，以及是否在妖股先锋 Top50 内及其排名。"
                "用户问「这票妖不妖」「妖股基因怎么样」「连板空间/身位」「在不在妖股池里」「为什么它能成妖」时调用。"
                "⚠️ 因子来自历史涨停事件统计，是对『妖性』的画像，**不是**推荐。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "6 位股票代码，如 002361"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="妖股六维因子库(涨停事件驱动: 连板/封板率/参与度/龙虎榜/流通盘/题材)，非推荐",
)
async def _tool_get_demon_factor(db: Session, args: dict, user: User | None = None) -> str:
    from src.core.permissions import PERM_VIEW_OPPORTUNITIES

    denied = _perm_denied(user, db, PERM_VIEW_OPPORTUNITIES)
    if denied:
        return denied

    symbol = str(args.get("symbol") or "").strip()
    if not symbol:
        return "请提供股票代码(symbol)，如 002361。"

    try:
        from src.core.demon_factors import load_factor_pool

        # 只读 200 行因子表(单表 <200KB, 毫秒级)，比 demon_detail 现算(拉事件+龙虎榜)快两个量级
        pool = await asyncio.to_thread(load_factor_pool, 200, 0)
        if not pool:
            return (
                "[妖股因子] 因子库为空（尚未跑过回填/重算）。"
                "请先执行妖股因子管线，本次不给数值 —— 不猜。"
            )
        idx = next((i for i, p in enumerate(pool) if str(p.get("symbol")) == symbol), None)
        if idx is None:
            return (
                f"[妖股因子] {symbol} 未进入因子库 Top200（近 260 日涨停事件不足或从未触发）。"
                "这不等于『不会妖』，只表示当前无统计样本。"
            )
        p = pool[idx]
        dims = p.get("dims") or {}
        dim_txt = " ｜ ".join(f"{k} {v}" for k, v in list(dims.items())[:6]) or "无分项"
        flags = "、".join(p.get("flags") or []) or "无"
        return "\n".join([
            f"[数据源: 妖股六维因子库·{p.get('factor_date') or '最新'} ] {symbol} {p.get('name') or ''}",
            f"  综合分：{p.get('total')} ｜ 分级：{p.get('grade') or '未分级'} ｜ 池内排名：第 {idx + 1}/200"
            f"（{'Top50 内' if idx < 50 else 'Top50 外'}）",
            f"  连板身位：最高 {p.get('max_streak') or 0} 连板 ｜ 涨停事件：{p.get('n_events') or 0} 次"
            f"（封住 {p.get('n_sealed') or 0} 次） ｜ 资金参与度：{p.get('participation')}",
            f"  分项：{dim_txt}",
            f"  标签：{flags}",
            "  ⚠️ 口径：涨停事件统计画像（近 260 日），非当日热度、非推荐。",
        ])
    except Exception as e:  # noqa: BLE001
        logger.warning("get_demon_factor 工具失败 [%s]: %s", symbol, e)
        return f"妖股因子获取失败: {str(e)[:100]}"
```

**为什么不复用 `demon_pool.demon_detail`**：它每次都 `get_events_window` + `lhb_stats`（外部调用），单次 1–3s，聊天里连问 3 只就会把 5 轮工具预算烧完；因子表是管线预先算好的快照，读 200 行 <10ms。两者口径一致（都源自 `demon_score_from_events`），无分叉风险。

### 5.5 `scan_auction_anomaly` —— 全市场竞价五态扫描

```python
@register_chat_tool(
    "scan_auction_anomaly",
    schema={
        "type": "function",
        "function": {
            "name": "scan_auction_anomaly",
            "description": (
                "【全市场集合竞价异动扫描】: 按五态筛选 9:25 竞价异动股 —— "
                "strong_buy(👑涨停抢筹) / buy(🔥爆量抢筹、🛡️弱转强) / warning(⚠️诱多试盘) / "
                "risk(⚡恐慌砸盘) / neutral(常态试盘)，并给出竞价真实成交额(元)、竞价量比、竞价撤单率、涨停委买额。"
                "用户问「今天竞价谁在爆量抢筹」「有哪些票竞价异动」「竞价撤单率高的票」「谁被核了」时调用。"
                "⚠️ 与 get_auction_data(竞价池固定场景)不同: 本工具可按五态与金额阈值过滤并排序。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "level": {
                        "type": "string",
                        "description": "strong_buy / buy / warning / risk / neutral / all(默认 buy)",
                        "default": "buy",
                    },
                    "min_open_amount": {
                        "type": "number",
                        "description": "竞价成交额下限(元)，默认 0；常见阈值 1e7=1000万、2e7=2000万",
                        "default": 0,
                    },
                    "limit": {"type": "integer", "description": "返回条数，默认 10，最多 30", "default": 10},
                },
                "required": [],
            },
        },
    },
    caliber="9:25 竞价异动池 + TQ OpenAmo/OpenVolPre1/BCancel(真实撤单率)",
)
async def _tool_scan_auction_anomaly(db: Session, args: dict, user: User | None = None) -> str:
    from src.core.permissions import PERM_VIEW_AUCTION

    denied = _perm_denied(user, db, PERM_VIEW_AUCTION)
    if denied:
        return denied

    level = str(args.get("level") or "buy")
    if level not in ("strong_buy", "buy", "warning", "risk", "neutral", "all"):
        level = "buy"
    min_amt = float(args.get("min_open_amount") or 0)
    limit = max(1, min(int(args.get("limit") or 10), 30))

    try:
        from src.core.auction_pool import fetch_auction_anomaly

        # 该函数自带 30s 进程缓存 + TQ 逐票增强(OpenAmo/OpenVolPre1/BCancel)，全市场约 20~60s 冷启动
        rows = await asyncio.to_thread(fetch_auction_anomaly, "CN")
        if not rows:
            return (
                "[竞价异动] 当前无竞价异动记录：可能非 9:15–9:25 时段、非交易日，"
                "或 thsdk 竞价源未就绪。无数据，不猜。"
            )
        picked = [
            r for r in rows
            if (level == "all" or r.get("verdict_level") == level)
            and (r.get("open_amount") or 0) >= min_amt
        ]
        picked.sort(key=lambda r: (r.get("open_amount") or 0, r.get("gap_pct") or -99), reverse=True)
        picked = picked[:limit]

        if not picked:
            return (
                f"[竞价异动] 无满足条件（level={level}, 竞价额≥{min_amt/1e4:.0f}万）的标的；"
                f"全池 {len(rows)} 条。可放宽阈值或换 level=all。"
            )

        lines = [
            f"[数据源: 9:25 竞价异动池] 命中 {len(picked)} 只（level={level}，全池 {len(rows)} 条）:"
        ]
        for i, r in enumerate(picked, 1):
            amt = (r.get("open_amount") or 0) / 1e4
            gap = r.get("gap_pct")
            vr = r.get("volume_ratio")
            wr = r.get("withdraw_rate")
            lines.append(
                f"  {i}. {r.get('symbol')} {r.get('name') or ''} ｜ {r.get('verdict_tag')} "
                f"｜ 竞价额 {amt:,.0f} 万 ｜ 竞价涨幅 {gap:+.2f}%" if gap is not None else
                f"  {i}. {r.get('symbol')} {r.get('name') or ''} ｜ {r.get('verdict_tag')} ｜ 竞价额 {amt:,.0f} 万"
            )
            extra = []
            if vr is not None:
                extra.append(f"量比 {vr:.2f}")
            if wr is not None:
                extra.append(f"撤单率 {wr:.1f}%")
            if r.get("open_zt_buy"):
                extra.append(f"涨停委买 {(r['open_zt_buy'])/1e4:,.0f} 万")
            if extra:
                lines.append(f"      {' ｜ '.join(extra)}")
        lines.append("  ⚠️ 口径：竞价成交额=TQ OpenAmo(元)；量比=OpenVolPre1(相对前1日量)；撤单率=BCancel/(TotalBVol+BCancel)。竞价强弱≠日内走势。")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        logger.warning("scan_auction_anomaly 工具失败: %s", e)
        return f"竞价异动扫描失败: {str(e)[:100]}"
```

**性能护栏**：`fetch_auction_anomaly` 冷启动会逐票打 TQ（几十秒），因此

- 工具描述里不承诺"秒回"；
- handler 前置 30s 缓存命中判断（函数内部已有），并在 `chat.py` 的 `_TOOL_STAGE_LABELS` 里给它单独的阶段文案「正在扫描全市场竞价异动（约 20–60 秒）…」，避免用户以为卡死；
- 对话侧的路由规则写明：**只有在用户明确问"今天竞价/全市场竞价"时才调用**，个股竞价一律走 `get_auction_data`。

### 5.6 原地增强 `get_decision_pioneer`（不计新增）

```python
# src/core/decision_pioneer.py::decision_pioneer_text 末尾追加一行（唯一改动点）
parts.append(f"共振状态: {d.get('resonance_state')} · {d.get('resonance_action')}")
# 五态之一: 🔥首次共振 / ⭐共振向好 / ⚠️全线走坏 / ⚡警惕背离 / 👀潜伏吸筹
```

**收益**：模型回答「三指标共振」时不再自己编五态，直接引用后端状态机结论；无需改 schema、无需动 inventory guard。补一条断言到既有 `tests/test_decision_pioneer_text.py`（若无则新建）。

---

## 6. 工具编排：让模型"选对工具、说对话"

### 6.1 路由矩阵（写进 `src/web/api/chat.py::SYSTEM_PROMPT`，追加一节）

```
- 数智参谋盘口工具路由(2026-10-18):
  * 「底仓在哪 / 底仓成本 / 筹码集中不集中 / 获利盘多少 / 跌到哪算破位」→ get_chip_accumulation（自算CYQ口径）；
    若用户明确说「筹码峰公式 / SSRP / 主力控盘度」→ get_stock_chips（客户端公式口径）。两口径数字冲突时，
    说明口径差异后**只采用一个口径**作答，禁止混算或取平均。
  * 「撤单多不多 / 有没有假托单 / 买方在诱多吗 / 卖压重不重」→ get_cancel_order_intent（撤单行为）；
    「十档挂单 / 托单压单队列 / 幽灵单」→ get_orderbook（静态挂单）。两者不可互相替代。
  * 「洗盘还是出货 / 弹簧针 / 放量滞涨 / 缩量沉淀 / 突破有效吗」→ get_wyckoff_structure；
    必须保留「弹簧待确认」「放量突破须回踩确认」的不确定性表述，不得说成"已见底/已突破"。
  * 「妖不妖 / 连板身位 / 在不在妖股池」→ get_demon_factor；同时说明这是历史事件画像，非当日热度。
  * 「今天竞价谁爆量 / 全市场竞价异动」→ scan_auction_anomaly（全市场扫描，较慢）；
    「某只票竞价怎么样」→ get_auction_data / 竞价快照。个股竞价**不要**用 scan_auction_anomaly。
  * 一次回答最多调用 5 个工具（MAX_TOOL_ROUNDS=5）；优先「1 个主证据 + 1 个反向验证」的组合，
    不做无差别全量调用。推荐组合:
      - 该不该追 → get_chip_accumulation + get_cancel_order_intent
      - 洗盘还是出货 → get_wyckoff_structure + get_decision_pioneer
      - 能不能成妖 → get_demon_factor + get_auction_data
- 结论一致性: 若工具返回的数字与用户给出的数字冲突，以工具为准并指出差异；**不得**为了顺应用户而改数。
- 参谋结论引用: 用户在个股工作台提问时，system 里会带一段「页面快照」(含 brief 的 verdict)。若快照的 as_of
  超过 5 分钟，必须先调工具刷新再回答，并说明「快照时间 vs 现在」。
```

### 6.2 阶段提示文案（`_TOOL_STAGE_LABELS`，少一条用户就以为卡死）

```python
_TOOL_STAGE_LABELS.update({
    "get_chip_accumulation": "正在计算 120 日底仓筹码沉淀…",
    "get_cancel_order_intent": "正在分析盘口撤单意图…",
    "get_wyckoff_structure": "正在识别威科夫量价结构…",
    "get_demon_factor": "正在读取妖股因子画像…",
    "scan_auction_anomaly": "正在扫描全市场竞价异动（约 20–60 秒）…",
})
```

### 6.3 回答模板（三段式，与卡片结构对齐）

```
【结论】<一句话：tone + intent_label>，置信度 x/10（基于 N/7 项证据）
【依据】• 底仓成本 35.20 / 70%带 33.44–36.96 / 获利盘 99%
        • 买撤 88.9% vs 卖撤 64.7%，判定 ⚠️托单诱多
        • 三指标 ⚠️全线走坏，活跃度 2.69（生命线），L2 净额 -5395 万
【预案】持仓者：…（含具体价位）；空仓者：…
【口径】自算CYQ / TQ撤单 / 三指标(TQ口径)
以上为客观数据推演，不构成投资建议。
```

**为什么强制三段**：与 §4.4 的 `verdict` 字段一一对应，用户从"聊天里的答案"切到"卡片"时看到的是同一套结构，不会出现两套话语体系。

### 6.4 工具结果的"防幻觉"约束

1. 工具返回里的 `⚠️ 口径` 行必须被保留在回答中（哪怕精简，也要写明数据源）。
2. 模型**不得**在工具未返回某字段时补一个数字（例：`open_amount` 缺失时不许写"约 2000 万"）。
3. 工具返回 `available=false` 时，模型必须原样转述原因，不许改写成"暂无数据"这类模糊说法（原因本身是诊断信息）。
4. 涉及"主力意图"的方向性结论，只能来自 `get_main_intent` / `get_cancel_order_intent` / `get_decision_pioneer`；**威科夫工具不得单独支撑方向结论**（其模块头已声明红线）。

---

## 7. 注册、门禁与验收：四道关一次过

### 7.1 注册位置（决定导出顺序，错了必红）

```python
# src/agents/chat/registry.py
# 必须插在下面这行注释【之前】——core 工具是导出清单的连续前缀，
# 插到 thsdk 之后就排到 44+ 位，EXPECTED_CORE_ORDER 的 index 断言直接失败。
# ──────────────── thsdk 11 个高价值工具(2026-08-20 选项 C, W3.3 并入注册表) ────────────────
```

插入后**必须**同步（三处，缺一不可）：

| 文件 | 改动 |
| :--- | :--- |
| `tests/test_w33_chat_registry.py` | `EXPECTED_CORE_ORDER` 末尾追加 5 个名字（顺序 = 注册顺序）；`assert len(CHAT_TOOL_REGISTRY) == 49` → **54** |
| `tests/test_w33_chat_registry.py::test_self_requires_scoping` | 新工具若 `requires="self"` 需同步；本批全部 `requires=""`（公共行情），**不用改** |
| `src/web/api/chat.py::_TOOL_STAGE_LABELS` | 追加 5 条阶段文案（§6.2） |

**自证顺序**（不靠猜）：

```bash
python3 -c "
from src.agents.chat.registry import CHAT_TOOL_REGISTRY
from src.web.api import chat as c
reg=list(CHAT_TOOL_REGISTRY); exp=[t['function']['name'] for t in c.CHAT_TOOLS]
for n in ['get_chip_accumulation','get_cancel_order_intent','get_wyckoff_structure','get_demon_factor','scan_auction_anomaly']:
    print(n, exp.index(n), reg.index(n))
"
# 期望: exp.index 依次 = 31,32,33,34,35；reg 允许整体 +1(handler-only 工具)
```

### 7.2 后端门禁

```bash
pytest tests/test_w33_chat_registry.py -q          # inventory / 顺序 / caliber 非空
pytest tests/test_w41_core_web_dependency.py -q    # 新增 core 模块不得 import src.web
pytest tests/test_copilot_brief.py tests/test_chat_tools_copilot.py -q
python scripts/check_migrations.py                 # 本批无迁移，应为 no-op
python scripts/check_coverage_ratchet.py /tmp/cov.json   # 新增 src/ 模块必须带测试
```

### 7.3 前端门禁

```bash
cd frontend
npx tsc -b
npx eslint src/components/copilot src/pages/AiCopilot.tsx src/hooks/useCopilotBrief.ts
npx vitest run tests/components/copilot-*.test.tsx
cd .. && node scripts/check_ui_rules.mjs           # R6(裸 toFixed) / R12(手调透明度)
```

### 7.4 合规门禁（新增，防文案回归）

```ts
// frontend/tests/components/copilot-copy-contract.test.ts
import { readFileSync } from 'fs'
import { resolve } from 'path'
import { describe, expect, it } from 'vitest'

const SRC = readFileSync(resolve(__dirname, '../../src/lib/copilot/copy.ts'), 'utf-8')
const FORBIDDEN = /建议买入|建议卖出|主力建仓|主力出货|必涨|稳赚|抄底必赚|满仓|梭哈/

describe('参谋文案合规', () => {
  it('不含禁用词', () => {
    const hits = SRC.match(FORBIDDEN)
    expect(hits, `命中禁用词: ${hits}`).toBeNull()
  })
  it('每套预案都带风险提示锚点', () => {
    expect(SRC).toMatch(/不构成投资建议/)
  })
  it('底仓成本一律用"底仓成本/主力成本"，不用"建仓成本"', () => {
    expect(SRC).not.toMatch(/建仓成本/)
  })
})
```

> 该测试只扫 `copy.ts`（所有面向用户的文案都收敛在这一个文件里，便于机器判定）——这也是**为什么**文案不散落在各个组件里。

---

## 8. 前端组件结构（TSX）

### 8.1 文件清单与职责

```
frontend/src/lib/copilot/
  types.ts            # 与后端 Pydantic 一一对应（见 §4.5）
  api.ts              # getCopilotBrief / getCopilotPresets（走 fetchAPI，自带 /api 前缀）
  copy.ts             # 全部面向用户的文案 + 合规尾注（唯一文案源，被 compliance test 扫描）
  tone.ts             # tone → 令牌/图标映射（纯函数，可单测）
  symbolChips.ts      # 从回答正文抽取 6 位代码 → 可点击跳板（纯函数）
frontend/src/hooks/
  useCopilotBrief.ts  # 取数 + abort + 手动刷新 + 降级状态
  useCopilotChat.ts   # /ai-copilot 页的会话 runner（复用 chatApi 的 SSE 封装）
frontend/src/components/copilot/
  CopilotBriefCard.tsx     # 主卡（三段式），SuggestTab 与 /ai-copilot 共用
  CopilotIntentBlock.tsx   # 【主力意图定性】
  CopilotDefenseBlock.tsx  # 【关键防守价位】
  CopilotPlanBlock.tsx     # 【操盘行动预案】
  CopilotEvidenceDrawer.tsx# 证据链抽屉（7 块逐项 + 口径 + 降级原因）
  CopilotSkeleton.tsx      # 骨架屏（禁 spinner 抖整页）
  CopilotCapsule.tsx       # 工作台右下角常驻胶囊
frontend/src/pages/
  AiCopilot.tsx            # /ai-copilot 一级页
```

**组件树**

```
CopilotBriefCard (data-testid="copilot-brief")
├── Header        标的 + tone 徽章 + 置信度 + as_of + [刷新]
├── CopilotIntentBlock    intent_label / narrative / risk_flags
├── CopilotDefenseBlock   support[] / resistance[]（最多 4 行）
├── CopilotPlanBlock      holder / empty 两栏
├── EvidenceStrip         7 个证据小灯（●available ○missing ⊘gated）+ [查看证据链]
├── CopilotEvidenceDrawer （懒挂载，点开才渲染）
└── Footer                caliber_notes（折叠） + disclaimer + version
```

### 8.2 `useCopilotBrief.ts`

```ts
import { useCallback, useEffect, useRef, useState } from 'react'
import { getCopilotBrief } from '@/lib/copilot/api'
import type { CopilotBrief } from '@/lib/copilot/types'

type State = {
  data: CopilotBrief | null
  loading: boolean
  error: string | null   // 事实性错误原文，不改写、不猜原因
}

/** 参谋卡取数：带 abort、带手动刷新、带"过期快照"标记（stale 由后端给，前端不自己算）。 */
export function useCopilotBrief(symbol: string, market = 'CN', opts?: { enabled?: boolean; timeoutMs?: number }) {
  const enabled = opts?.enabled !== false
  const timeoutMs = opts?.timeoutMs ?? 12000
  const [state, setState] = useState<State>({ data: null, loading: enabled, error: null })
  const abortRef = useRef<AbortController | null>(null)

  const load = useCallback(
    async (refresh = false) => {
      if (!enabled || !symbol) return
      abortRef.current?.abort()
      const ac = new AbortController()
      abortRef.current = ac
      const timer = setTimeout(() => ac.abort(), timeoutMs)
      setState((s) => ({ ...s, loading: true, error: null }))
      try {
        const data = await getCopilotBrief(symbol, market, refresh, ac.signal)
        if (!ac.signal.aborted) setState({ data, loading: false, error: null })
      } catch (e: any) {
        if (ac.signal.aborted) {
          setState((s) => ({ ...s, loading: false, error: s.data ? null : '请求超时（12s）' }))
        } else {
          setState((s) => ({ ...s, loading: false, error: e?.message || '请求失败' }))
        }
      } finally {
        clearTimeout(timer)
      }
    },
    [symbol, market, enabled, timeoutMs],
  )

  useEffect(() => {
    void load(false)
    return () => abortRef.current?.abort()
  }, [load])

  return { ...state, reload: () => load(true) }
}
```

`api.ts`：

```ts
import { fetchAPI } from '@sida/api'
import type { CopilotBrief } from './types'

/** fetchAPI 自带 /api 前缀 → 这里写 /copilot/... 而非 /api/copilot/... */
export const getCopilotBrief = (symbol: string, market = 'CN', refresh = false, signal?: AbortSignal) =>
  fetchAPI<CopilotBrief>(
    `/copilot/brief/${encodeURIComponent(symbol)}?market=${encodeURIComponent(market)}${refresh ? '&refresh=true' : ''}`,
    { signal, cacheMode: refresh ? 'reload' : undefined },
  )
```

### 8.3 `copy.ts`（唯一文案源）

```ts
import type { Tone } from './types'

export const DISCLAIMER = '以上为盘口与筹码数据的客观推演，不构成投资建议。'

export const TONE_META: Record<Tone, { label: string; badge: string; bar: string; icon: string }> = {
  opportunity: { label: '机会', badge: 'bg-role-opp/15 text-role-opp border-role-opp/40', bar: 'bg-role-opp', icon: '▲' },
  neutral:     { label: '中性', badge: 'bg-s3 text-muted-foreground border-border/60',        bar: 'bg-role-system', icon: '＝' },
  caution:     { label: '谨慎', badge: 'bg-role-watch/15 text-role-watch border-role-watch/40', bar: 'bg-role-watch', icon: '!' },
  risk:        { label: '风险', badge: 'bg-role-risk/15 text-role-risk border-role-risk/40',   bar: 'bg-role-risk', icon: '▼' },
}

export const DEFENSE_LABEL: Record<string, string> = {
  first: '第一防守',
  core: '核心生命线',
  probe: '弹簧针低点',
  cost: '底仓成本',
  cap: '强压制',
}

export const AUDIENCE_LABEL = { holder: '持仓者', empty: '空仓者' } as const

export const EVIDENCE_NAME: Record<string, string> = {
  chips: '底仓筹码',
  resonance: '三指标共振',
  microstructure: '盘口撤单',
  wyckoff: '量价结构',
  auction: '竞价异动',
  demon: '妖股因子',
  theme: '主线身位',
}

/** 降级时的诚实文案：原因来自后端，不在这里编 */
export const DEGRADED_HINT = (reason: string | null) => (reason ? `暂无：${reason}` : '暂无数据')
export const GATED_HINT = '需要更高权限，升级后可见'
export const SNAPSHOT_HINT = (d: string | null) => (d ? `收盘快照 ${d}` : '收盘快照')
```

> 注意：`bg-role-opp/15` 这类**修饰符**在新代码里是可用的（tailwind 已注册 `hsl(var(--x) / <alpha-value>)`），但**背景色阶（s0–s4）不得手调透明度**（R12 棘轮）——卡片容器用实色 `bg-s1`/`bg-s2`。

### 8.4 `CopilotBriefCard.tsx`（主卡，核心 TSX）

```tsx
import { useState } from 'react'
import { ChevronDown, RefreshCw, ShieldAlert, Target } from 'lucide-react'
import { safeFixed, safeNum } from '@/lib/format'
import { useCopilotBrief } from '@/hooks/useCopilotBrief'
import { CopilotIntentBlock } from './CopilotIntentBlock'
import { CopilotDefenseBlock } from './CopilotDefenseBlock'
import { CopilotPlanBlock } from './CopilotPlanBlock'
import { CopilotEvidenceDrawer } from './CopilotEvidenceDrawer'
import { CopilotSkeleton } from './CopilotSkeleton'
import { DISCLAIMER, TONE_META, EVIDENCE_NAME, DEGRADED_HINT } from '@/lib/copilot/copy'
import type { CopilotBrief } from '@/lib/copilot/types'

const EVIDENCE_KEYS = ['chips', 'resonance', 'microstructure', 'wyckoff', 'auction', 'demon', 'theme'] as const

export function CopilotBriefCard({
  symbol,
  market = 'CN',
  hasPosition,
  compact = false,
}: {
  symbol: string
  market?: string
  hasPosition?: boolean
  /** compact=true 用于 /ai-copilot 右栏与抽屉：隐藏证据抽屉入口与口径折叠 */
  compact?: boolean
}) {
  const { data, loading, error, reload } = useCopilotBrief(symbol, market)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [caliberOpen, setCaliberOpen] = useState(false)

  if (loading && !data) return <CopilotSkeleton rows={compact ? 3 : 5} />
  if (error && !data) {
    return (
      <div className="rounded-xl border border-border/60 bg-s1 p-3 text-[12px]" data-testid="copilot-brief-error">
        <div className="text-muted-foreground">参谋卡片暂时取不到数据：{error}</div>
        <button
          type="button"
          onClick={() => void reload()}
          className="mt-2 inline-flex h-6 items-center gap-1 rounded border border-border/50 px-2 text-[11px] hover:text-foreground"
        >
          <RefreshCw className="h-3 w-3" /> 重试
        </button>
      </div>
    )
  }
  if (!data) return null

  const v = data.verdict
  const tone = TONE_META[v.intent_tone] ?? TONE_META.neutral
  const used = v.evidence_used.length
  const total = EVIDENCE_KEYS.length
  const missing = v.evidence_missing

  return (
    <div
      className="rounded-xl border border-border/60 bg-s1"
      data-testid="copilot-brief"
      data-tone={v.intent_tone}
      data-rule={v.rule_id}
      data-evidence-used={used}
      data-as-of={data.as_of}
    >
      {/* 左角色条：只表达角色，不用涨跌色冒充 */}
      <div className="flex">
        <div className={`w-[3px] shrink-0 rounded-l-xl ${tone.bar}`} aria-hidden />
        <div className="min-w-0 flex-1 p-3">
          {/* Header */}
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="text-[13px] font-medium">
              {data.name || data.symbol}
              <span className="ml-1 font-mono text-[11px] text-muted-foreground">{data.symbol}</span>
            </span>
            <span className={`rounded border px-1.5 py-[1px] text-[10px] ${tone.badge}`} data-testid="copilot-tone-badge">
              {tone.icon} {tone.label}
            </span>
            <span className="text-[11px] text-muted-foreground" data-testid="copilot-confidence">
              置信 {safeFixed(v.confidence * 10, 1)}/10 · 证据 {used}/{total}
            </span>
            {data.session.stale ? (
              <span className="rounded bg-s3 px-1.5 py-[1px] text-[10px] text-muted-foreground">
                快照 {data.trade_date || '--'}
              </span>
            ) : null}
            <button
              type="button"
              onClick={() => void reload()}
              disabled={loading}
              title="重新计算（跳过 20s 进程缓存）"
              className="ml-auto inline-flex h-6 w-6 items-center justify-center rounded border border-border/50 text-muted-foreground hover:text-foreground disabled:opacity-50"
              data-testid="copilot-refresh"
            >
              <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
            </button>
          </div>

          {/* 证据不足横幅：独立一行，绝不塞进可选块 */}
          {missing.length >= 2 ? (
            <div
              className="mt-2 flex items-start gap-1.5 rounded border border-border/60 bg-s2 px-2 py-1.5 text-[11px] text-muted-foreground"
              data-testid="copilot-degraded-banner"
            >
              <ShieldAlert className="mt-[1px] h-3 w-3 shrink-0 text-role-watch" />
              <span>
                本次仅 {used}/{total} 项证据可用（缺 {missing.map((k) => EVIDENCE_NAME[k] || k).join('、')}），
                结论置信度已下调，价位仅供参考。
              </span>
            </div>
          ) : null}

          <CopilotIntentBlock verdict={v} className="mt-2.5" />
          <CopilotDefenseBlock lines={v.defense_lines} close={data.quote.close} className="mt-3" />
          <CopilotPlanBlock
            plans={v.action_plans}
            hasPosition={hasPosition}
            className="mt-3"
          />

          {/* 证据小灯 */}
          <div className="mt-3 flex flex-wrap items-center gap-x-2 gap-y-1 border-t border-border/40 pt-2 text-[10px] text-muted-foreground">
            {EVIDENCE_KEYS.map((k) => {
              const ev = data.evidence[k]
              const dot = !ev.available ? (ev.gated ? '◍' : '○') : '●'
              const cls = !ev.available ? 'text-muted-foreground/60' : 'text-role-watch'
              return (
                <span key={k} className={cls} title={!ev.available ? DEGRADED_HINT(ev.degraded_reason) : ev.caliber}>
                  {dot} {EVIDENCE_NAME[k]}
                </span>
              )
            })}
            {!compact ? (
              <button
                type="button"
                onClick={() => setDrawerOpen(true)}
                className="ml-auto inline-flex items-center gap-0.5 hover:text-foreground"
                data-testid="copilot-open-evidence"
              >
                查看证据链 <ChevronDown className="h-3 w-3" />
              </button>
            ) : null}
          </div>

          {/* 口径折叠 + 合规尾注 */}
          {!compact ? (
            <div className="mt-1.5">
              <button
                type="button"
                onClick={() => setCaliberOpen((x) => !x)}
                className="text-[10px] text-muted-foreground hover:text-foreground"
              >
                口径说明 {caliberOpen ? '▾' : '▸'}
              </button>
              {caliberOpen ? (
                <ul className="mt-1 space-y-0.5 pl-3 text-[10px] text-muted-foreground">
                  {v.caliber_notes.map((c) => (
                    <li key={c}>· {c}</li>
                  ))}
                </ul>
              ) : null}
            </div>
          ) : null}

          <div className="mt-1.5 flex items-center gap-1 text-[10px] text-muted-foreground/80" data-testid="copilot-disclaimer">
            <Target className="h-2.5 w-2.5" />
            {DISCLAIMER}
            <span className="ml-auto font-mono">{data.as_of.slice(11, 16)}</span>
          </div>
        </div>
      </div>

      {!compact ? <CopilotEvidenceDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)} brief={data} /> : null}
    </div>
  )
}
```

### 8.5 `CopilotDefenseBlock.tsx`（防守位：只渲染有数的，缺就不占位）

```tsx
import { safeFixed, safeNum } from '@/lib/format'
import { DEFENSE_LABEL } from '@/lib/copilot/copy'
import type { DefenseLine } from '@/lib/copilot/types'

export function CopilotDefenseBlock({
  lines,
  close,
  className = '',
}: { lines: DefenseLine[]; close: number | null; className?: string }) {
  if (!lines.length) {
    return (
      <div className={className} data-testid="copilot-defense-empty">
        <div className="text-[11px] font-medium text-muted-foreground">关键防守价位</div>
        <div className="mt-1 text-[11px] text-muted-foreground">筹码与盘口价位不足，本次不给防守线（不猜价位）。</div>
      </div>
    )
  }
  const supports = lines.filter((l) => l.side === 'support')
  const resists = lines.filter((l) => l.side === 'resistance')
  return (
    <div className={className} data-testid="copilot-defense">
      <div className="text-[11px] font-medium text-muted-foreground">
        关键防守价位
        {close != null ? <span className="ml-1 font-mono text-foreground/80">现价 {safeFixed(close, 2)}</span> : null}
      </div>
      <div className="mt-1 divide-y divide-border/30">
        {[...supports, ...resists].map((l) => (
          <div key={`${l.slot}-${l.price}`} className="flex items-baseline gap-2 py-1 text-[11px]" data-testid="copilot-defense-row" data-slot={l.slot}>
            <span className={`w-[52px] shrink-0 ${l.side === 'support' ? 'text-muted-foreground' : 'text-role-risk'}`}>
              {l.slot === 'cap' ? '压制' : '防守'}
            </span>
            <span className="w-[72px] shrink-0 font-mono text-[12px] text-foreground">{safeFixed(l.price, 2)}</span>
            <span className="shrink-0 text-[10px] text-muted-foreground">
              {safeNum(l.distance_pct) != null ? `${safeFixed(l.distance_pct, 2)}%` : '--'}
            </span>
            <span className="min-w-0 truncate text-[10px] text-muted-foreground" title={`${DEFENSE_LABEL[l.slot]} · ${l.basis}`}>
              {DEFENSE_LABEL[l.slot]} · {l.basis}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
```

### 8.6 `CopilotPlanBlock.tsx`（预案：持仓 / 空仓两栏，按用户持仓状态置顶）

```tsx
import { safeFixed } from '@/lib/format'
import { AUDIENCE_LABEL } from '@/lib/copilot/copy'
import type { ActionPlan } from '@/lib/copilot/types'

const ACTION_TAG: Record<ActionPlan['action'], string> = {
  reduce: '减仓防守',
  hold: '持有观察',
  defend: '守住关键位',
  wait: '等待确认',
  probe: '分批试探',
  avoid: '不参与',
}

export function CopilotPlanBlock({
  plans,
  hasPosition,
  className = '',
}: { plans: ActionPlan[]; hasPosition?: boolean; className?: string }) {
  if (!plans.length) return null
  // 用户持仓状态已知 → 对应那栏排前面（不知道就不排序，保持后端顺序）
  const ordered = hasPosition === undefined ? plans : [...plans].sort((a, b) =>
    (a.audience === 'holder' ? 0 : 1) === (hasPosition ? 0 : 1) ? -1 : 1,
  )
  return (
    <div className={className} data-testid="copilot-plans">
      <div className="text-[11px] font-medium text-muted-foreground">操盘行动预案</div>
      <div className="mt-1 space-y-1.5">
        {ordered.map((p) => (
          <div key={p.audience} className="rounded border border-border/50 bg-s2 px-2 py-1.5" data-testid={`copilot-plan-${p.audience}`}>
            <div className="flex items-center gap-1.5">
              <span className="text-[11px] text-foreground">{AUDIENCE_LABEL[p.audience]}</span>
              <span className="rounded bg-s3 px-1 py-[0.5px] text-[10px] text-muted-foreground">{ACTION_TAG[p.action]}</span>
            </div>
            <div className="mt-0.5 text-[11px] leading-relaxed text-foreground/90">{p.text}</div>
            {p.triggers.length ? (
              <div className="mt-1 flex flex-wrap gap-1">
                {p.triggers.map((t, i) => (
                  <span key={i} className="rounded border border-border/50 px-1 py-[0.5px] text-[10px] text-muted-foreground">
                    {t.price != null ? `${safeFixed(t.price, 2)} ` : ''}{t.note}
                  </span>
                ))}
              </div>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  )
}
```

> ⚠️ 该组件**必须**从 `@/lib/format` 引入 `safeFixed`（已在文件头 import）；裸 `.toFixed()` 会被 `check_ui_rules.mjs` R6 棘轮拦下，且非数会渲染成 `NaN`。

### 8.7 `CopilotCapsule.tsx`（右下角常驻胶囊 + 零输入唤醒）

**核心机制（不新写一套聊天）**：胶囊只做两件事 —— ① 轻量展示"当前标的 tone + 一句话结论"；② 点击时 `dispatchEvent('sida-open-chat', { detail })`，由既有 `ChatWidget` 创建带 `initial_context` 的新会话（`ChatWidget.tsx:188-213` 已实现）。**零新增聊天通道、零 SSE 重复实现。**

```tsx
import { useEffect, useState } from 'react'
import { Sparkles } from 'lucide-react'
import { useCopilotBrief } from '@/hooks/useCopilotBrief'
import { TONE_META } from '@/lib/copilot/copy'

const QUICK_QUESTIONS = [
  '主力底仓成本在什么位置？现在获利盘多少？',
  '买方是不是在挂假托单？撤单率说明了什么？',
  '跌到多少必须止损？关键防守位在哪？',
]

export function CopilotCapsule({
  symbol,
  market = 'CN',
  stockName,
}: { symbol: string; market?: string; stockName?: string }) {
  const { data } = useCopilotBrief(symbol, market)
  const [hidden, setHidden] = useState(false)

  // 记忆用户是否收起胶囊（按标的维度，避免每次进页面都弹）
  useEffect(() => {
    setHidden(localStorage.getItem(`copilot_capsule_hidden:${symbol}`) === '1')
  }, [symbol])

  if (hidden) return null
  const tone = data ? TONE_META[data.verdict.intent_tone] : null

  const open = () => {
    // 页面快照：把 brief 的关键结论压成一段文本，随会话创建注入 system prompt
    const snapshot = data
      ? [
          `[数智参谋快照 ${data.as_of}] ${data.symbol} ${data.name || ''}`,
          `现价 ${data.quote.close ?? '--'}（${data.quote.change_pct ?? '--'}%）`,
          `意图：${data.verdict.intent_label}（${data.verdict.intent_tone}，置信 ${data.verdict.confidence.toFixed(2)}）`,
          `底仓成本 ${data.evidence.chips.main_cost ?? '--'}；70%带 ${data.evidence.chips.cost_70_low ?? '--'}–${data.evidence.chips.cost_70_high ?? '--'}；获利盘 ${data.evidence.chips.profit_ratio ?? '--'}%`,
          `三指标：${data.evidence.resonance.resonance_state ?? '--'}；机构活跃度 ${data.evidence.resonance.activity ?? '--'}`,
          `撤单：买撤 ${data.evidence.microstructure.b_cancel_ratio != null ? (data.evidence.microstructure.b_cancel_ratio * 100).toFixed(1) : '--'}% / 卖撤 ${data.evidence.microstructure.s_cancel_ratio != null ? (data.evidence.microstructure.s_cancel_ratio * 100).toFixed(1) : '--'}%`,
          `常用追问：${QUICK_QUESTIONS.join(' | ')}`,
          '注意：以上为快照，若用户询问当前实时值，必须调用工具重新取数。',
        ].join('\n')
      : `[页面] 个股 ${symbol}，用户从工作台胶囊进入，尚无参谋快照。`

    window.dispatchEvent(
      new CustomEvent('sida-open-chat', {
        detail: { symbol, market, stockName: stockName || '', pageContext: snapshot },
      }),
    )
  }

  return (
    <div className="fixed right-4 bottom-36 z-40 md:bottom-20" data-testid="copilot-capsule">
      <div className="flex items-stretch overflow-hidden rounded-full border border-role-watch/40 bg-s2 shadow-float">
        <button
          type="button"
          onClick={open}
          className="flex h-9 items-center gap-1.5 px-3 text-[12px] text-foreground hover:bg-s3"
          title="打开盘口参谋（已预注入当前标的上下文）"
        >
          <Sparkles className="h-3.5 w-3.5 text-role-watch" />
          <span className="max-w-[132px] truncate">
            {tone ? `${tone.icon} ${data?.verdict.intent_label}` : `盘口参谋 · ${symbol}`}
          </span>
        </button>
        <button
          type="button"
          aria-label="收起参谋胶囊"
          onClick={() => {
            localStorage.setItem(`copilot_capsule_hidden:${symbol}`, '1')
            setHidden(true)
          }}
          className="flex h-9 w-7 items-center justify-center border-l border-border/40 text-[11px] text-muted-foreground hover:text-foreground"
        >
          ×
        </button>
      </div>
    </div>
  )
}
```

**布局避让**（必须实测，不能只看代码）：ChatWidget 的 FAB 是 `bottom-20 right-4 md:bottom-5`（48×48）。胶囊放 `bottom-36 right-4 md:bottom-20`（144px/80px），在移动端与桌面端都在 FAB 正上方且留 12px 空隙。上线前用 DOM 探针断言两元素 `getBoundingClientRect()` 不相交：

```ts
const a = screen.getByTestId('copilot-capsule').getBoundingClientRect()
const b = screen.getByTestId('chat-fab').getBoundingClientRect()   // 若 FAB 无 testid，先补一个
expect(a.bottom <= b.top || a.top >= b.bottom).toBe(true)
```

**收起后可恢复**：`hidden` 状态只影响当前标的；工作台工具栏提供「✨ 参谋」按钮重新显示（写 `localStorage.removeItem`），否则用户一旦误点就永久失去入口（这是常驻组件最常见的可用性事故）。

### 8.8 `AiCopilot.tsx`（一级页 /ai-copilot）

```tsx
import { useEffect, useRef, useState } from 'react'
import { Loader2, Send } from 'lucide-react'
import { chatApi, fetchAPI } from '@sida/api'
import SafeMarkdown from '@/components/SafeMarkdown'
import { CopilotBriefCard } from '@/components/copilot/CopilotBriefCard'
import { copilotPresets } from '@/lib/copilot/api'
import { extractSymbolChips } from '@/lib/copilot/symbolChips'
import type { CopilotPreset } from '@/lib/copilot/types'

type Msg = { role: 'user' | 'assistant'; content: string }

export default function AiCopilotPage() {
  const [presets, setPresets] = useState<CopilotPreset[]>([])
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [stage, setStage] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [convId, setConvId] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const scroller = useRef<HTMLDivElement>(null)

  useEffect(() => {
    void copilotPresets().then(setPresets).catch(() => setPresets([]))
  }, [])

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight })
  }, [msgs, stage])

  const send = async (text: string) => {
    const content = text.trim()
    if (!content || streaming) return
    setError(null)
    setMsgs((m) => [...m, { role: 'user', content }])
    setInput('')
    setStreaming(true)
    setStage('正在准备上下文…')
    let assistant = ''
    try {
      let id = convId
      if (!id) {
        const conv = await chatApi.createConversation({ initial_context: '用户从【数智参谋】一级页进入，尚未指定标的。' })
        id = conv.id
        setConvId(id)
      }
      const ac = new AbortController()
      abortRef.current = ac
      await chatApi.sendMessageStream(
        id,
        content,
        {
          onStage: (s) => setStage(s),
          onDelta: (d) => {
            assistant += d
            setStage('')
            setMsgs((m) => {
              const last = m[m.length - 1]
              if (last?.role === 'assistant') return [...m.slice(0, -1), { role: 'assistant', content: assistant }]
              return [...m, { role: 'assistant', content: assistant }]
            })
          },
          onError: (e) => setError(e),
        },
        ac.signal,
      )
    } catch (e: any) {
      setError(e?.message || '对话失败')
    } finally {
      setStreaming(false)
      setStage('')
    }
  }

  return (
    <div className="flex h-[calc(100vh-9rem)] gap-3" data-testid="ai-copilot-page">
      {/* 左：战术指令池 */}
      <aside className="hidden w-[248px] shrink-0 flex-col gap-2 lg:flex">
        <div className="text-[12px] font-medium text-muted-foreground">战术指令</div>
        <div className="space-y-1.5">
          {presets.map((p) => (
            <button
              key={p.id}
              type="button"
              onClick={() => void send(p.prompt)}
              disabled={streaming}
              className="w-full rounded-lg border border-border/60 bg-s1 px-2.5 py-2 text-left text-[12px] hover:bg-s2 disabled:opacity-60"
            >
              <span className="mr-1">{p.icon}</span>
              {p.label}
            </button>
          ))}
          {!presets.length ? (
            <div className="rounded-lg border border-border/50 px-2.5 py-2 text-[11px] text-muted-foreground">
              指令池暂不可用（接口失败），可直接在右侧提问。
            </div>
          ) : null}
        </div>
        <div className="mt-auto text-[10px] leading-relaxed text-muted-foreground/80">
          所有结论均来自盘口/筹码/资金真实数据，涉及买卖倾向的表述仅供参考，不构成投资建议。
        </div>
      </aside>

      {/* 右：交互工作台 */}
      <section className="flex min-w-0 flex-1 flex-col rounded-xl border border-border/60 bg-s1">
        <div className="flex items-center gap-2 border-b border-border/40 px-3 py-2">
          <span className="text-[12px] font-medium">数智参谋</span>
          <span className="text-[10px] text-muted-foreground">筹码 · 共振 · 量价 · 盘口 · 竞价 · 妖股因子 全量穿透</span>
          {convId ? <span className="ml-auto font-mono text-[10px] text-muted-foreground">#{convId}</span> : null}
        </div>

        <div ref={scroller} className="scrollbar flex-1 space-y-3 overflow-y-auto px-3 py-3">
          {!msgs.length ? (
            <div className="mt-6 space-y-2 text-[12px] text-muted-foreground">
              <div>直接问，或点左侧战术指令。例：</div>
              <ul className="space-y-1 pl-4">
                <li>· 今天 9:25 谁在爆量抢筹？</li>
                <li>· 600111 底仓成本多少，现在追安全吗？</li>
                <li>· 我的持仓里谁已经跌破 70% 筹码下沿？</li>
              </ul>
            </div>
          ) : null}
          {msgs.map((m, i) => (
            <div key={i} className={m.role === 'user' ? 'text-right' : ''}>
              <div
                className={
                  m.role === 'user'
                    ? 'inline-block max-w-[86%] rounded-lg bg-s3 px-2.5 py-1.5 text-[12px]'
                    : 'rounded-lg border border-border/50 bg-s2 px-2.5 py-2 text-[12px] leading-relaxed'
                }
              >
                {m.role === 'user' ? m.content : <SafeMarkdown content={m.content} />}
                {m.role === 'assistant'
                  ? extractSymbolChips(m.content).map((s) => (
                      <a key={s} href={`/stocks/${s}`} className="mr-1 mt-1 inline-block rounded border border-role-watch/40 px-1.5 text-[11px] text-role-watch">
                        {s} 工作台 ↗
                      </a>
                    ))
                  : null}
              </div>
            </div>
          ))}
          {stage ? (
            <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" />
              {stage}
            </div>
          ) : null}
          {error ? <div className="rounded border border-role-risk/40 px-2 py-1.5 text-[11px] text-role-risk">{error}</div> : null}
        </div>

        <div className="border-t border-border/40 p-2">
          <div className="flex items-end gap-2">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault()
                  void send(input)
                }
              }}
              rows={2}
              placeholder="问筹码、共振、量价、撤单、竞价、妖股因子…（Enter 发送 / Shift+Enter 换行）"
              className="scrollbar min-h-[44px] flex-1 resize-none rounded-lg border border-border/60 bg-s0 px-2 py-1.5 text-[12px] outline-none focus:border-role-watch/60"
            />
            <button
              type="button"
              onClick={() => void send(input)}
              disabled={streaming || !input.trim()}
              className="flex h-9 w-9 items-center justify-center rounded-lg bg-role-watch/15 text-role-watch hover:bg-role-watch/25 disabled:opacity-40"
              aria-label="发送"
            >
              {streaming ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
            </button>
          </div>
        </div>
      </section>
    </div>
  )
}
```

> `chatApi.sendMessageStream(id, content, handlers, signal)` 的现有签名见 `frontend/packages/api/src/chat.ts:96-142`（已支持 `signal`）；**不要**另写一份 SSE 解析。
> `extractSymbolChips(text)` 是纯函数：`text.match(/\b(6\d{5}|0\d{5}|3\d{5}|8\d{5}|4\d{5})\b/g)` 去重、剔除已是链接的、上限 6 个 —— 单测覆盖"不把 2026 年 10 月 18 日当代码"这类误判（用前后中文/数字边界断言）。

### 8.9 `SuggestTab.tsx` 集成（原位升级，不删旧列表）

```tsx
{/* 本土三层滤网决策行之后、建议列表之前插入参谋卡 */}
<CopilotBriefCard symbol={symbol} market={market} hasPosition={hasPosition} />

<ThreeTierVerdictRow symbol={symbol} market={market} />
<SuggestionsTab />
```

**为什么不替换 `SuggestionsTab`**：后者承载「AI 建议池 / 过期开关 / 盘中监测触发 / 技术指标回退」四条既有能力，替换会破坏已有契约测试与 KI-058 的边界。参谋卡是**加在最上面的一层结论**，旧列表退居为"历史建议流水"——这也符合"副驾驶先说结论，日志留在下面"的产品直觉。

同时给 `SuggestTab` 的 `keys` 保持 `['suggestions','core']` 不变（参谋卡自带取数，不经过 `InsightProvider`，避免把它拖进 `core` 的 5 个端点串行链）。

---

## 9. 入口与导航：把 AI 从"小图标"升级为"第一入口"

### 9.1 三处入口，一条心智

| 入口 | 位置 | 形态 | 触发后的落地 |
| :--- | :--- | :--- | :--- |
| **A. 一级导航** | 左侧栏第 2 组（行情之后、机会之前），独立高亮块 | `🤖 数智参谋` + 微光徽章 + `⌘J` 提示 | `/ai-copilot` 全屏工作台 |
| **B. 顶栏快捷** | 头部 `NotificationBell` 左侧 | 图标按钮（`Sparkles`）+ 呼吸点 | `/ai-copilot`（保留当前页为工作台页签） |
| **C. 工作台胶囊** | `/stocks/:symbol` 右下角 | 玻璃态胶囊，实时显示 tone 与一句话结论 | 唤起 `ChatWidget`（预注入 brief 快照） |

### 9.2 `App.tsx` 具体改动（4 处，全部为增量）

```tsx
// ① navItems 追加（放在 /auction 之后，保持"机会域"相邻语义）
{ to: '/ai-copilot', icon: Sparkles, labelKey: 'nav.aiCopilot', perm: 'view_quotes' },

// ② desktopNavGroups：新增一组，且用独立形态渲染（见 9.3 的分组数说明）
{ key: 'ai', labelKey: 'nav.aiCopilot', items: navItems.filter(n => n.to === '/ai-copilot') },

// ③ 路由（与 /stocks/:symbol 同级，需登录）
<Route path="/ai-copilot" element={<PermGuard perm="view_quotes" myPerms={myPerms}><AiCopilotPage /></PermGuard>} />

// ④ 工作台挂胶囊（仅个股分支，指数分支不挂 —— 指数没有筹码/撤单证据）
//    frontend/src/pages/StockWorkbench.tsx 末尾、</div> 之前：
{!isIndex ? <CopilotCapsule symbol={symbol} market={MARKET} stockName={stockName} /> : null}
```

i18n（`src/locales/zh-CN.ts` / `en-US.ts` 的 `nav` 对象内）：

```ts
aiCopilot: '数智参谋',        // en: 'AI Copilot'
```

### 9.3 分组数超限的处理（必须拍板，别偷偷改）

设计稿约束「一级导航 ≤ 6 组」，当前正好 6 组（market / opportunity / decision / research / mine / system）。新增 `ai` 组会变成 7 组。

**推荐方案（A）**：`ai` 组不参与折叠菜单，而渲染成**独立的顶部高亮卡**（`bg-s2` + `border-role-watch/40` + 图标 + 两行副标题），视觉上不属于"组"，因此仍是 6 组 + 1 个固定入口。理由：AI 是产品的第一心智，塞进"机会"组会被当成第 4 个子项，等于没做入口。

**兜底方案（B）**：把 `/ai-copilot` 作为 `opportunity` 组的**首项**（`opportunities / ai-copilot / auction / dark-fund-top / war-report`），组数不变，靠 `⌘J` 与顶栏按钮补可见度。

两方案都需在发版说明里写清；**不允许**两种同时上线（会出现两个入口，用户以为重复）。

### 9.4 快捷键与移动端

- 全局 `⌘/Ctrl + J` → `/ai-copilot`（`CommandPalette` 已有快捷键注册表，追加一条即可）。
- 移动端底部 5 槽**不新增**（槽位已满：首页/持仓/机会/行情/通知）；改为在移动端顶栏放 `Sparkles` 图标，并在个股工作台保留胶囊（`bottom-36` 在移动端已避让底栏）。
- 移动端胶囊宽度上限 `max-w-[132px]`，结论文字 `truncate`，避免遮挡 K 线。

---

## 10. 视觉与交互规范（暗色优先）

### 10.1 令牌使用（只用这些，禁止手调）

| 用途 | 令牌 | 说明 |
| :--- | :--- | :--- |
| 卡片容器 | `bg-s1` | 比页面底色深一档，形成"浮起"（不用阴影） |
| 卡片内分区 | `bg-s2` / `bg-s3` | 预案块、徽章底 |
| 角色色条 | `bg-role-opp / role-watch / role-risk` | 只做左 3px 色条与徽章，**绝不**用于价格涨跌 |
| 涨跌 | `text-stock-up / text-stock-down` | 仅价格数字与 K 线 |
| 阴影 | `.shadow-float`（唯一一档） | 仅胶囊与 Drawer |
| 圆角 | `rounded-xl`（卡片）/ `rounded-lg`（块）/ `rounded-full`（胶囊） | 不超过 `xl` |
| 动效 | `--dur-*` + `--ease-*` | 刷新按钮旋转、胶囊 hover；`prefers-reduced-motion` 已在 token 层归零，无需组件里再判断 |

### 10.2 信息密度（对齐工作台既有 11–12px 正文）

- 卡片标题 13px，正文 11px，脚注 10px；
- 防守位每行高度 ≤ 24px，最多 4 行（超出截断，进抽屉看全量）；
- 预案两栏各 ≤ 3 行文字（长句拆成 `text` + `triggers`）；
- **一屏内必须能同时看到**：tone、意图一句话、三条价位、两条预案。看不到＝设计失败。

### 10.3 三态设计（加载 / 降级 / 空）

| 态 | 表现 | 判据 |
| :--- | :--- | :--- |
| 加载 | `CopilotSkeleton`：3–5 行灰条，**不**整页 spinner | `loading && !data` |
| 部分降级 | 证据灯变空心 + 顶部横幅 + 置信度已下调 | `evidence_missing.length >= 2` |
| 权限受限 | 该块显示「升级解锁」按钮（链到 `/tiers`），**不显示 `--`** | `gated === true` |
| 完全无数据 | 整卡显示一行「参谋数据暂不可用：{reason}」+ 重试 | `evidence_used.length === 0` |
| 过期 | 头部 `快照 10-17` 角标 | `session.stale` |

### 10.4 可访问性

- 色条只做辅助，tone 必须有**文字**（机会/中性/谨慎/风险）+ 图标（▲/＝/!/▼），色盲可读；
- 刷新按钮 `aria-label="重新计算参谋结论"`；
- Drawer 用既有 `Dialog`（已带 focus trap 与 Esc）；
- 胶囊收起按钮 `aria-label="收起参谋胶囊"`，且提供恢复入口（§8.7）。

---

## 11. 性能预算与可观测

| 指标 | 预算 | 观测点 |
| :--- | :--- | :--- |
| `GET /brief` P95（缓存命中） | ≤ 60ms | 响应体 `cache.hit=true` |
| `GET /brief` P95（现算） | ≤ 2.8s | `cache.hit=false` + 各证据块耗时 |
| 首屏参谋卡可见 | ≤ 3.2s（与 K 线并行，不阻塞主图） | `data-as-of` vs 页面 `performance.now()` |
| 聊天工具单次 | ≤ 3s（`scan_auction_anomaly` ≤ 60s 且必带阶段提示） | `_TOOL_STAGE_LABELS` |
| 页面卡顿 | 卡片重渲染不触发 K 线重绘 | 参谋卡状态独立于 `KlineChart` props |

**埋点（复用既有 `reportFrontendError` / 后端 logger，不新建平台）**：

```python
logger.info("copilot_brief symbol=%s rule=%s tone=%s used=%d missing=%d ms=%d cache=%s",
            symbol, v["rule_id"], v["intent_tone"], len(v["evidence_used"]),
            len(v["evidence_missing"]), elapsed_ms, cache_hit)
```

**告警阈值**：同一标的连续 10 次 `evidence_used <= 2` → 记 WARNING（说明数据源成片失效，而不是单个用户网络问题）。

---

## 12. 测试计划

### 12.1 后端（`tests/test_copilot_brief.py`）

| # | 用例 | 断言 |
| :--- | :--- | :--- |
| 1 | 全证据正常 | `rule_id == "R1"`（构造 profit=99 + warning）；`confidence` 在 [0.2,0.95]；`defense_lines` 有 `first` 与 `core` |
| 2 | 筹码缺失 | `confidence <= 0.55`；`defense_lines` 不含 `core`；narrative 含「筹码模型暂无数据」 |
| 3 | 5 个证据源全抛异常 | 仍 200；`rule_id=="R9"`；`evidence_used==[]`；`action_plans` 走"关键价位缺失"分支 |
| 4 | 权限不足 | `resonance.gated is True` 且 `available is False`；不计入 `evidence_used` |
| 5 | 防守位去重 | `intraday_low` 与 `main_cost` 相差 0.3% → 只保留 1 条，`basis` 含两个来源 |
| 6 | 防守位过滤 | 现价 35，`cost_70_high=50`（偏离 >25%）→ 不出现 |
| 7 | 预案文案 | 4 个 tone × 2 audience 共 8 条，全部含价位数字且不含禁用词 |
| 8 | 非 A 股 | `market="HK"` → 200，`evidence.chips.available=false`，reason 含「仅 A 股」 |
| 9 | 缓存 | 连续两次调用第二次 `cache.hit=true`；`as_of` 两次不同（不许缓存时间戳） |
| 10 | 全降级不缓存 | 第一次全降级 → 第二次仍 `hit=false` |

### 12.2 工具（`tests/test_chat_tools_copilot.py`）

| # | 用例 | 断言 |
| :--- | :--- | :--- |
| 1 | 5 个工具全部在 registry | `set(新工具) <= set(CHAT_TOOL_REGISTRY)`，且 `caliber` 非空 |
| 2 | 导出顺序 | 5 个工具的 `EXPECTED_CORE_ORDER` 索引连续（31–35） |
| 3 | 空参不炸 | 每个 handler 传 `{}` → 返回提示字符串，无异常 |
| 4 | dict bars（P0 锁定） | `get_chip_accumulation` 正常出数 |
| 5 | 撤单字段映射 | mock `md_more_info` 返回小写键 → 断言 `intent_tag` 不是「无数据」 |
| 6 | 会话值全 0 回退 | mock `has_live_values→False` + `latest_snapshot` 有值 → 输出含「收盘快照」 |
| 7 | 竞价空池 | mock `fetch_auction_anomaly→[]` → 输出含「无数据，不猜」 |
| 8 | 妖股空库 | mock `load_factor_pool→[]` → 输出含「因子库为空」 |

### 12.3 前端

| 文件 | 断言 |
| :--- | :--- |
| `copilot-brief-card.test.tsx` | tone 徽章文本正确；`evidence_missing>=2` 时横幅出现；`gated` 块显示「升级解锁」而非 `--`；点击刷新调用 `reload` |
| `copilot-defense-block.test.tsx` | 空数组 → 显示「不给防守线（不猜价位）」；4 条以上截断；`distance_pct` 为 null → `--` |
| `copilot-capsule.test.tsx` | 点击后 `window.dispatchEvent` 收到 `sida-open-chat` 且 `detail.pageContext` 含「数智参谋快照」与 `as_of`；收起写 localStorage |
| `copilot-copy-contract.test.ts` | §7.4 合规正则 |
| `ai-copilot-page.test.tsx` | 空态提示；点预设 → 发消息；流式 delta 追加到同一条 assistant 消息（不新增气泡） |
| 布局探针 | 胶囊与 chat FAB 的 `getBoundingClientRect` 不相交（桌面 + 移动两档） |

### 12.4 真机验收（生产小主机，见 `sida-pro-dev` 的 prod 验证范式）

1. 容器内直接调 handler（不经 LLM）验证真实网关数据：
   ```bash
   docker exec <c> python3 -c "
   import asyncio
   from src.agents.chat.registry import _tool_get_chip_accumulation as f
   print(asyncio.run(f(None, {'symbol':'600111'}))[:600])"
   ```
2. 调 `build_copilot_brief` 打印 `rule_id/tone/evidence_used`，与页面截图逐项对照。
3. 检查 `/api/copilot/brief/600111` 的 `available` 矩阵：盘中应有 6–7 块可用；收盘后 `microstructure.session_fallback=true` 且 `stale=true`。

---

## 13. 实施批次（逐文件、可验收、可回滚）

### P0-0 前置修复（0.5 天，必须先合）

| 文件 | 改动 | 验收 |
| :--- | :--- | :--- |
| `src/core/chip_distribution.py` | `as_bar_views()` + `compute_main_accumulation_chips` 首行自适应 | 新增 3 条单测；`/klines/{s}/summary` 的 `main_accumulation_chips` 不再为空 |

> 这一步单独发一版（`v0.14.9`）并观察一天 K 线筹码山峰是否真正渲染出来。**如果这版没验证，参谋卡的"底仓成本"就是摆设。**

### Phase 1：后端契约（2 天）

| 文件 | 改动 |
| :--- | :--- |
| `src/core/copilot_brief.py`（新，~420 行） | 证据并发取数 + `INTENT_RULES` + 防守位 + 预案 + 文案 |
| `src/web/api/copilot.py`（新，~160 行） | 2 个端点 + Pydantic + 缓存/单飞 |
| `src/web/app.py` | `include_router(prefix="/api/copilot", dependencies=protected)` |
| `src/core/decision_pioneer.py` | `decision_pioneer_text` 补共振五态一行 |
| `tests/test_copilot_brief.py`（新） | 10 条用例 |
| `tests/test_decision_pioneer_text.py` | 补共振行断言 |

**验收**：`pytest tests/test_copilot_brief.py -q` 全绿；`curl` 401/200 区分正确；`app.openapi()["paths"]` 含新路径。

**回滚**：删 router 挂载一行即可（端点消失，前端 `useCopilotBrief` 走 error 态 → 卡片显示"暂不可用+重试"，不白屏）。

### Phase 2：5 个 chat 工具（1.5 天）

| 文件 | 改动 |
| :--- | :--- |
| `src/agents/chat/registry.py` | 插入 5 个 `@register_chat_tool` 块（anchor 见 §7.1） |
| `src/web/api/chat.py` | `SYSTEM_PROMPT` 追加路由节 + `_TOOL_STAGE_LABELS` 5 条 |
| `tests/test_w33_chat_registry.py` | `EXPECTED_CORE_ORDER` +5，count 49→54 |
| `tests/test_chat_tools_copilot.py`（新） | 8 条用例 |

**验收**：§7.1 的自证脚本输出 index 31–35；`pytest tests/test_w33_chat_registry.py -q` 绿。

**回滚**：移除 5 个注册块 + 还原测试字面量，一次 commit 可 revert。

### Phase 3：参谋卡 + 胶囊（2.5 天）

| 文件 | 说明 |
| :--- | :--- |
| `lib/copilot/{types,api,copy,tone,symbolChips}.ts` | 纯函数与文案 |
| `hooks/useCopilotBrief.ts` | 取数 |
| `components/copilot/CopilotBriefCard.tsx` + 4 个子块 + Skeleton | 主卡 |
| `components/copilot/CopilotCapsule.tsx` | 胶囊（复用 ChatWidget 通道） |
| `pages/workbench/tabs/SuggestTab.tsx` | 插入一行卡片 |
| `pages/StockWorkbench.tsx` | 挂胶囊 |
| 测试 5 个文件 | 见 §12.3 |

**验收**：工作台首屏可见卡片；胶囊不与 FAB 重叠；`tsc -b` + `check_ui_rules` 绿。

**回滚**：`SuggestTab` 删 1 行、`StockWorkbench` 删 1 行，卡片与胶囊组件保留但不再挂载（零副作用）。

### Phase 4：/ai-copilot 一级页（2 天）

| 文件 | 说明 |
| :--- | :--- |
| `pages/AiCopilot.tsx` | 左指令池 + 右工作台 |
| `hooks/useCopilotChat.ts` | 会话 runner（复用 `chatApi`） |
| `App.tsx` | navItems + 分组 + 路由 + 快捷键 |
| `locales/{zh-CN,en-US}.ts` | `nav.aiCopilot` |
| `components/CommandPalette.tsx` | 追加 `⌘J` |

**验收**：点击预设能出真数据回答；个股跳板链接可跳转；移动端可用。

### Phase 5：可选增强（按需）

1. **SSE 内联卡片**：流结束前推 `event: card`（`{symbol, brief}`），前端在回答下方插一张 mini 参谋卡。需改 `chat.py::send_message_stream` 与 `packages/api/src/chat.ts` 的 `dispatchSSEEvent`（加一个 case）。**先不做**：价值中等，破坏面（SSE 契约）较大。
2. **持仓批量参谋**：`POST /api/copilot/brief/batch`（≤20 只），给"我的"页做一张"持仓体检"表。注意别做成 N 次单查（会打爆 TQ）。
3. **参谋结论落库与复盘**：把每次 `verdict` 写 `copilot_brief_snapshots`，7 日后自动回填"结论是否被验证"，用于校准 `INTENT_RULES` 阈值（这是把规则引擎变成可进化的关键，但要先有 2 周数据）。

---

## 14. 风险、未决项与"不做清单"

### 14.1 风险

| 风险 | 影响 | 缓解 |
| :--- | :--- | :--- |
| TQ 网关在盘中抖动 | 4 个证据块同时失效，卡片全是 `--` | 每块独立降级 + 顶部横幅 + 「证据 2/7」显式告知；不缓存全降级 |
| `scan_auction_anomaly` 冷启动 20–60s | 用户以为卡死 | 专属阶段文案 + 描述里不承诺速度 + 仅在明确问全市场竞价时调用 |
| 两套筹码口径被混用 | 用户发现"同一只票两个成本价"，信任崩塌 | 工具 description 与卡片脚注**都**写口径；LLM prompt 禁止混算；必要时在 UI 上把 `get_stock_chips` 的入口文案改为"客户端公式口径" |
| 规则引擎的阈值拍脑袋 | 结论方向性错误 | 所有阈值集中在 `INTENT_RULES` 常量表；`rule_id` 可审计；Phase 5.3 做回填校准 |
| 文案踩合规线 | 上线后被合规/审核打回 | `copy.ts` 单点收敛 + vitest 正则门禁 |
| 一级导航变 7 组 | 违反设计稿约束 | §9.3 二选一，先拍板再写代码 |

### 14.2 未决项（需老板/产品确认）

1. **导航方案 A 还是 B**（§9.3）——影响 `App.tsx` 的分组结构与移动端布局。
2. **参谋卡是否也要出现在指数工作台**：当前设计为"不出现"（指数无筹码/撤单）。若要做，需另设指数证据（宽度/涨跌家数/资金），是独立工作量。
3. **member 档能看到几块证据**：本方案默认 `chips/wyckoff/demon` 免费、`resonance/microstructure/auction` 需 pro。这会直接影响"免费用户看到的卡片长什么样"（3/7 证据，置信度会被压到 ~0.5）。需与商业档位对齐。
4. **是否允许一键"设为提醒"**：卡片上有防守位，天然适合"跌破 34.62 提醒我"。但 `handleSetAlert` 当前零生产调用方（KI-058），接线等于复活一条已退役路径，需产品明确。

### 14.3 明确不做（本期）

| 不做 | 理由 |
| :--- | :--- |
| LLM 直接生成买卖结论 | 不可复现、不可审计、合规风险；结论必须来自规则引擎 |
| 为参谋卡新建一套 chat 通道 | `ChatWidget` 已有完整通道与上下文注入，重写＝双维护 |
| 把 5 个工具做成 1 个"万能盘口工具" | 模型无法据 description 精确选择，反而更容易错调；细粒度工具 + 路由矩阵更稳 |
| 卡片内嵌实时 K 线缩略图 | 首屏已有主图；再加一个 canvas 会显著拖慢并抢占主图 GPU |
| 语音播报参谋结论 | 交易场景静默优先，且需额外的 TTS 成本与合规审核 |

---

## 15. 交付清单（可直接当 PR checklist）

**后端**

- [ ] `as_bar_views` + 筹码自适应（P0，独立 commit）
- [ ] `src/core/copilot_brief.py`（规则引擎，纯函数可测）
- [ ] `src/web/api/copilot.py`（2 端点，Pydantic 契约）
- [ ] `app.py` 挂载 + openapi 断言
- [ ] `decision_pioneer_text` 补共振五态
- [ ] 5 个 chat 工具（anchor 正确、caliber 非空、requires="")
- [ ] `SYSTEM_PROMPT` 路由节 + `_TOOL_STAGE_LABELS` 5 条
- [ ] `EXPECTED_CORE_ORDER` +5、`len==54`
- [ ] `tests/test_copilot_brief.py`（10）+ `tests/test_chat_tools_copilot.py`（8）
- [ ] 覆盖率棘轮通过

**前端**

- [ ] `lib/copilot/*` 五个文件（types/api/copy/tone/symbolChips）
- [ ] `useCopilotBrief` / `useCopilotChat`
- [ ] `CopilotBriefCard` + 4 子块 + Skeleton + EvidenceDrawer
- [ ] `CopilotCapsule`（避让 FAB + 可恢复）
- [ ] `AiCopilot` 页面 + 导航 + i18n + `⌘J`
- [ ] `SuggestTab` / `StockWorkbench` 各 1 行接线
- [ ] 6 个 vitest 文件（含合规正则 + 布局探针）
- [ ] `tsc -b` / `eslint` / `check_ui_rules.mjs` 全绿
- [ ] `.tsx` 全部 CRLF

**文档与运维**

- [ ] `CHANGELOG.md` 同 commit 记录（含 P0 修复）
- [ ] `docs/permission-matrix.md` 补 copilot 端点的权限要求
- [ ] 本文件回填"实施结果"小节（实际 rule_id 分布、命中率观察）

---

*本方案已完成工程级细化：契约可照抄、伪码可直译、组件可直写、门禁可自证。下一步按 P0 → Phase 1 → 2 → 3 → 4 顺序施工，每阶段独立可回滚。*

---

## 附录 A：v1 战略原文（保留存档，2026-10-18 初版）

> 以下为 v2 细化前的初版内容，保留以追溯设计意图；其中"5 个工具"的命名已被 §1.2 纠偏，**施工以正文为准**。


> **文档定位**：将 SIDA-Pro 已沉淀的 24+ 通达信接口、CYQ 筹码峰、决策先锋三指标共振、威科夫量价结构、9:25 竞价量比与妖股因子等高阶能力全面接入 AI 大脑，构建主动参谋、全量数据打通与高可见度 Copilot 的完整实战方案。  
> **归档路径**：`/home/ubuntu/sida-pro/docs/research/AI数智参谋全链路落地设计方案.md`  
> **设计日期**：2026-10-18  

---

#### 一、 方案背景与核心命题

##### 1. 现状矛盾
SIDA-Pro 目前已经拥有全市场极强的底层微观数据武器库：
- **通达信 24 个高阶接口**（真实竞价金额 `OpenAmo`、竞价量比 `OpenVolPre1`、真实撤单率、涨停委买）；
- **CYQ 120日波段主力建仓筹码沉淀模型** 与 K 线横向筹码山峰直方图；
- **同花顺决策先锋选股铁三角**（GS趋势 + AI机构活跃度 + 逐笔暗盘净额）五态共振；
- **威科夫量价结构图层**（弹簧针 `spring_probe`、放量滞涨、缩量沉淀）；
- **妖股先锋 Top50 基因库** 与独立集合竞价专区 (`/auction`)。

然而**职业短线与超短交易者在盘中 4 个小时内，根本来不及逐个翻看 10 几个卡片与复杂图层**。普通用户面对密集的数据看板，极易产生“信息过载、不知该买谁、何时止损”的决策瘫痪。

##### 2. 核心命题：让 AI 成为操盘手的“副驾驶”
- **变被动为主动**：不再等用户费劲去查，AI 主动融合多维证据，输出极简、确定、可执行的参谋预案；
- **全量数据接入大脑**：AI 对话助手拥有全套盘口量化工具，用户随口一问即可秒级调出真实底层数据；
- **大幅提升入口存在感**：打破仅有一个小图标的低可见度现状，打造独立一级大模块与工作台常驻唤醒胶囊。

---

#### 二、 核心阵地 1：个股工作台【AI 建议】升级为【多维数智参谋】

在 `/stocks/:symbol` 的【建议】标签（以及主图顶部醒目位置），重塑原有的文本建议，升级为具备**事实证据链支撑的职业级操盘卡片**。

##### 1. 四维确定性证据链输入（AI 决策依据）
后端汇聚统一的结构化 Context，输入给研判引擎（绝不允许模型主观臆造）：

```json
{
  "symbol": "600111",
  "name": "北方稀土",
  "close": 35.32,
  "chip_model": {
    "main_cost": 35.20,
    "cost_70_low": 33.44,
    "cost_70_high": 36.96,
    "profit_ratio": 99.0,
    "concentration": 0.082,
    "pattern": "单峰密集",
    "departure_pct": 0.34
  },
  "resonance": {
    "gs_state": "S区",
    "ai_activity": 2.69,
    "activity_level": "生命",
    "dark_net": -5395.0,
    "resonance_state": "全线走坏"
  },
  "microstructure": {
    "b_cancel_ratio": 88.9,
    "s_cancel_ratio": 64.7,
    "intent_label": "⚠️ 托单诱多",
    "wyckoff": "放量滞涨"
  },
  "theme_and_demon": {
    "theme": "稀土永磁",
    "theme_mood": "高潮分歧",
    "is_demon_top50": false
  }
}
```

##### 2. 结构化输出卡片规范（拒绝车轱辘话）
界面直接采用三段式实战操盘卡片：
1. **【主力意图定性】**：
   - 例：`主力在 35.20 附近沉淀重仓底仓，当前获利 99% 处于极高位。盘口买方撤单率高达 88.9%，呈现虚假托单掩护出货迹象，威科夫触发放量滞涨！`
2. **【关键防守价位】**：
   - `第一防守线：34.62 (今日分时探底位)`；
   - `核心生命线：33.44 (70%建仓筹码底沿，破位即确认破局)`；
   - `强阻力压制：36.96 (70%筹码顶沿)`。
3. **【操盘行动预案】**：
   - `持仓者：建议在 35.40 附近主动减仓保利，跌破 34.62 坚决清仓防守；`
   - `空仓者：严禁追高，耐心等待回踩 33.44 筹码底沿且 GS 信号翻红转 G 时再行关注。`

---

#### 三、 核心阵地 2：AI 对话助手工具链（Chat Tool Registry）全面打通

在 `src/agents/chat/` 下新增 5 个专业量化取数工具，并注册到 `registry.py` 中，使 AI 对话引擎具备全盘穿透力：

##### 1. 新增 AI 工具清单

| 工具名称 | 功能描述 | 核心字段返回 |
| :--- | :--- | :--- |
| **`get_chip_accumulation`** | 提取个股 120 交易日波段主力筹码沉淀数据 | 主力建仓成本(`main_cost`)、70%筹码区间(`cost_70_low/high`)、获利盘比例(`profit_ratio`)、集中度(`concentration`)、形态(`pattern`) |
| **`get_auction_anomaly`** | 提取个股当日 9:25 集合竞价真实盘口数据 | 竞价金额(`open_amount`)、竞价量比(`volume_ratio`)、真实撤单率(`withdraw_rate`)、五态意图(`verdict_tag`) |
| **`get_cancel_order_intent`** | 提取个股盘口微观撤单意图 | 买撤率(`b_cancel_ratio`)、卖撤率(`s_cancel_ratio`)、撤单偏差(`cancel_bias`)、意向判定(`intent_label`) |
| **`get_decision_resonance`** | 提取决策先锋三指标共振状态 | GS状态与买卖点、AI机构活跃度数值与分级、逐笔暗盘资金流、共振五态标签 |
| **`get_demon_factor`** | 提取个股妖股评分与主线身位 | 妖股综合得分(`demon_score`)、分级(`demon_grade`)、连板天数、主线梯队归属 |

##### 2. 问答实战效果
- **用户问**：*“北方稀土现在主力底仓在什么位置？现在买安全吗？”*
  - **AI 调用**：`get_chip_accumulation(symbol='600111')` + `get_decision_resonance(symbol='600111')`
  - **AI 回答**：直报主力底仓在 35.20 元，获利盘 99%，当前 GS 处于 S 卖区，明确告知不安全，回踩 33.44 再看。
- **用户问**：*“今天早上竞价有哪些票在爆量抢筹？”*
  - **AI 调用**：`get_auction_anomaly(filter='strong_buy')`
  - **AI 回答**：直接列出竞价金额 >2000 万且量比放大 3 倍以上的龙头标的清单。

---

#### 四、 核心阵地 3：提升 AI 存在感与交互入口体验

##### 1. 左侧一级导航独立新增【数智参谋】大模块 (`/ai-copilot`)
- **导航图标**：位于【行情】与【机会】下方，命名为 **`🤖 数智参谋 (AI Copilot)`**，带有微发光高亮徽章；
- **主功能布局**：
  - **左侧推荐战术指令池**：
    - `🔥 扫描今日 9:25 爆量抢筹股`
    - `🎯 诊断持仓股主力成本与获利盘`
    - `🛡️ 筛选决策先锋三指标首次共振股`
    - `👑 检索当前主线中的妖股先锋候选`
  - **右侧全屏交互工作台**：支持流式输出、直接内嵌可点击的个股跳板链接（如 `[600111 北方稀土]` 直接切入工作台）、以及结构化筹码卡片展示。

##### 2. 个股工作台右下角【AI 智能盯盘悬浮胶囊 (Copilot Capsule)】
- **常驻位置**：在 `/stocks/:symbol` 工作台右下角；
- **视觉设计**：半透明玻璃态胶囊按钮，显示 `✨ 盘口参谋 · 600111`；
- **深度上下文预注入（Zero-Input）**：
  - 用户点开后，无需输入股票代码，弹出的抽屉对话框已经**自动把当前个股的筹码峰、GS信号、盘口大单状态预注入上下文**；
  - 底部提供快捷气泡按钮：`[主力成本在哪？]`、`[买方在托单出货吗？]`、`[跌到多少必须止损？]`，单手点击一键秒回！

---

#### 五、 实施路线图与落地批次

| 阶段 | 核心任务 | 涉及代码与模块 | 交付物 |
| :--- | :--- | :--- | :--- |
| **Phase 1: 工具链打通** | 在 `src/agents/chat/` 下新增 5 个盘口量化工具，打通底层接口并完成单测验证 | `src/agents/chat/registry.py`<br>`src/agents/chat/tools_market.py` | 5 项工具 100% 注册并通过对话调用单测 |
| **Phase 2: 工作台建议升级** | 重塑 `/stocks/:symbol` 的【建议】标签，落地三段式“意图 + 成本线 + 操盘预案”卡片 | `frontend/src/pages/workbench/tabs/SuggestTab.tsx`<br>`src/web/api/insights.py` | 多维证据参谋卡片正式上线 |
| **Phase 3: 独立入口与胶囊** | 新增 `/ai-copilot` 一级大模块页面，并在个股工作台挂载右下角悬浮唤醒胶囊 | `frontend/src/pages/AiCopilot.tsx`<br>`frontend/src/components/CopilotCapsule.tsx` | 全新一级 AI 模块与快捷交互闭环 |

---
*方案设计已就绪，可随时依此路线图开启代码实施！*