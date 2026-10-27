#!/usr/bin/env python3
"""生成三份自绘 Lottie 场景（不依赖任何外部素材/网络）。

为什么自己画而不是去下载：
  · 素材站导出的 JSON 通常 25~200KB，且配色/线宽/圆角都不受我们控制；
  · 这三个场景只需要「描线 + 脉冲」，手写 bodymovin 结构各 2~4KB，
    体积、颜色、节奏都能按主题 token 对齐（配合 LottieScene 的 token 染色）。

输出：frontend/public/lotion/{scan,check,grow}.json（v5.7.0 结构，lottie-web 5.x 直读）
颜色一律写成中性灰 [0.5,0.5,0.5,1]，名字带 "token:" 前缀，运行时由 LottieScene 按主题替换。
"""
from __future__ import annotations

import json
import pathlib

OUT = pathlib.Path(__file__).resolve().parents[1] / "public" / "lotion"
TOKEN_COLOR = [0.5, 0.5, 0.5, 1]


def _st(name: str, width: float, opacity: float = 100) -> dict:
    return {
        "ty": "st",
        "nm": f"token:{name}",
        "c": {"a": 0, "k": TOKEN_COLOR},
        "o": {"a": 0, "k": opacity},
        "w": {"a": 0, "k": width},
        "lc": 2,
        "lj": 2,
    }


def _ellipse(r: float) -> dict:
    return {"ty": "el", "nm": "ring", "p": {"a": 0, "k": [0, 0]}, "s": {"a": 0, "k": [r * 2, r * 2]}}


def _trim(start: float = 0, end: float = 100, offset: float = 0) -> dict:
    return {
        "ty": "tm",
        "nm": "trim",
        "s": {"a": 0, "k": start},
        "e": {"a": 0, "k": end},
        "o": {"a": 0, "k": offset},
        "m": 1,
    }


def _transform() -> dict:
    return {
        "ty": "tr",
        "nm": "tr",
        "p": {"a": 0, "k": [0, 0]},
        "a": {"a": 0, "k": [0, 0]},
        "s": {"a": 0, "k": [100, 100]},
        "r": {"a": 0, "k": 0},
        "o": {"a": 0, "k": 100},
    }


def _group(items: list[dict]) -> dict:
    return {"ty": "gr", "nm": "g", "it": items + [_transform()]}


def _ks(opacity_k: dict | None = None, scale_k: dict | None = None) -> dict:
    return {
        "o": opacity_k or {"a": 0, "k": 100},
        "r": {"a": 0, "k": 0},
        "p": {"a": 0, "k": [0, 0, 0]},
        "a": {"a": 0, "k": [0, 0, 0]},
        "s": scale_k or {"a": 0, "k": [100, 100, 100]},
    }


def _kf(pairs: list[tuple[int, list[float]]], linear: bool = True) -> dict:
    """线性关键帧：pairs = [(frame, value[]), ...]。"""
    keys = []
    for idx, (frame, value) in enumerate(pairs):
        key = {"t": frame, "s": value}
        if idx < len(pairs) - 1:
            nxt = pairs[idx + 1][0]
            key["e"] = pairs[idx + 1][1]
            if linear:
                key["i"] = {"x": [1], "y": [1]}
                key["o"] = {"x": [0], "y": [0]}
            key["h"] = 1 if nxt - frame == 1 else 0
        keys.append(key)
    return {"a": 1, "k": keys}


def scan() -> dict:
    """雷达脉冲：三圈依次扩散后淡出，用于「刷新中 / 扫描中」。"""
    layers = []
    for index in range(3):
        begin = index * 20
        layers.append(
            {
                "ddd": 0,
                "ind": index + 1,
                "ty": 4,
                "nm": f"pulse-{index}",
                "sr": 1,
                "ks": _ks(
                    opacity_k=_kf([(begin, [80]), (begin + 60, [0]), (begin + 61, [0]), (90, [0])]),
                    scale_k=_kf([(begin, [40, 40, 100]), (begin + 60, [150, 150, 100]), (90, [150, 150, 100])]),
                ),
                "ao": 0,
                "shapes": [_group([_ellipse(26), _st(f"ring-{index}", 3)])],
                "ip": 0,
                "op": 90,
                "st": 0,
                "bm": 0,
            }
        )
    # 中心点：呼吸的实心圆
    layers.append(
        {
            "ddd": 0,
            "ind": 9,
            "ty": 4,
            "nm": "core",
            "sr": 1,
            "ks": _ks(
                opacity_k=_kf([(0, [90]), (45, [55]), (90, [90])]),
                scale_k=_kf([(0, [100, 100, 100]), (45, [84, 84, 100]), (90, [100, 100, 100])]),
            ),
            "ao": 0,
            "shapes": [
                _group(
                    [
                        {"ty": "el", "nm": "dot", "p": {"a": 0, "k": [0, 0]}, "s": {"a": 0, "k": [10, 10]}},
                        {"ty": "fl", "nm": "token:core", "c": {"a": 0, "k": TOKEN_COLOR}, "o": {"a": 0, "k": 100}},
                    ]
                )
            ],
            "ip": 0,
            "op": 90,
            "st": 0,
            "bm": 0,
        }
    )
    return {
        "v": "5.7.0",
        "fr": 60,
        "ip": 0,
        "op": 90,
        "w": 96,
        "h": 96,
        "nm": "scan",
        "ddd": 0,
        "assets": [],
        "layers": layers,
        "markers": [],
    }


