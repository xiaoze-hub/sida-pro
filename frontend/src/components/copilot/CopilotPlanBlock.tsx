import { Compass } from 'lucide-react'
import { safeFixed, safeNum } from '@/lib/format'
import { AUDIENCE_LABEL, NO_PLAN_HINT, SECTION_PLAN } from '@/lib/copilot/copy'
import type { ActionPlan } from '@/lib/copilot/types'

/**
 * 操盘预案（持仓 / 空仓两栏）。
 *
 * 为什么两栏都给：用户可能是持仓者也可能是空仓者，只给一栏等于让另一半人自己脑补。
 * 有持仓时把「持仓者」置顶（`hasPosition` 由工作台传入，未知时按空仓在前）。
 *
 * 价位一律 `safeFixed`：后端给 null（关键位缺失）就打 `--`，**不补 0**。
 */
export function CopilotPlanBlock({
  plans,
  hasPosition,
}: {
  plans: ActionPlan[]
  hasPosition?: boolean
}) {
  const list = (plans || []).slice(0, 2)
  const ordered = hasPosition
    ? [...list].sort((a, b) => (a.audience === 'holder' ? -1 : 1) - (b.audience === 'holder' ? -1 : 1))
    : list

  return (
    <section className="card-surface rounded-lg bg-s2 p-2" data-testid="copilot-plan">
      <div className="mb-1 flex items-center gap-1.5 text-[12px] font-medium text-muted-foreground">
        <Compass className="h-3 w-3" />
        {SECTION_PLAN}
      </div>

      {ordered.length === 0 ? (
        <div className="text-[12px] text-muted-foreground">{NO_PLAN_HINT}</div>
      ) : (
        <div className="grid gap-1.5 md:grid-cols-2">
          {ordered.map((p) => {
            const active =
              hasPosition !== undefined && p.audience === (hasPosition ? 'holder' : 'empty')
            return (
              <div
                key={p.audience}
                className={"card-surface " + (`rounded p-1.5 ${
 active ? ' bg-s3' : ''
 }`)}
                data-testid={`copilot-plan-${p.audience}`}
              >
                <div className="flex items-center gap-1 text-[12px] text-muted-foreground">
                  {AUDIENCE_LABEL[p.audience]}
                  {active ? <span className="text-role-watch">· 你的位置</span> : null}
                </div>
                <div className="mt-0.5 text-[12px] font-medium text-foreground">{p.title}</div>
                <div className="mt-0.5 text-[12px] leading-relaxed text-muted-foreground">{p.text}</div>
                {p.triggers?.length ? (
                  <ul className="mt-1 space-y-0.5">
                    {p.triggers.slice(0, 3).map((t, i) => (
                      <li key={`${p.audience}-${i}`} className="text-[12px] text-muted-foreground">
                        <span className="font-mono text-foreground">
                          {safeNum(t.price) === null ? '--' : safeFixed(t.price, 2)}
                        </span>
                        {' · '}
                        {t.note}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}
