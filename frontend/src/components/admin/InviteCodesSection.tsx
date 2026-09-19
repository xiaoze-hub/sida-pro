/**
 * Admin → 邀请码管理区块（内部使用模式，2026-09-19）。
 *
 * 为什么单独一个组件：Admin.tsx 已近 900 行，塞进去会继续膨胀；邀请码有独立的
 * 生成/列表/审计三块交互，拆出来更清楚。**权限仍由后端保证**（端点 owner only），
 * 这里不做任何"前端当权限"的事。
 */
import { useCallback, useMemo, useState } from 'react'
import { Loader2, Plus, Ticket, Copy, Ban, RefreshCw } from 'lucide-react'
import { inviteCodesApi, type InviteCodeItem, type InviteCodeUse } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { useApiQuery } from '@/hooks/useApiQuery'
import { Section, EmptyState } from '@/components/dev/DevPageLayout'

const MODE_LABEL: Record<string, string> = {
  invite: '邀请制（需邀请码）',
  open: '开放注册',
  closed: '已关闭注册',
}

export function InviteCodesSection() {
  const { toast } = useToast()
  const [note, setNote] = useState('')
  const [maxUses, setMaxUses] = useState('1')
  const [expiresDays, setExpiresDays] = useState('')
  const [creating, setCreating] = useState(false)
  const [lastCreated, setLastCreated] = useState<string>('')
  const [showUses, setShowUses] = useState(false)

  const { data, refetch, isLoading } = useApiQuery<{ items: InviteCodeItem[]; mode: string }>(
    ['admin-invite-codes'],
    '/admin/invite-codes',
  )
  const items = useMemo(() => data?.items ?? [], [data])
  const mode = data?.mode ?? 'invite'

  const { data: useData } = useApiQuery<{ items: InviteCodeUse[] }>(
    ['admin-invite-uses'],
    showUses ? '/admin/invite-codes/uses' : '',
    { enabled: showUses },
  )
  const uses = useMemo(() => useData?.items ?? [], [useData])

  const onCreate = useCallback(async () => {
    setCreating(true)
    try {
      const item = await inviteCodesApi.create({
        note: note.trim(),
        max_uses: Number(maxUses) || 1,
        expires_in_days: expiresDays.trim() ? Number(expiresDays) : null,
      })
      setLastCreated(item.code)
      setNote('')
      toast('邀请码已生成', 'success')
      void refetch()
    } catch (e) {
      toast(e instanceof Error ? e.message : '生成失败', 'error')
    } finally {
      setCreating(false)
    }
  }, [note, maxUses, expiresDays, refetch, toast])

  const onDisable = useCallback(
    async (code: string) => {
      try {
        await inviteCodesApi.disable(code)
        toast('已停用', 'success')
        void refetch()
      } catch (e) {
        toast(e instanceof Error ? e.message : '停用失败', 'error')
      }
    },
    [refetch, toast],
  )

  const copy = useCallback(
    async (code: string) => {
      try {
        await navigator.clipboard.writeText(code)
        toast('已复制', 'success')
      } catch {
        toast('复制失败，请手动选择', 'error')
      }
    },
    [toast],
  )

  return (
    <Section
      id="sec-invite"
      title="邀请码管理"
      description={`当前注册模式：${MODE_LABEL[mode] ?? mode}（内测邀请制，准入权在管理员）`}
      action={
        <Button size="sm" variant="outline" className="h-8" onClick={() => refetch()}>
          <RefreshCw className="h-3.5 w-3.5" />
          <span className="hidden sm:inline">刷新</span>
        </Button>
      }
    >
      {/* 生成 */}
      <div className="mb-4 grid gap-3 sm:grid-cols-[1fr_100px_120px_auto] sm:items-end">
        <div>
          <Label className="text-[12px]">备注（给谁用的，便于回收）</Label>
          <Input
            className="h-9"
            value={note}
            onChange={e => setNote(e.target.value)}
            placeholder="如：给黄磊"
          />
        </div>
        <div>
          <Label className="text-[12px]">可用次数</Label>
          <Input
            className="h-9"
            type="number"
            min={1}
            value={maxUses}
            onChange={e => setMaxUses(e.target.value)}
          />
        </div>
        <div>
          <Label className="text-[12px]">有效期（天，可空）</Label>
          <Input
            className="h-9"
            type="number"
            min={1}
            value={expiresDays}
            onChange={e => setExpiresDays(e.target.value)}
            placeholder="永久"
          />
        </div>
        <Button className="h-9" onClick={onCreate} disabled={creating}>
          {creating ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
          生成
        </Button>
      </div>

      {lastCreated && (
        <div className="mb-4 flex items-center gap-3 rounded-lg border border-primary/30 bg-primary/5 px-3 py-2">
          <Ticket className="h-4 w-4 text-primary" />
          <span className="text-[12px] text-muted-foreground">刚生成的邀请码（只在这里显示一次）：</span>
          <code className="font-mono text-[13px] font-semibold tracking-widest text-foreground">{lastCreated}</code>
          <Button size="sm" variant="ghost" className="h-7" onClick={() => copy(lastCreated)}>
            <Copy className="h-3.5 w-3.5" />
            复制
          </Button>
        </div>
      )}

      {/* 列表 */}
      {isLoading ? (
        <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> 加载中…
        </div>
      ) : items.length === 0 ? (
        <EmptyState icon={<Ticket className="h-8 w-8" />} title="还没有邀请码" desc="生成一个发给需要开通账号的人" />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-[12px]">
            <thead>
              <tr className="border-b border-border/40 text-left text-muted-foreground">
                <th className="py-2 pr-4 font-normal">邀请码</th>
                <th className="py-2 pr-4 font-normal">备注</th>
                <th className="py-2 pr-4 font-normal">已用/可用</th>
                <th className="py-2 pr-4 font-normal">有效期</th>
                <th className="py-2 pr-4 font-normal">状态</th>
                <th className="py-2 font-normal">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map(it => (
                <tr key={it.code} className="border-b border-border/20">
                  <td className="py-2 pr-4 font-mono tracking-wider">{it.code}</td>
                  <td className="py-2 pr-4 text-muted-foreground">{it.note || '--'}</td>
                  <td className="py-2 pr-4 tabular-nums">
                    {it.used_count}/{it.max_uses}
                  </td>
                  <td className="py-2 pr-4 text-muted-foreground">
                    {it.expires_at ? it.expires_at.slice(0, 10) : '永久'}
                  </td>
                  <td className="py-2 pr-4">
                    {it.disabled ? (
                      <span className="text-muted-foreground">已停用</span>
                    ) : it.remaining <= 0 ? (
                      <span className="text-muted-foreground">已用尽</span>
                    ) : (
                      <span className="text-emerald-500">可用</span>
                    )}
                  </td>
                  <td className="py-2">
                    <div className="flex items-center gap-1">
                      <Button size="sm" variant="ghost" className="h-7" onClick={() => copy(it.code)}>
                        <Copy className="h-3.5 w-3.5" />
                      </Button>
                      {!it.disabled && it.remaining > 0 && (
                        <Button size="sm" variant="ghost" className="h-7" onClick={() => onDisable(it.code)}>
                          <Ban className="h-3.5 w-3.5" />
                        </Button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* 使用审计 */}
      <div className="mt-4">
        <Button size="sm" variant="ghost" className="h-7" onClick={() => setShowUses(v => !v)}>
          {showUses ? '收起使用审计' : '查看使用审计（谁在什么时候用什么 IP 注册的）'}
        </Button>
        {showUses && (
          <div className="mt-2 overflow-x-auto">
            {uses.length === 0 ? (
              <p className="text-[12px] text-muted-foreground">还没有使用记录。</p>
            ) : (
              <table className="w-full text-[12px]">
                <thead>
                  <tr className="border-b border-border/40 text-left text-muted-foreground">
                    <th className="py-2 pr-4 font-normal">邀请码</th>
                    <th className="py-2 pr-4 font-normal">使用人</th>
                    <th className="py-2 pr-4 font-normal">IP</th>
                    <th className="py-2 font-normal">时间</th>
                  </tr>
                </thead>
                <tbody>
                  {uses.map((u, i) => (
                    <tr key={`${u.code}-${i}`} className="border-b border-border/20">
                      <td className="py-2 pr-4 font-mono">{u.code}</td>
                      <td className="py-2 pr-4">{u.username || '--'}</td>
                      <td className="py-2 pr-4 font-mono text-muted-foreground">{u.client_ip || '--'}</td>
                      <td className="py-2 text-muted-foreground">{u.used_at?.slice(0, 19)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </div>
    </Section>
  )
}
