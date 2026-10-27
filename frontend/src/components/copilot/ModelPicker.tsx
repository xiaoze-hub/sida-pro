/**
 * 参谋模型选择器（2026-10-25，工作台·选模型）。
 *
 * 说清楚一件事：**选中的模型真的会生效**，不是摆设。
 * 路径：本组件 PUT `/copilot/model` → `user_ai_prefs` → `get_model_for_scene(db,"chat",user)`
 * → chat.py 组装 AIClient 时读到的就是它（chat.py 一行没改）。
 *
 * 三条诚实约束：
 *  1. 来源必须如实显示：`跟随场景绑定` 与 `你选的` 是两件事，不能都写成模型名了事；
 *  2. 越权不可选：可选清单由后端按 `permissions.model_access` 算好，前端不再过滤（不猜权限）；
 *  3. 失败不静默：保存失败要显示服务端 detail，且**回滚本地显示**（否则用户以为改了）。
 */
import { useCallback, useEffect, useState } from 'react'
import { Check, ChevronDown, Loader2 } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { Popover, PopoverContent, PopoverTrigger } from '@panwatch/base-ui/components/ui/popover'

export interface ModelOption {
  id: number
  model: string
  service: string
  is_default: boolean
}

export interface CurrentModel {
  source: 'pref' | 'byok' | 'scene' | 'none' | string
  model_id: number | null
  model: string | null
  service: string | null
}

export interface CopilotModelState {
  scene: string
  current: CurrentModel
  models: ModelOption[]
}

const SOURCE_LABEL: Record<string, string> = {
  pref: '你选的',
  byok: '自带 Key',
  scene: '跟随场景绑定',
  none: '未配置',
}

export function sourceLabel(source: string | undefined | null): string {
  const key = String(source ?? '')
  return SOURCE_LABEL[key] ?? (key || '未知')
}

export function ModelPicker({ className = '' }: { className?: string }) {
  const [state, setState] = useState<CopilotModelState | null>(null)
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    setLoadError(null)
    try {
      const body = await fetchAPI<CopilotModelState>('/copilot/model', { signal })
      setState(body)
    } catch (exc) {
      setLoadError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const ctrl = new AbortController()
    void load(ctrl.signal)
    return () => ctrl.abort()
  }, [load])

  // 点外面收起（不写全局监听就没有"点空白关闭"，下拉会一直挡着输入框）
  // 2026-10-27: 改用 Radix Popover 后由它自己处理 outside-click / Escape / 焦点，
  // 手写监听已在下面删除（两套并存会互相抢着关）。

  const choose = async (modelId: number | null) => {
    if (saving) return
    const prev = state
    setSaving(true)
    setError(null)
    // 乐观更新只用于"立即反馈"，失败必须回滚（见 catch）
    if (state) {
      const picked = modelId === null ? null : state.models.find((m) => m.id === modelId) ?? null
      setState({
        ...state,
        current: modelId === null
          ? { source: 'scene', model_id: null, model: picked?.model ?? state.current.model, service: null }
          : {
              source: 'pref',
              model_id: modelId,
              model: picked?.model ?? null,
              service: picked?.service ?? null,
            },
      })
    }
    try {
      const body = await fetchAPI<CopilotModelState>('/copilot/model', {
        method: 'PUT',
        body: JSON.stringify({ model_id: modelId }),
        headers: { 'Content-Type': 'application/json' },
      } as RequestInit)
      setState(body)
      setOpen(false)
    } catch (exc) {
      setState(prev)
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setSaving(false)
    }
  }

  const current = state?.current
  const disabled = loading || saving || loadError !== null

  return (
    <div className={`relative ${className}`} data-testid="copilot-model-picker">
      <Popover open={open} onOpenChange={(v) => { if (!disabled) setOpen(v) }}>
        <PopoverTrigger asChild>
          <button
            type="button"
            disabled={disabled}
            aria-label="选择模型"
            title={
              loadError
                ? `读取模型列表失败：${loadError}`
                : '选择参谋本轮使用的模型（只在你已获授权的模型里）'
            }
            className="flex h-7 items-center gap-1 rounded-md px-2 text-[12px] text-muted-foreground hover:bg-muted hover:text-foreground disabled:cursor-not-allowed"
          >
            {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
            <span className="tabular-nums">{current?.model ?? (loadError ? '读取失败' : '--')}</span>
            <span className="text-[12px]">{sourceLabel(current?.source)}</span>
            <ChevronDown className="h-3 w-3" />
          </button>
        </PopoverTrigger>
        {/*
          2026-10-27 修「列表被上面挡住、看不到模型名」：
          原来的下拉是 `absolute bottom-full`（向上弹）挂在顶部工具条里, 一旦工具条靠近视口顶部,
          整块面板就翻到视口外 —— 用户只能看到被裁掉的一条。
          改为 Radix Popover: Portal 挂到 body(不受任何 overflow 容器裁剪), 自带碰撞检测
          会按可用空间自动上下翻转 + 左右避让; 列表高度吃 Radix 给出的可用高度, 短屏也不越界。
        */}
        <PopoverContent
          align="end"
          side="bottom"
          sideOffset={6}
          collisionPadding={8}
          className="w-[280px] p-1"
          data-testid="copilot-model-menu"
        >
          <button
            type="button"
            onClick={() => void choose(null)}
            className="flex w-full items-center justify-between rounded-lg px-2 py-1.5 text-left text-[12px] text-foreground hover:bg-muted"
          >
            <span>跟随场景绑定</span>
            {current?.source !== 'pref' ? <Check className="h-3 w-3" /> : null}
          </button>
          <div className="my-1 h-px bg-border" />
          <div
            className="overflow-y-auto"
            style={{ maxHeight: 'min(300px, var(--radix-popover-content-available-height))' }}
          >
            {(state?.models ?? []).length === 0 ? (
              <div className="px-2 py-2 text-[12px] text-muted-foreground">
                没有可用模型（权限或模型池为空）
              </div>
            ) : (
              (state?.models ?? []).map((m) => (
                <button
                  key={m.id}
                  type="button"
                  onClick={() => void choose(m.id)}
                  className="flex w-full items-center justify-between gap-2 rounded-lg px-2 py-1.5 text-left hover:bg-muted"
                >
                  <span className="flex min-w-0 flex-col">
                    <span className="truncate text-[12px] text-foreground">{m.model}</span>
                    <span className="truncate text-[12px] text-muted-foreground">
                      {m.service}
                      {m.is_default ? ' · 默认' : ''}
                    </span>
                  </span>
                  {current?.model_id === m.id ? <Check className="h-3 w-3 shrink-0" /> : null}
                </button>
              ))
            )}
          </div>
        </PopoverContent>
      </Popover>

      {/* 保存失败: 挂在触发按钮下方(不是上方), 顶部工具条贴着视口顶时不会被裁掉 */}
      {error ? (
        <div className="absolute left-0 top-full z-30 mt-1 w-[260px] rounded-lg border border-border bg-card p-2 text-[12px] text-stock-down">
          保存失败：{error}
        </div>
      ) : null}
    </div>
  )
}
