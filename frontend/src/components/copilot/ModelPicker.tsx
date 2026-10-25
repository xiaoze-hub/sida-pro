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
import { useCallback, useEffect, useRef, useState } from 'react'
import { Check, ChevronDown, Loader2 } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'

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
  const boxRef = useRef<HTMLDivElement | null>(null)

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
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

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
    <div ref={boxRef} className={`relative ${className}`} data-testid="copilot-model-picker">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={disabled}
        aria-label="选择模型"
        aria-expanded={open}
        title={
          loadError
            ? `读取模型列表失败：${loadError}`
            : '选择参谋本轮使用的模型（只在你已获授权的模型里）'
        }
        className="flex h-9 items-center gap-1 rounded-lg border border-border/60 px-2 text-[11px] text-muted-foreground hover:text-foreground disabled:cursor-not-allowed"
      >
        {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
        <span className="tabular-nums">{current?.model ?? (loadError ? '读取失败' : '--')}</span>
        <span className="text-[11px]">{sourceLabel(current?.source)}</span>
        <ChevronDown className="h-3 w-3" />
      </button>

      {open ? (
        <div className="absolute bottom-full right-0 z-30 mb-1 w-[260px] rounded-xl border border-border bg-card p-1">
          <button
            type="button"
            onClick={() => void choose(null)}
            className="flex w-full items-center justify-between rounded-lg px-2 py-1.5 text-left text-[12px] text-foreground hover:bg-muted"
          >
            <span>跟随场景绑定</span>
            {current?.source !== 'pref' ? <Check className="h-3 w-3" /> : null}
          </button>
          <div className="my-1 h-px bg-border" />
          <div className="max-h-[260px] overflow-y-auto">
            {(state?.models ?? []).length === 0 ? (
              <div className="px-2 py-2 text-[11px] text-muted-foreground">
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
                    <span className="truncate text-[11px] text-muted-foreground">
                      {m.service}
                      {m.is_default ? ' · 默认' : ''}
                    </span>
                  </span>
                  {current?.model_id === m.id ? <Check className="h-3 w-3 shrink-0" /> : null}
                </button>
              ))
            )}
          </div>
        </div>
      ) : null}

      {error ? (
        <div className="absolute bottom-full right-0 mb-1 w-[260px] rounded-lg border border-border bg-card p-2 text-[11px] text-stock-down">
          保存失败：{error}
        </div>
      ) : null}
    </div>
  )
}
