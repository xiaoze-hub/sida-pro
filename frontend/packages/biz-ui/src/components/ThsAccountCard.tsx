import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, CheckCircle2, XCircle } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { cn } from '@panwatch/base-ui'
import SectionHeader from '@panwatch/biz-ui/components/SectionHeader'

/**
 * 同花顺账号卡(2026-09-05 精简: 扫码登录已下线, 只展示 SDK 模式 + 已验证能力)。
 * SDK 只认账号密码(THS_USERNAME/PASSWORD env), 自包含数据加载, 设置页 sec-ths 直接挂载。
 */

interface ThsCapability {
  key: string
  label: string
  ok: boolean
}

interface ThsAccount {
  mode: 'formal' | 'guest'
  mode_label: string
  source?: 'db' | 'env' | 'none'
  capabilities: ThsCapability[]
  note?: string
}

export default function ThsAccountCard() {
  const [acct, setAcct] = useState<ThsAccount | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setErr('')
    try {
      const d = await fetchAPI<any>('/ths/account')
      setAcct(d?.data ?? d)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const formal = acct?.mode === 'formal'

  return (
    <div>
      <SectionHeader
        title="同花顺账号"
        action={
          <button
            type="button"
            onClick={() => void load()}
            disabled={loading}
            className="inline-flex h-7 items-center gap-1 px-2 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
            title="刷新"
          >
            <RefreshCw className={cn('h-3.5 w-3.5', loading && 'animate-spin')} />
            刷新
          </button>
        }
      />
      {err ? <div className="mb-2 text-[11px] text-destructive">{err}</div> : null}
      {loading && !acct ? (
        <div className="text-[12px] text-muted-foreground">加载中…</div>
      ) : (
        <div className="rounded-md bg-accent/30 p-3.5">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-[12px]">
            <div>
              <span className="text-muted-foreground">SDK 模式: </span>
              <span className={cn('font-semibold', formal ? 'text-stock-up' : 'text-amber-600 dark:text-amber-400')}>
                {acct?.mode_label ?? '--'}
              </span>
            </div>
            <div className="text-muted-foreground">
              账号密码在设置页维护(ths_username/ths_sdk_password, DB 优先
              {acct?.source === 'db' ? ', 当前用设置页的值' : acct?.source === 'env' ? ', 当前用容器环境变量' : ''})
            </div>
          </div>
          {acct?.capabilities?.length ? (
            <div className="mt-2.5 flex flex-wrap gap-x-4 gap-y-1 border-t border-border/40 pt-2.5 text-[11px]">
              {acct.capabilities.map((c) => (
                <span key={c.key} className="inline-flex items-center gap-1 text-muted-foreground">
                  {c.ok ? (
                    <CheckCircle2 className="h-3 w-3 text-emerald-600 dark:text-emerald-500" />
                  ) : (
                    <XCircle className="h-3 w-3 text-muted-foreground/50" />
                  )}
                  {c.label}
                </span>
              ))}
            </div>
          ) : null}
          {acct?.note && !formal ? (
            <div className="mt-1.5 text-[10px] text-amber-700 dark:text-amber-400">{acct.note}</div>
          ) : null}
        </div>
      )}
    </div>
  )
}
