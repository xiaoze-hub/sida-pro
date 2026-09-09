import { Button } from '@panwatch/base-ui/components/ui/button'
import { CHANNEL_TYPE_FIELDS } from './types'
import { MonitorUp } from 'lucide-react'
import { Pencil } from 'lucide-react'
import { Plus } from 'lucide-react'
import { QrCode } from 'lucide-react'
import { Send } from 'lucide-react'
import { Star } from 'lucide-react'
import { Switch } from '@panwatch/base-ui/components/ui/switch'
import { Trash2 } from 'lucide-react'
import { browserNotificationsSupported } from '@/lib/browser-notifications'
import { useSettings } from './context'

export function NotifySection() {
  const {
    channels,
    sectionMatches,
    testing,
    browserPushEnabled,
    browserPushTesting,
    wechatBindInfo,
    wechatBindStarting,
    wechatUnbinding,
    openChannelDialog,
    deleteChannel,
    setDefaultChannel,
    toggleChannelEnabled,
    testChannel,
    startWechatBind,
    unbindWechat,
    toggleBrowserPush,
    testBrowserPush,
  } = useSettings()
  return (
    <>
  {/* Notify Channel Section */}
  <section id="sec-notify" className="border-t border-border/40 pt-4 md:pt-5 lg:col-span-5" style={{ display: sectionMatches('sec-notify') ? undefined : 'none' }}>
    <div className="flex items-start justify-between mb-4 md:mb-5 gap-3">
      <div>
        <h3 className="text-[12px] md:text-[13px] font-semibold text-foreground">通知渠道</h3>
        <p className="text-[11px] text-muted-foreground mt-1">推送到 Telegram/Bark 等渠道</p>
      </div>
      <Button size="sm" className="h-8" onClick={() => openChannelDialog()}>
        <Plus className="w-3.5 h-3.5" />
        <span className="hidden sm:inline">添加</span>
      </Button>
    </div>
    <div className="mb-3 rounded-md border border-border/50 bg-accent/20 p-3.5">
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-start gap-2.5">
          <MonitorUp className="mt-0.5 h-4 w-4 flex-shrink-0 text-primary" />
          <div>
            <div className="text-[12px] font-medium text-foreground">电脑 Web 推送</div>
            <p className="mt-0.5 text-[10.5px] text-muted-foreground">页面打开或在后台运行时，新消息直接弹出电脑系统通知。需要 HTTPS 或 localhost。</p>
          </div>
        </div>
        <Switch
          checked={browserPushEnabled}
          disabled={!browserNotificationsSupported()}
          onCheckedChange={value => void toggleBrowserPush(value)}
        />
      </div>
      {browserPushEnabled && (
        <Button
          variant="ghost"
          size="sm"
          className="mt-2 h-7 px-2 text-[11px]"
          disabled={browserPushTesting}
          onClick={() => void testBrowserPush()}
        >
          <Send className="h-3.5 w-3.5" />
          {browserPushTesting ? '测试中…' : '测试电脑通知'}
        </Button>
      )}
    </div>
    {/* 个人微信(iLink): 扫码绑定 */}
    <div className="mb-3 rounded-md border border-border/50 bg-accent/20 p-3.5">
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-start gap-2.5">
          <QrCode className="mt-0.5 h-4 w-4 flex-shrink-0 text-primary" />
          <div className="min-w-0">
            <div className="text-[12px] font-medium text-foreground">个人微信(iLink)</div>
            {wechatBindInfo?.account_id ? (
              <p className="mt-0.5 text-[10.5px] text-muted-foreground">
                已绑定：
                <span className="font-mono text-foreground">{wechatBindInfo.user_id || wechatBindInfo.account_id}</span>
                {wechatBindInfo.nickname ? `（${wechatBindInfo.nickname}）` : ''}
              </p>
            ) : (
              <p className="mt-0.5 text-[10.5px] text-muted-foreground">扫码绑定个人微信，绑定成功后自动创建通知渠道</p>
            )}
          </div>
        </div>
        {wechatBindInfo?.account_id ? (
          <Button
            variant="ghost"
            size="sm"
            className="h-7 shrink-0 px-2 text-[11px] hover:text-destructive"
            onClick={() => void unbindWechat()}
            disabled={wechatUnbinding}
          >
            <Trash2 className="h-3.5 w-3.5" />
            {wechatUnbinding ? '解除中…' : '解除绑定'}
          </Button>
        ) : (
          <Button
            size="sm"
            className="h-7 shrink-0 px-2 text-[11px]"
            onClick={() => void startWechatBind()}
            disabled={wechatBindStarting}
          >
            <QrCode className="h-3.5 w-3.5" />
            {wechatBindStarting ? '发起中…' : '绑定个人微信'}
          </Button>
        )}
      </div>
    </div>
    {channels.length === 0 ? (
      <p className="text-[13px] text-muted-foreground text-center py-6">暂无通知渠道，点击"添加"创建</p>
    ) : (
      <div className="space-y-3">
        {channels.map(ch => (
          <div key={ch.id} className="flex items-center justify-between gap-3 border-b border-border/40 py-2.5 transition-colors hover:bg-accent/20">
            <div className="flex items-center gap-3 min-w-0">
              {ch.is_default && <Star className="w-3.5 h-3.5 text-amber-700 dark:text-amber-500 flex-shrink-0" />}
              <div className="min-w-0">
                <span className="text-[13px] font-medium text-foreground">{ch.name}</span>
                <p className="text-[11px] text-muted-foreground mt-0.5">{CHANNEL_TYPE_FIELDS[ch.type]?.label || ch.type}</p>
              </div>
            </div>
            <div className="flex items-center gap-1 flex-shrink-0">
              <Button
                variant="ghost" size="sm" className="h-7 px-2 text-[11px]"
                onClick={() => testChannel(ch.id)}
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
                <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => setDefaultChannel(ch.id)} title="设为默认">
                  <Star className="w-3.5 h-3.5" />
                </Button>
              )}
              <Switch checked={ch.enabled} onCheckedChange={() => toggleChannelEnabled(ch)} />
              <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => openChannelDialog(ch)}>
                <Pencil className="w-3.5 h-3.5" />
              </Button>
              <Button variant="ghost" size="icon" className="h-7 w-7 hover:text-destructive" onClick={() => deleteChannel(ch.id)}>
                <Trash2 className="w-3.5 h-3.5" />
              </Button>
            </div>
          </div>
        ))}
      </div>
    )}
  </section>
    </>
  )
}