def check() -> dict:
    """成功勾选：外圈先描一遍，再描勾。用于 toast / 保存成功。"""
    circle_keys = {
        "a": 1,
        "k": [
            {"t": 0, "s": [0], "e": [100], "i": {"x": [1], "y": [1]}, "o": {"x": [0], "y": [0]}},
            {"t": 26, "s": [100]},
        ],
    }
    tick_keys = {
        "a": 1,
        "k": [
            {"t": 20, "s": [0], "e": [100], "i": {"x": [1], "y": [1]}, "o": {"x": [0], "y": [0]}},
            {"t": 48, "s": [100]},
        ],
    }
    layers = [
        {
            "ddd": 0,
            "ind": 1,
            "ty": 4,
            "nm": "circle",
            "sr": 1,
            "ks": _ks(),
            "ao": 0,
            "shapes": [
                _group(
                    # 顺序铁律（2026-10-27 用四种排列实测出来的，别凭直觉改）：
                    #   shape → stroke → trim  ✅ 生效（trim 直接裁路径几何，**不写 dasharray**）
                    #   shape → trim → stroke  ✅ 同上
                    #   trim  → stroke → shape ❌ 路径变空 d="M0 0"
                    #   trim  → shape → stroke ❌ trim 被忽略（画全路径）
                    # 判据不能用 stroke-dasharray —— lottie 不写它；要比对 getTotalLength() 比例。
                    [
                        _ellipse(21),
                        _st("ring", 3),
                        {"ty": "tm", "nm": "trim", "s": {"a": 0, "k": 0}, "e": circle_keys, "o": {"a": 0, "k": 0}, "m": 1},
                    ]
                )
            ],
            "ip": 0,
            "op": 60,
            "st": 0,
            "bm": 0,
        },
        {
            "ddd": 0,
            "ind": 2,
            "ty": 4,
            "nm": "tick",
            "sr": 1,
            "ks": _ks(),
            "ao": 0,
            "shapes": [
                _group(
                    [
                        {
                            "ty": "sh",
                            "nm": "path",
                            "ks": {
                                "a": 0,
                                "k": {
                                    "i": [[0, 0], [0, 0], [0, 0]],
                                    "o": [[0, 0], [0, 0], [0, 0]],
                                    "v": [[-9, 1], [-2.5, 8], [10, -7]],
                                    "c": False,
                                },
                            },
                        },
                        _st("ring", 3.6),
                        {"ty": "tm", "nm": "trim", "s": {"a": 0, "k": 0}, "e": tick_keys, "o": {"a": 0, "k": 0}, "m": 1},
                    ]
                )
            ],
            "ip": 0,
            "op": 60,
            "st": 0,
            "bm": 0,
        },
    ]
    return {
        "v": "5.7.0",
        "fr": 60,
        "ip": 0,
        "op": 60,
        "w": 48,
        "h": 48,
        "nm": "check",
        "ddd": 0,
        "assets": [],
        "layers": layers,
        "markers": [],
    }


def grow() -> dict:
    """空态折线生长：一条上扬折线 + 末端光点，用于「暂无数据」占位。"""
    path = {
        "i": [[0, 0], [0, 0], [0, 0], [0, 0]],
        "o": [[0, 0], [0, 0], [0, 0], [0, 0]],
        "v": [[-46, 22], [-16, 2], [12, 12], [46, -20]],
        "c": False,
    }
    trim_keys = {
        "a": 1,
        "k": [
            {"t": 0, "s": [0], "e": [100], "i": {"x": [1], "y": [1]}, "o": {"x": [0], "y": [0]}},
            {"t": 54, "s": [100]},
        ],
    }
    return {
        "v": "5.7.0",
        "fr": 60,
        "ip": 0,
        "op": 90,
        "w": 120,
        "h": 72,
        "nm": "grow",
        "ddd": 0,
        "assets": [],
        "layers": [
            {
                "ddd": 0,
                "ind": 1,
                "ty": 4,
                "nm": "line",
                "sr": 1,
                "ks": _ks(),
                "ao": 0,
                "shapes": [
                    _group(
                        [
                            {"ty": "sh", "nm": "path", "ks": {"a": 0, "k": path}},
                            _st("line", 3),
                            {"ty": "tm", "nm": "trim", "s": {"a": 0, "k": 0}, "e": trim_keys, "o": {"a": 0, "k": 0}, "m": 1},
                        ]
                    )
                ],
                "ip": 0,
                "op": 90,
                "st": 0,
                "bm": 0,
            },
            {
                "ddd": 0,
                "ind": 2,
                "ty": 4,
                "nm": "head",
                "sr": 1,
                "ks": _ks(
                    opacity_k=_kf([(0, [0]), (46, [0]), (60, [85]), (90, [85])]),
                    scale_k=_kf([(46, [60, 60, 100]), (66, [120, 120, 100]), (90, [120, 120, 100])]),
                ),
                "ao": 0,
                "shapes": [
                    _group(
                        [
                            {"ty": "el", "nm": "dot", "p": {"a": 0, "k": [46, -20]}, "s": {"a": 0, "k": [6, 6]}},
                            {"ty": "fl", "nm": "token:head", "c": {"a": 0, "k": TOKEN_COLOR}, "o": {"a": 0, "k": 100}},
                        ]
                    )
                ],
                "ip": 0,
                "op": 90,
                "st": 0,
                "bm": 0,
            },
        ],
        "markers": [],
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, builder in (("scan", scan), ("check", check), ("grow", grow)):
        data = builder()
        target = OUT / f"{name}.json"
        target.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"{target.relative_to(OUT.parents[1])}: {target.stat().st_size} bytes, {len(data['layers'])} layers")


if __name__ == "__main__":
    main()
