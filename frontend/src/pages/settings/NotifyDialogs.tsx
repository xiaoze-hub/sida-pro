import { Button } from '@panwatch/base-ui/components/ui/button'
import { CHANNEL_TYPE_FIELDS } from './types'
import { Copy } from 'lucide-react'
import { Dialog } from '@panwatch/base-ui/components/ui/dialog'
import { DialogContent } from '@panwatch/base-ui/components/ui/dialog'
import { DialogDescription } from '@panwatch/base-ui/components/ui/dialog'
import { DialogHeader } from '@panwatch/base-ui/components/ui/dialog'
import { DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import { Eye } from 'lucide-react'
import { EyeOff } from 'lucide-react'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { QRCodeSVG } from 'qrcode.react'
import { QrCode } from 'lucide-react'
import { Select } from '@panwatch/base-ui/components/ui/select'
import { SelectContent } from '@panwatch/base-ui/components/ui/select'
import { SelectItem } from '@panwatch/base-ui/components/ui/select'
import { SelectTrigger } from '@panwatch/base-ui/components/ui/select'
import { SelectValue } from '@panwatch/base-ui/components/ui/select'
import { useSettings } from './context'

export function NotifyDialogs() {
  const {
    channelDialogOpen,
    setChannelDialogOpen,
    channelForm,
    setChannelForm,
    editChannelId,
    channelKeyVisible,
    setChannelKeyVisible,
    testing,
    wechatBindStarting,
    wechatQrOpen,
    wechatQr,
    wechatQrStatus,
    saveChannel,
    isChannelFormValid,
    startWechatBind,
    closeWechatQr,
    copyWechatLink,
  } = useSettings()
  return (
    <>
{/* 微信扫码绑定弹窗 */}
<Dialog open={wechatQrOpen} onOpenChange={open => { if (!open) closeWechatQr() }}>
  <DialogContent>
    <DialogHeader>
      <DialogTitle>绑定个人微信</DialogTitle>
      <DialogDescription>使用微信「扫一扫」扫描二维码，在手机上确认绑定</DialogDescription>
    </DialogHeader>
    <div className="mt-2 flex flex-col items-center gap-3">
      {wechatQr && (
        <>
          <div className="rounded-md border border-border/50 bg-white p-3">
            <QRCodeSVG value={wechatQr.qrcode_url} size={200} />
          </div>
          <p className="text-[11px] text-muted-foreground text-center">
            请用微信「扫一扫」扫描二维码(约 3 分钟内有效)
          </p>
          <button
            type="button"
            onClick={() => void copyWechatLink(wechatQr.qrcode_url)}
            className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-border/50 bg-accent/30 px-2.5 py-1.5 text-[11px] text-muted-foreground hover:text-foreground transition-colors"
            title="点击复制链接"
          >
            <Copy className="h-3 w-3 flex-shrink-0" />
            <span className="truncate font-mono">{wechatQr.qrcode_url}</span>
          </button>
          {wechatQrStatus === 'waiting' && (
            <p className="flex items-center gap-2 text-[11px] text-muted-foreground">
              <span className="h-3 w-3 border-2 border-current/30 border-t-current rounded-full animate-spin" />
              等待扫码确认…
            </p>
          )}
          {wechatQrStatus === 'expired' && (
            <p className="text-[11px] text-destructive">二维码已过期，请关闭后重新发起绑定</p>
          )}
        </>
      )}
      <div className="flex w-full justify-end gap-2 pt-1">
        <Button variant="ghost" onClick={closeWechatQr}>关闭</Button>
      </div>
    </div>
  </DialogContent>
</Dialog>

{/* Channel Dialog */}
<Dialog open={channelDialogOpen} onOpenChange={setChannelDialogOpen}>
  <DialogContent>
    <DialogHeader>
      <DialogTitle>{editChannelId ? '编辑通知渠道' : '添加通知渠道'}</DialogTitle>
      <DialogDescription>配置通知推送方式</DialogDescription>
    </DialogHeader>
    <div className="space-y-4 mt-2">
      {channelForm.type !== 'wechat_ilink' && (
        <div>
          <Label>名称</Label>
          <Input
            value={channelForm.name}
            onChange={e => setChannelForm({ ...channelForm, name: e.target.value })}
            placeholder="如 我的 Telegram"
           aria-label="如 我的 Telegram"/>
        </div>
      )}
      <div>
        <Label>类型</Label>
        <Select
          value={channelForm.type}
          onValueChange={val => setChannelForm({ ...channelForm, type: val, config: {} })}
        >
          <SelectTrigger>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {Object.entries(CHANNEL_TYPE_FIELDS).map(([key, def]) => (
              <SelectItem key={key} value={key}>{def.label}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {channelForm.type === 'wechat_ilink' ? (
        <div className="rounded-md border border-border/50 bg-accent/20 p-4">
          <div className="flex items-start gap-2.5">
            <QrCode className="mt-0.5 h-4 w-4 flex-shrink-0 text-primary" />
            <div>
              <div className="text-[12px] font-medium text-foreground">扫码绑定个人微信</div>
              <p className="mt-0.5 text-[10.5px] text-muted-foreground">
                无需填写地址与密钥。点击下方按钮，用微信扫码确认后自动创建「个人微信」渠道。
              </p>
            </div>
          </div>
          <Button className="mt-3" size="sm" onClick={() => void startWechatBind()} disabled={wechatBindStarting}>
            <QrCode className="h-3.5 w-3.5" />
            {wechatBindStarting ? '发起中…' : '绑定个人微信'}
          </Button>
        </div>
      ) : (
        CHANNEL_TYPE_FIELDS[channelForm.type]?.fields.map(field => (
          <div key={field.key}>
            <Label>{field.label}{!field.required && <span className="text-muted-foreground font-normal"> (选填)</span>}</Label>
            <div className="relative">
              <Input
                type={field.secret && !channelKeyVisible ? 'password' : 'text'}
                value={channelForm.config[field.key] || ''}
                onChange={e => setChannelForm({
                  ...channelForm,
                  config: { ...channelForm.config, [field.key]: e.target.value },
                })}
                placeholder={field.placeholder}
                className={`font-mono ${field.secret ? 'pr-10' : ''}`}
              />
              {field.secret && (
                <Button
                  type="button" variant="ghost" size="icon"
                  className="absolute right-1 top-1/2 -translate-y-1/2 h-8 w-8"
                  onClick={() => setChannelKeyVisible(!channelKeyVisible)}
                >
                  {channelKeyVisible ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                </Button>
              )}
            </div>
          </div>
        ))
      )}
      <div className="flex justify-end gap-2 pt-2">
        <Button variant="ghost" onClick={() => setChannelDialogOpen(false)}>取消</Button>
        {channelForm.type === 'wechat_ilink' ? (
          <Button onClick={() => void startWechatBind()} disabled={wechatBindStarting}>
            {wechatBindStarting ? '发起中…' : '绑定个人微信'}
          </Button>
        ) : (
          <Button onClick={saveChannel} disabled={!isChannelFormValid() || testing !== null}>
            {testing !== null ? '测试中…' : (editChannelId ? '保存并测试' : '创建并测试')}
          </Button>
        )}
      </div>
    </div>
  </DialogContent>
</Dialog>

    </>
  )
}
