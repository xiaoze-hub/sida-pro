import type { EvidenceRowAudit } from '@sida/api'

/**
 * 依据行数字核对（2026-10-30）：只陈述"这条依据里的数字在本轮取数里找不到"。
 *
 * 它**不说**：这句来自哪个工具（链路没有「依据行 → step」的关联）、这个数是编造的。
 * 因此文案统一是"本轮取数里未出现"，并且只在真的有查不到的数字时才出现 —— 全对上就不摆
 * 一块"0 问题"的装饰（那也是变相的暗示）。
 */
export default function EvidenceRowAuditPanel({ audit }: { audit: EvidenceRowAudit }) {
  const rows = (audit?.rows || []).filter((r) => (r.unverified || []).length > 0)
  if (!rows.length) return null
  const bad = rows.reduce((n, r) => n + r.unverified.length, 0)
  return (
    <details
      data-testid="copilot-row-audit"
      className="mt-2 rounded bg-role-risk/5 px-2 py-1.5 text-[12px]"
    >
      <summary className="cursor-pointer select-none text-role-risk">
        依据行数字核对：{bad} 个数字本轮取数里未出现
      </summary>
      <ul className="mt-1 space-y-1">
        {rows.map((r) => (
          <li key={r.index} className="text-muted-foreground">
            <span className="text-foreground" title={r.text}>
              {r.text.length > 60 ? `${r.text.slice(0, 60)}…` : r.text}
            </span>
            <span className="ml-1 text-role-risk">→ {r.unverified.join('、')}</span>
          </li>
        ))}
      </ul>
      {audit.truncated ? (
        <p className="mt-1 text-role-watch">依据行过多，仅核对了前 {audit.rows.length} 行。</p>
      ) : null}
      <p className="mt-1 text-muted-foreground">
        只核对数字是否出现在本轮工具返回与注入上下文里；不判断来源归属，也不断言编造。请以工具原始读数为准。
      </p>
    </details>
  )
}
