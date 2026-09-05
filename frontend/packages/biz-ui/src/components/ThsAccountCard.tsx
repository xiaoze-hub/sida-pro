import { useCallback, useEffect, useState } from 'react'
import { QrCode, RefreshCw, LogOut, CheckCircle2, XCircle } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { cn } from '@panwatch/base-ui'
import SectionHeader from '@panwatch/biz-ui/components/SectionHeader'

/**
 * 同花顺账号维护卡(2026-09-05, v0.5.3)。
 * SDK 模式(正式/游客) + 扫码登录态 + 登出 + 已验证能力一览。
 * 自包含数据加载, 设置页 sec-ths 直接挂载。
 */

interface ThsCapability {
  key: string
  label: string
  ok: boolean
}

interface ThsAccount {
  mode: 'formal' | 'guest'
  mode_label: string
  session: {
    logged_in?: boolean
    account?: string
    userid?: string
    expires?: string
    need_scan?: boolean
  } | null
  capabilities: ThsCapability[]
  note?: string
}

export default function ThsAccountCard() {
  const [acct, setAcct] = useState<ThsAccount | null>(null)
  const [loading, setLoading] = useState(true)
  const [qr, setQr] = useState('')
  const [qrLoading, setQrLoading] = useState(false)
  const [err, setErr] = useState('')

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true)
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

  const getQrcode = async () => {
    setQrLoading(true)
    setQr('')
    setErr('')
    try {
      const d = await fetchAPI<any>('/ths/qrcode', { method: 'POST' })
      const data = d?.data ?? d
      setQr(data?.img_base64 ?? '')
      const qrid = data?.qrid
      if (!qrid) {
        setErr('未获取到二维码')
        return
      }
      for (let i = 0; i < 40; i++) {
        await new Promise((r) => setTimeout(r, 3000))
        try {
          const r = await fetchAPI<any>(`/ths/qrcode/${qrid}`)
          const rd = r?.data ?? r
          if (rd?.logged_in) {
            setQr('')
            await load(true)
            return
          }
        } catch {
          /* 继续轮询 */
        }
      }
      setErr('扫码超时,请重新生成')
    } catch (e) {
      setErr(e instanceof Error ? e.message : '生成二维码失败')
    } finally {
      setQrLoading(false)
    }
  }

  const logout = async () => {
    setErr('')
    try {
      await fetchAPI<any>('/ths/logout', { method: 'POST' })
      setQr('')
      await load(true)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '登出失败')
    }
  }

  const sess = acct?.session
  const formal = acct?.mode === 'formal'

  return (
    <div>
      <SectionHeader
        title="同花顺账号"
        action={
          <>
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
            <button
              type="button"
              onClick={() => void getQrcode()}
              disabled={qrLoading}
              className="inline-flex h-7 items-center gap-1 rounded-md bg-primary px-2.5 text-[11px] font-medium text-primary-foreground transition-colors hover:bg-primary/90"
            >
              <QrCode className="h-3.5 w-3.5" />
              {qrLoading ? '等待扫码...' : sess?.logged_in ? '重新扫码' : '扫码登录'}
            </button>
            {sess?.logged_in ? (
              <button
                type="button"
                onClick={() => void logout()}
                className="inline-flex h-7 items-center gap-1 px-2 text-[11px] text-muted-foreground transition-colors hover:text-destructive"
                title="清除登录态"
              >
                <LogOut className="h-3.5 w-3.5" />
                登出
              </button>
            ) : null}
          </>
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
            {sess?.logged_in ? (
              <>
                <div>
                  <span className="text-muted-foreground">账号: </span>
                  <span className="font-mono">{sess.account}</span>
                </div>
                <div>
                  <span className="text-muted-foreground">过期: </span>
                  <span className="font-mono">{sess.expires?.replace('T', ' ').slice(0, 19)}</span>
                </div>
                <div className="text-emerald-700 dark:text-emerald-500">✓ 已登录 · 自动续期</div>
              </>
            ) : (
              <div className="text-muted-foreground">未登录。扫码登录获取登录态,用于解锁同花顺数据源。</div>
            )}
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
      {qr ? (
        <div className="mt-3 flex items-center gap-4">
          <img src={`data:image/png;base64,${qr}`} alt="同花顺扫码登录" className="h-40 w-40 rounded-md border border-border/50" />
          <div className="text-[11px] text-muted-foreground">
            <p>用手机同花顺 APP 扫描二维码</p>
            <p className="mt-1">有效期约 3 分钟,扫码后自动登录</p>
          </div>
        </div>
      ) : null}
    </div>
  )
}
