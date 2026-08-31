"""板块资金流向 vendor:东财 push2 clist 接口（仅国内可用，海外 502）。

移植自 a-stock-data(SKILL.md V3.6.0)§3.8 board_fund_flow() 函数:
  行业/概念/地域 × 今日/5日/10日,主力净流入额/净占比 + 四档单净额。

⚠️ 实测验证(2026-08-09 海外节点 43.128.140.167):
  - push2.eastmoney.com 海外 502(Bad Gateway) ❌
  - push2delay.eastmoney.com 海外 rc=102 ❌(只缓存实时价,不缓存资金流)
  - 同花顺 q.10jqka.com.cn API 海外 404 ❌
  - 结论: **海外节点无法取板块资金流**;vendor 仍按 SKILL 实现写完,
    海外调用会 fast-fail 抛清晰错误(避免静默返回 0 行被误读成"今日无异动")。
  - 国内服务器直接 push2 直连可用;海外 server 跑 PanWatch 时应在配置文件里
    把 board_fund_flow vendor 标记 disabled,免得浪费重试配额。

接口范式:市场级、单源、非 symbol 模型——与 discovery.py 同 pattern,不继承 Vendor。
"""
from __future__ import annotations

import logging
from typing import Any

from marketdata.http import market_get

logger = logging.getLogger(__name__)

_API = "https://push2.eastmoney.com/api/qt/clist/get"
_HOST_KEY = "push2.eastmoney.com"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"}

# 板块类型 → 东财 fs 参数
_BOARD_FS = {
    "industry": "m:90+t:2",
    "concept": "m:90+t:3",
    "region": "m:90+t:1",
}

# 周期 → (排序fid, 主力净额fid, 主力净占比fid, 涨跌幅fid, 领涨股name fid)
# 四档明细(超大/大/中/小单净额)仅今日周期有意义(用 f66/f72/f78/f84)
_BOARD_PERIOD = {
    "today": ("f62",  "f62",  "f184", "f3",   "f204"),
    "5d":    ("f164", "f164", "f165", "f109", "f257"),
    "10d":   ("f174", "f174", "f175", "f160", None),  # 10日领涨股字段不稳定
}


def _normalize_diff(d: Any) -> list[dict]:
    """东财 clist 的 diff 可能是 list 也可能是 dict(按 index 为 key)→ 统一 list。"""
    diff = ((d or {}).get("data") or {}).get("diff") or []
    if isinstance(diff, dict):
        return [v for v in diff.values() if isinstance(v, dict)]
    return diff if isinstance(diff, list) else []


class BoardFundFlowVendor:
    """板块资金流向(东财 push2)——国内服务器可用,海外节点调用会 502。"""

    name = "eastmoney_board_fund_flow"

    def fetch(
        self,
        board_type: str = "industry",
        period: str = "today",
        top_n: int = 20,
        **_: Any,
    ) -> dict:
        """
        板块资金流向排名(按主力净流入降序)。

        Args:
            board_type: industry(行业) / concept(概念) / region(地域)
            period: today(今日) / 5d(5日) / 10d(10日)
            top_n: 返回前 N 条(默认 20)

        Returns:
            {
              "board_type": "industry",
              "period": "today",
              "total": 496,
              "rows": [
                {
                  "rank": 1, "name": "电力设备", "code": "BK0428",
                  "change_pct": 2.5,
                  "main_net": 6466000000.0,    # 主力净额(元)
                  "main_pct": 8.5,             # 主力净占比(%)
                  "leader": "宁德时代",
                  # 仅 today:
                  "super_large_net": 4355000000.0,
                  "large_net":       2111000000.0,
                  "medium_net":      0.0,
                  "small_net":       0.0,
                }, ...
              ]
            }
        """
        if board_type not in _BOARD_FS:
            raise ValueError(f"board_type 须为 {list(_BOARD_FS)}")
        if period not in _BOARD_PERIOD:
            raise ValueError(f"period 须为 {list(_BOARD_PERIOD)}")
        if not isinstance(top_n, int) or top_n <= 0:
            raise ValueError(f"top_n 须为正整数, 收到 {top_n}")

        fid_sort, f_main, f_pct, f_chg, f_leader = _BOARD_PERIOD[period]

        # fields: 必须包含 fid_sort(用于排序)+ 4 档(仅 today)
        fields = ["f12", "f14", f_chg, f_main, f_pct]
        if f_leader:
            fields.append(f_leader)
        if period == "today":
            fields += ["f66", "f72", "f78", "f84"]

        base = {
            "pz": "200",
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": fid_sort,
            "fs": _BOARD_FS[board_type],
            "fields": ",".join(dict.fromkeys(fields)),  # 去重保序
        }

        # 实测行业 496 / 概念 495 个,pz=200 会截断,需要翻页拿全
        def _page(pn: int) -> tuple[list, int]:
            r = market_get(
                _API,
                host_key=_HOST_KEY,
                headers=_HEADERS,
                params={**base, "pn": str(pn)},
                parse="json",
                retries=2,
                timeout=15,
                log_label=f"板块资金流[{board_type}/{period}]",
            )
            if r is None:
                # 海外节点 push2 502 死路 + push2delay 没缓存资金流 → 显式 fail-fast
                # (避免静默返 0 行被误读成"今日无异动")。
                # 国内服务器在 settings 关掉此 vendor 即可,海外无可用备胎。
                raise RuntimeError(
                    f"板块资金流 vendor 不可用 (board_type={board_type}, period={period}, "
                    f"pn={pn});海外 push2 502 + push2delay 不缓存资金流,无 fallback。"
                )
            d = (r or {}).get("data") or {}
            return _normalize_diff(d), int(d.get("total") or 0)

        _PAGE = 200
        items, total = _page(1)
        pn = 2
        while len(items) < top_n:
            if total and len(items) >= total:
                break
            more, _ = _page(pn)
            if not more:
                break
            items += more
            pn += 1
            if len(more) < _PAGE:
                break
        total = max(total, len(items))

        rows = []
        for i, it in enumerate(items[:top_n]):
            row = {
                "rank": i + 1,
                "name": str(it.get("f14", "")),
                "code": str(it.get("f12", "")),
                "change_pct": it.get(f_chg, 0),
                "main_net": it.get(f_main, 0),  # 元
                "main_pct": it.get(f_pct, 0),   # %
                "leader": str(it.get(f_leader, "")) if f_leader else "",
            }
            if period == "today":
                row.update({
                    "super_large_net": it.get("f66", 0),
                    "large_net":       it.get("f72", 0),
                    "medium_net":      it.get("f78", 0),
                    "small_net":       it.get("f84", 0),
                })
            rows.append(row)

        return {
            "board_type": board_type,
            "period": period,
            "total": total,
            "rows": rows,
        }
