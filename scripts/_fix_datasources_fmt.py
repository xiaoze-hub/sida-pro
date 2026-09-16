"""KI-015: DataSources 预览区裸 toFixed → safeFixed。"""
from pathlib import Path

p = Path("frontend/src/pages/DataSources.tsx")
text = p.read_text(encoding="utf-8")
repls = [
    ("{quoteItem.price?.toFixed(2)}", "{safeFixed(quoteItem.price)}"),
    ("{quoteItem.change_pct?.toFixed(2)}%", "{safeFixed(quoteItem.change_pct)}%"),
    ("{klineItem.last_close?.toFixed(2)}", "{safeFixed(klineItem.last_close)}"),
    ("PE {fundItem.pe_ttm?.toFixed(2) ?? '-'}", "PE {safeFixed(fundItem.pe_ttm, 2, '-')}"),
    ("PB {fundItem.pb?.toFixed(2) ?? '-'}", "PB {safeFixed(fundItem.pb, 2, '-')}"),
    ("ROE {fundItem.roe?.toFixed(2) ?? '-'}%", "ROE {safeFixed(fundItem.roe, 2, '-')}%"),
    (
        "{((flowItem.main_net ?? 0) / 10000).toFixed(2)}万",
        "{safeNum(flowItem.main_net) == null ? '--' : safeFixed(Number(flowItem.main_net) / 10000)}万",
    ),
    ("{flowItem.main_pct?.toFixed(2)}%", "{safeFixed(flowItem.main_pct)}%"),
    (
        "{((dtItem.net_buy ?? 0) / 10000).toFixed(2)}万",
        "{safeNum(dtItem.net_buy) == null ? '--' : safeFixed(Number(dtItem.net_buy) / 10000)}万",
    ),
    (
        "{((marginItem.total_balance ?? 0) / 10000).toFixed(2)}万",
        "{safeNum(marginItem.total_balance) == null ? '--' : safeFixed(Number(marginItem.total_balance) / 10000)}万",
    ),
    ("{divItem.dividend_per_share?.toFixed(4) ?? '-'} 元/股", "{safeFixed(divItem.dividend_per_share, 4, '-')} 元/股"),
    (
        "{((nbItem.total_net ?? 0) / 10000).toFixed(2)}万",
        "{safeNum(nbItem.total_net) == null ? '--' : safeFixed(Number(nbItem.total_net) / 10000)}万",
    ),
    (
        "{((nbItem.hgt_net ?? 0) / 10000).toFixed(2)}万",
        "{safeNum(nbItem.hgt_net) == null ? '--' : safeFixed(Number(nbItem.hgt_net) / 10000)}万",
    ),
]
n = 0
for a, b in repls:
    c = text.count(a)
    if c:
        text = text.replace(a, b)
        n += c
p.write_text(text, encoding="utf-8")
print("replaced", n)
