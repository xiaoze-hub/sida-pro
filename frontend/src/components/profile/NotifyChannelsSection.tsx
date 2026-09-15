import { useCallback, useEffect, useState } from 'react'
import { Bell, Pencil, Plus, Send, Star, Trash2 } from 'lucide-react'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Switch } from '@panwatch/base-ui/components/ui/switch'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { fetchAPI, type NotifyChannel } from '@panwatch/api'
import { useNavigate } from 'react-router-dom'

/**
 * 个人中心 · 通知渠道(B.4, 2026-09-18)
 * 自包含: 自己拉 /api/channels, 不依赖 SettingsContext。
 * 添加/编辑跳转 /settings?tab=settings(复用既有对话框)。
 */
export function NotifyChannelsSection() {
  const { toast } = useToast()
  const navigate = useNavigate()
  const [channels, setChannels] = useState<NotifyChannel[]>([])
  const [loading, setLoading] = useState(true)
  const [testing, setTesting] = useState<number | null>(null)

  const load = useCallback(async () => {
    try {
      const list = await fetchAPI<NotifyChannel[]>('/channels')
      setChannels(Array.isArray(list) ? list : [])
    } catch {
      // 静默: 个人中心加载失败不弹 toast
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const testChannel = async (id: number) => {
    setTesting(id)
    try {
      const r = await fetchAPI<{ message?: string }>(`/channels/${id}/test`, { method: 'POST' })
      toast(r?.message || '测试通知已发送', 'success')
    } catch (e) {
      toast(e instanceof Error ? e.message : '测试失败', 'error')
    } finally {
      setTesting(null)
    }
  }

  const toggleEnabled = async (ch: NotifyChannel) => {
    try {
      await fetchAPI(`/channels/${ch.id}`, { method: 'PUT', body: JSON.stringify({ enabled: !ch.enabled }) })
      void load()
    } catch {
      toast('操作失败', 'error')
    }
  }

  const setDefault = async (id: number) => {
    try {
      await fetchAPI(`/channels/${id}`, { method: 'PUT', body: JSON.stringify({ is_default: true }) })
      void load()
    } catch {
      toast('设置失败', 'error')
    }
  }

  const remove = async (id: number) => {
    if (!confirm('确定删除此通知渠道？')) return
    try {
      await fetchAPI(`/channels/${id}`, { method: 'DELETE' })
      void load()
    } catch (e) {
      toast(e instanceof Error ? e.message : '删除失败', 'error')
    }
  }

  return (
    <section className="border-t border-border/40 pt-4 md:pt-5 lg:col-span-12">
      <div className="flex items-start justify-between mb-4 gap-3">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <Bell className="w-4 h-4 text-primary" />
            <h3 className="text-[12px] md:text-[13px] font-semibold text-foreground">通知渠道</h3>
          </div>
          <p className="text-[11px] text-muted-foreground">
            管理你的推送通道。添加/编辑请前往
            <button
              type="button"
              className="mx-0.5 text-primary underline-offset-2 hover:underline"
              onClick={() => navigate('/settings?tab=settings')}
            >
              设置页
            </button>
            。
          </p>
        </div>
        <Button size="sm" variant="outline" className="h-8" onClick={() => navigate('/settings?tab=settings')}>
          <Plus className="w-3.5 h-3.5" />
          <span className="hidden sm:inline">添加</span>
        </Button>
      </div>

      {loading ? (
        <p className="text-[12px] text-muted-foreground py-4">加载中…</p>
      ) : channels.length === 0 ? (
        <p className="text-[12px] text-muted-foreground py-4">暂无通知渠道，点击「添加」前往设置页创建。</p>
      ) : (
        <div className="space-y-2">
          {channels.map(ch => (
            <div
              key={ch.id}
              className="flex items-center justify-between gap-3 border-b border-border/40 px-1 py-2.5"
            >
              <div className="flex items-center gap-2.5 min-w-0">
                {ch.is_default && (
                  <Star className="w-3.5 h-3.5 text-amber-600 dark:text-amber-400 shrink-0" />
                )}
                <div className="min-w-0">
                  <span className="text-[12px] font-medium text-foreground">{ch.name}</span>
                  <p className="text-[10px] text-muted-foreground mt-0.5">{ch.type}</p>
                </div>
              </div>
              <div className="flex items-center gap-1 shrink-0">
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 px-2 text-[11px]"
                  onClick={() => void testChannel(ch.id)}
                  disabled={testing === ch.id || !ch.enabled}
                  title="发送测试"
                >
                  {testing === ch.id ? (
                    <span className="w-3 h-3 border-2 border-current/30 border-t-current rounded-full animate-spin" />
                  ) : (
                    <><Send className="w-3.5 h-3.5" />测试</>
                  )}
                </Button>
                {!ch.is_default && (
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    onClick={() => void setDefault(ch.id)}
                    title="设为默认"
                  >
                    <Star className="w-3.5 h-3.5" />
                  </Button>
                )}
                <Switch checked={ch.enabled} onCheckedChange={() => void toggleEnabled(ch)} />
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7"
                  onClick={() => navigate('/settings?tab=settings')}
                  title="编辑"
                >
                  <Pencil className="w-3.5 h-3.5" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7 hover:text-destructive"
                  onClick={() => void remove(ch.id)}
                  title="删除"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

export default NotifyChannelsSection
