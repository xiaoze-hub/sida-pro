"""资金类指标口径契约(风险整改 3.4/B3)。

把"口径"从 prompt 字符串(chat.py 系统提示 0.0)升级为代码层类型: 资金类指标
返回值必须携带 caliber + direction_semantics, 下游据此决定能不能做方向性判断。

红线(AGENTS.md "SIDA 业务硬约束" 口径条目):
- ``tick``   腾讯逐笔主动买卖方向 —— 唯一可用于主力意图/方向性判定的口径
- ``eastmoney4`` 按单金额四档归类净额(东财 push2 直连/网关、腾讯四档、Engine
  四档同属此类) —— 与逐笔口径方向可能相反, 禁止用于主力意图判定, 仅作资金面参考
- ``ths``    同花顺 DDE 大单口径 —— 与逐笔/东财四档均不同, 同样禁止用于主力意图判定
- ``unknown`` 未标注 —— 一律不得用于方向性判定
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Caliber = Literal["tick", "eastmoney4", "ths", "unknown"]

DIRECTION_TICK = "active_buy_minus_sell(逐笔主动买卖方向, 可用于主力意图判定)"
DIRECTION_EASTMONEY4 = (
    "size_bucket_net(按单金额四档归类净额, 非逐笔方向, 与逐笔口径可能相反, "
    "禁止用于主力意图判定, 仅作资金面参考)"
)
DIRECTION_THS = "ths_dde(同花顺 DDE 大单口径, 与逐笔/东财四档均不同, 禁止用于主力意图判定)"
DIRECTION_UNKNOWN = "unknown(未标注口径, 不得用于方向性判定)"


@dataclass(frozen=True)
class CaliberTag:
    """指标口径标签: 随数据一起透传到下游与 UI。"""

    caliber: Caliber
    direction_semantics: str
    source: str

    def ui_label(self) -> str:
        if self.caliber == "tick":
            return "【口径: 腾讯逐笔·可用于主力意图判定】"
        if self.caliber == "eastmoney4":
            return "【口径: 东财四档·资金面参考, 禁用于主力意图判定】"
        if self.caliber == "ths":
            return "【口径: 同花顺DDE·资金面参考, 禁用于主力意图判定】"
        return "【口径: 未标注·不得用于方向性判定】"

    def to_dict(self) -> dict:
        return {
            "caliber": self.caliber,
            "direction_semantics": self.direction_semantics,
            "source": self.source,
            "label": self.ui_label(),
        }


# collector 全部取数路径(东财 push2 直连/网关、腾讯四档、Engine 四档)同属
# 按单金额归类口径 —— 方向语义同类, 统一打 eastmoney4 标(详见 capital_flow_collector docstring)
CAPITAL_FLOW_TAG = CaliberTag(
    caliber="eastmoney4",
    direction_semantics=DIRECTION_EASTMONEY4,
    source="eastmoney_push2/腾讯四档/Engine四档(按单金额归类口径)",
)


class CaliberViolationError(ValueError):
    """非逐笔口径被用于方向性判定(AGENTS.md 口径红线)。"""


def require_directional(tag: CaliberTag, usage: str) -> None:
    """方向性判定出口校验: 非 tick 口径直接抛错。

    用在"拿资金数字下方向结论"的代码出口(策略信号/意图判定), 例如
    ``require_directional(tag, "主力意图判定")`` —— eastmoney4/ths/unknown 到这里就是红线违规。
    """
    if tag.caliber != "tick":
        raise CaliberViolationError(
            f"{usage} 需要 tick(逐笔)口径, 收到 {tag.caliber}: {tag.ui_label()}"
            "(AGENTS.md 口径红线: 主力意图识别必须走 get_main_intent 逐笔口径)"
        )


def reconcile_direction(
    main: CaliberTag, main_value: float, ref: CaliberTag, ref_value: float
) -> dict:
    """逐笔 vs 参考口径同时在场时的裁决(代码层可验证 chat.py 0.0 提示的要求)。

    返回 {"agree": bool, "statement": str}: 方向冲突时明确说明口径差异并
    **优先采信逐笔**(main 必须是 tick 口径, 否则抛 CaliberViolationError)。
    """
    require_directional(main, "口径裁决主口径")
    main_dir = "流入" if main_value > 0 else ("流出" if main_value < 0 else "平衡")
    ref_dir = "流入" if ref_value > 0 else ("流出" if ref_value < 0 else "平衡")
    agree = (main_value > 0) == (ref_value > 0)
    if agree:
        statement = (
            f"两口径方向一致({main_dir}): 主口径{main.ui_label()}{main_value:+.0f}元, "
            f"参考{ref.ui_label()}{ref_value:+.0f}元。"
        )
    else:
        statement = (
            f"⚠️ 口径冲突: 主口径{main.ui_label()}判{main_dir}({main_value:+.0f}元), "
            f"参考口径{ref.ui_label()}判{ref_dir}({ref_value:+.0f}元) —— "
            "统计方式不同(逐笔主动买卖盘 vs 按单金额四档归类), 方向判定一律以逐笔为准, "
            "参考口径仅作资金面参考, 禁止据此下主力意图结论。"
        )
    return {"agree": agree, "statement": statement}
