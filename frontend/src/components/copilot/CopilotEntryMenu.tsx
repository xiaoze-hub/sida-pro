/**
 * 数智参谋「功能入口」菜单（2026-11-07）。
 *
 * 病根：参谋的四个全页视图（`/copilot/runs` · `/copilot/experiments` · `/copilot/tools` ·
 * `/copilot/proposals`）在 App.tsx 里有真实 Route、在 navigation.ts 里有页签标题，但参谋页
 * 上只有一条 `/copilot-governance` 文本链接 —— 其余页面只能靠记 URL 进。
 *
 * 为什么不给它们再加一条侧栏：那会变成**常驻第二条侧栏**，还会把「本轮过程」拆成两处
 * （页内窄面板 + 侧栏），用户已明确否掉。这里只在工具条原「治理」链接的位置上换成一个
 * 下拉菜单，零新增常驻结构。
 *
 * 三条边界（tests/components/copilot-entry-hub 钉住）：
 *  ① 只列**全页视图**；本页 rail 里的「过程 / 提案」是会话内嵌窄面板，菜单首行写明区别，
 *     同一个语义不再出现第二个入口；
 *  ② 每条都是普通 `<a href>`，打开即离开本页，**不挂载**第二个参谋实例、不发请求，
 *     会话列表与过程面板由服务端/现有 hook 维持，旧 URL 一个都不改；
 *  ③ href 必须是 `lib/navigation` 的 `SECONDARY_ROUTES` 路径 —— 不新造 URL、不产生死链，
 *     页签标题照旧由那份表派生。
 */
import { useEffect, useId, useRef, useState } from 'react'

export interface CopilotEntryLink {
  key: string
  href: string
  label: string
  /** 一句话说明这一页回答什么问题；缺失信息不写「无」，只写能证实的事。 */
  hint: string
}

/** 目的地：只收已有真实路由的全页视图（顺序 = 由「我这一轮」到「平台全局」）。 */
export const COPILOT_ENTRY_LINKS: readonly CopilotEntryLink[] = [
  { key: 'runs', href: '/copilot/runs', label: '任务回放', hint: '每轮问答的状态 / 配方 / 工具数 / 耗时' },
  { key: 'experiments', href: '/copilot/experiments', label: '经验库', hint: '配方改动前后的对照结论' },
  { key: 'tools', href: '/copilot/tools', label: '工具目录', hint: '平台工具的口径与挂载配方' },
  { key: 'agents', href: '/copilot/agents', label: 'Agent 目录', hint: '智能体、配方与版本指纹' },
  { key: 'proposals', href: '/copilot/proposals', label: '能力提案', hint: '配方没覆盖的维度，人工裁决' },
  { key: 'parallel', href: '/copilot/parallel', label: '并行诊断', hint: '多个子问题各自独立跑一轮' },
  { key: 'governance', href: '/copilot-governance', label: '治理拦截', hint: '预算 / 并发真的拦下来过的记录' },
]

export function CopilotEntryMenu() {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement | null>(null)
  const menuId = useId()

  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false)
    }
    // 与全站浮层一致：Escape 收起，不靠 hover 维持展开态。
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        data-testid="copilot-entry-menu-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={menuId}
        title="参谋全页视图入口：任务回放 / 经验库 / 工具目录 / Agent 目录 / 能力提案 / 并行诊断 / 治理拦截"
        onClick={() => setOpen((value) => !value)}
        className="h-7 rounded-md px-2 text-[12px] text-muted-foreground hover:bg-muted hover:text-foreground"
      >
        入口
      </button>
      {open ? (
        <div
          id={menuId}
          role="menu"
          aria-label="参谋功能入口"
          data-testid="copilot-entry-menu"
          className="absolute left-0 top-full z-30 mt-1 flex w-[232px] flex-col rounded-lg border border-border bg-s1 p-1 shadow-float"
        >
          <p className="px-2 py-1 text-[12px] leading-4 text-muted-foreground">
            全页视图；本页「过程 / 提案」仍是会话内嵌面板
          </p>
          {COPILOT_ENTRY_LINKS.map((item) => (
            <a
              key={item.key}
              role="menuitem"
              href={item.href}
              data-testid={`copilot-entry-${item.key}`}
              title={item.hint}
              className="rounded-md px-2 py-1.5 hover:bg-muted"
            >
              <span className="block text-[12px] text-foreground">{item.label}</span>
              <span className="block text-[12px] leading-4 text-muted-foreground">{item.hint}</span>
            </a>
          ))}
        </div>
      ) : null}
    </div>
  )
}
