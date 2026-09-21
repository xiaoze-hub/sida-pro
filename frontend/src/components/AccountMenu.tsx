import { useState, useEffect, useRef } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { Moon, Sun, Monitor, Check, LogOut, User, Stethoscope, KeyRound, UserCog, Languages, type LucideIcon } from 'lucide-react'
import { isAuthenticated, logout } from '@panwatch/api'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@panwatch/base-ui/components/ui/dialog'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import type { ThemeMode } from '@/hooks/use-theme'
import { useAvatar } from '@/hooks/use-avatar'
import { useI18n, type Locale } from '@/hooks/useI18n'
import { submitChangePassword } from '@/lib/change-password'

export interface AccountNavItem {
  to: string
  icon: LucideIcon
  label: string
}

const THEME_OPTIONS: { value: ThemeMode; icon: LucideIcon; labelKey: string }[] = [
  { value: 'light', icon: Sun, labelKey: 'theme.light' },
  { value: 'dark', icon: Moon, labelKey: 'theme.dark' },
  { value: 'system', icon: Monitor, labelKey: 'theme.system' },
]

const LOCALE_OPTIONS: { value: Locale; label: string }[] = [
  { value: 'zh-CN', label: '简体中文' },
  { value: 'en-US', label: 'English' },
]

interface AccountMenuProps {
  /** 原“更多”里折叠的导航项(Agent / 历史 / 数据源 / 设置)。 */
  navItems: AccountNavItem[]
  mode: ThemeMode
  onSetMode: (m: ThemeMode) => void
  /** 打开「系统自检」弹窗(状态由上层 App 托管,避免桌面/移动两个实例重复)。 */
  onOpenSelfCheck: () => void
  /** 头像尺寸:桌面 md,移动端 sm。 */
  size?: 'sm' | 'md'
  /**
   * 面板展开方向。桌面实例在**全高侧栏的最底部**,向下展开会整体落到视口外
   * (主题/亮暗/退出等全部不可见) → 必须向上展开;移动端顶栏保持向下。
   */
  placement?: 'down' | 'up'
}

/**
 * 右上角头像区域 + 下拉菜单(参考 beecount-cloud):
 * 把原“更多”导航、主题色(亮/暗/跟随系统)、退出登录收进头像下拉
 * (查看日志 / GitHub 仍在外侧)。
 */
export default function AccountMenu({
  navItems,
  mode,
  onSetMode,
  onOpenSelfCheck,
  size = 'md',
  placement = 'down',
}: AccountMenuProps) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement | null>(null)
  const location = useLocation()
  const avatar = useAvatar()
  const { toast } = useToast()
  const { t, locale, setLocale } = useI18n()
  // 修改密码弹窗
  const [changePwdOpen, setChangePwdOpen] = useState(false)
  const [oldPwd, setOldPwd] = useState('')
  const [newPwd, setNewPwd] = useState('')
  const [confirmPwd, setConfirmPwd] = useState('')
  const [pwdError, setPwdError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  // 仅在支持 hover 的设备(PC)启用悬停展开;触屏维持点击
  const [canHover] = useState(
    () => typeof window !== 'undefined' && window.matchMedia('(hover: hover)').matches,
  )

  const openChangePwd = () => {
    setOpen(false)
    setOldPwd('')
    setNewPwd('')
    setConfirmPwd('')
    setPwdError(null)
    setChangePwdOpen(true)
  }

  const handleChangePassword = async () => {
    // 2026-08-17: 改用公共 helper (关闭 A P1-8 双份实现)
    await submitChangePassword({
      oldPwd, newPwd, confirmPwd,
      onError: setPwdError,
      onSuccess: () => {
        toast(t('accountMenu.passwordUpdated'), 'success')
        setChangePwdOpen(false)
        setOldPwd('')
        setNewPwd('')
        setConfirmPwd('')
      },
      onLoadingChange: setSubmitting,
    })
  }

  // 点击外部关闭
  useEffect(() => {
    const onPointerDown = (e: PointerEvent) => {
      if (open && ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('pointerdown', onPointerDown)
    return () => document.removeEventListener('pointerdown', onPointerDown)
  }, [open])

  // 路由变化时关闭
  useEffect(() => {
    setOpen(false)
  }, [location.pathname])

  const avatarSize = size === 'sm' ? 'w-6 h-6' : 'w-7 h-7'
  const iconSize = size === 'sm' ? 'w-3.5 h-3.5' : 'w-4 h-4'

  return (
    <div
      className="relative"
      ref={ref}
      onMouseEnter={canHover ? () => setOpen(true) : undefined}
      onMouseLeave={canHover ? () => setOpen(false) : undefined}
    >
      <button
        onClick={() => setOpen(v => !v)}
        className={`${avatarSize} rounded-full overflow-hidden bg-gradient-to-br from-primary to-primary/70 flex items-center justify-center shadow-sm ring-1 transition-shadow ${
          open ? 'ring-primary/50' : 'ring-border/40 hover:ring-primary/40'
        }`}
        title="账户与设置"
        aria-label="账户与设置"
      >
        {avatar ? (
          <img src={avatar} alt="头像" className="w-full h-full object-cover" />
        ) : (
          <User className={`${iconSize} text-white`} />
        )}
      </button>

      {open && (
        // top-full/bottom-full + pt-2/pb-2:用透明内边距桥接头像与菜单,hover 移入不断开
        <div
          className={`absolute right-0 z-50 ${
            placement === 'up' ? 'bottom-full pb-2' : 'top-full pt-2'
          }`}
        >
          <div className="w-48 rounded-xl border border-border/60 bg-card/95 backdrop-blur p-1.5 shadow-float">
          {/* 原“更多”导航 */}
          {navItems.map(({ to, icon: Icon, label }) => {
            const isActive = location.pathname.startsWith(to)
            return (
              <NavLink
                key={to}
                to={to}
                onClick={() => setOpen(false)}
                className={`flex items-center gap-2.5 px-2.5 py-2 rounded-lg text-[12px] transition-colors ${
                  isActive
                    ? 'bg-primary/10 text-primary'
                    : 'text-muted-foreground hover:text-foreground hover:bg-accent/60'
                }`}
              >
                <Icon className="w-3.5 h-3.5" />
                {label}
              </NavLink>
            )
          })}

          <div className="my-1 h-px bg-border/50" />

          {/* 个人中心(2026-08-15 SIDA P1) */}
          <NavLink
            to="/profile"
            onClick={() => setOpen(false)}
            className={`flex items-center gap-2.5 px-2.5 py-2 min-h-[44px] rounded-lg text-[12px] transition-colors ${
              location.pathname.startsWith('/profile')
                ? 'bg-primary/10 text-primary'
                : 'text-muted-foreground hover:text-foreground hover:bg-accent/60'
            }`}
          >
            <UserCog className="w-3.5 h-3.5" />
            {t('accountMenu.profile')}
          </NavLink>

          <div className="my-1 h-px bg-border/50" />

          {/* 主题色:亮 / 暗 / 跟随系统 */}
          <div className="px-2.5 pt-0.5 pb-1 text-[11px] text-muted-foreground">{t('accountMenu.theme')}</div>
          {THEME_OPTIONS.map(({ value, icon: Icon, labelKey }) => {
            const active = mode === value
            return (
              <button
                key={value}
                onClick={() => onSetMode(value)}
                className={`flex w-full items-center gap-2.5 px-2.5 py-2 min-h-[44px] rounded-lg text-[12px] transition-colors ${
                  active
                    ? 'text-foreground bg-accent/40'
                    : 'text-muted-foreground hover:text-foreground hover:bg-accent/60'
                }`}
              >
                <Icon className="w-3.5 h-3.5" />
                {t(labelKey)}
                {active && <Check className="w-3.5 h-3.5 ml-auto text-primary" />}
              </button>
            )
          })}

          <div className="my-1 h-px bg-border/50" />
          {/* 语言切换 (P2 i18n) */}
          <div className="px-2.5 pt-0.5 pb-1 text-[11px] text-muted-foreground flex items-center gap-1.5">
            <Languages className="w-3 h-3" />
            {t('accountMenu.language')}
          </div>
          {LOCALE_OPTIONS.map(({ value, label }) => {
            const active = locale === value
            return (
              <button
                key={value}
                onClick={() => setLocale(value)}
                className={`flex w-full items-center gap-2.5 px-2.5 py-2 min-h-[44px] rounded-lg text-[12px] transition-colors ${
                  active
                    ? 'text-foreground bg-accent/40'
                    : 'text-muted-foreground hover:text-foreground hover:bg-accent/60'
                }`}
              >
                {label}
                {active && <Check className="w-3.5 h-3.5 ml-auto text-primary" />}
              </button>
            )
          })}

          <div className="my-1 h-px bg-border/50" />
          {/* 系统自检:打开弹窗(逐项检查数据源/AI/通知连通性) */}
          <button
            onClick={() => {
              setOpen(false)
              onOpenSelfCheck()
            }}
            className="flex w-full items-center gap-2.5 px-2.5 py-2 min-h-[44px] rounded-lg text-[12px] text-muted-foreground hover:text-foreground hover:bg-accent/60 transition-colors"
          >
            <Stethoscope className="w-3.5 h-3.5" />
            {t('accountMenu.selfCheck')}
          </button>

          {isAuthenticated() && (
            <>
              <div className="my-1 h-px bg-border/50" />
              <button
                onClick={openChangePwd}
                className="flex w-full items-center gap-2.5 px-2.5 py-2 min-h-[44px] rounded-lg text-[12px] text-muted-foreground hover:text-foreground hover:bg-accent/60 transition-colors"
              >
                <KeyRound className="w-3.5 h-3.5" />
                {t('profile.changePassword')}
              </button>
              <div className="my-1 h-px bg-border/50" />
              <button
                onClick={logout}
                className="flex w-full items-center gap-2.5 px-2.5 py-2 min-h-[44px] rounded-lg text-[12px] text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors"
              >
                <LogOut className="w-3.5 h-3.5" />
                {t('accountMenu.logout')}
              </button>
            </>
          )}
          </div>
        </div>
      )}

      {/* 修改密码弹窗 */}
      <Dialog
        open={changePwdOpen}
        onOpenChange={v => {
          setChangePwdOpen(v)
          if (!v) setPwdError(null)
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('accountMenu.changePwdTitle')}</DialogTitle>
            <DialogDescription>{t('accountMenu.changePwdDesc')}</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 mt-2">
            <div>
              <Label>{t('profile.oldPassword')}</Label>
              <Input
                type="password"
                value={oldPwd}
                onChange={e => {
                  setOldPwd(e.target.value)
                  setPwdError(null)
                }}
                placeholder={t('accountMenu.oldPwdPlaceholder')}
                autoComplete="current-password"
              />
            </div>
            <div>
              <Label>{t('profile.newPassword')}</Label>
              <Input
                type="password"
                value={newPwd}
                onChange={e => {
                  setNewPwd(e.target.value)
                  setPwdError(null)
                }}
                placeholder={t('accountMenu.newPwdPlaceholder')}
                autoComplete="new-password"
              />
            </div>
            <div>
              <Label>{t('profile.confirmPassword')}</Label>
              <Input
                type="password"
                value={confirmPwd}
                onChange={e => {
                  setConfirmPwd(e.target.value)
                  setPwdError(null)
                }}
                placeholder={t('accountMenu.confirmPwdPlaceholder')}
                autoComplete="new-password"
              />
            </div>
            {pwdError && <div className="text-[12px] text-destructive">{pwdError}</div>}
            <div className="flex justify-end gap-2 pt-2">
              <Button variant="ghost" onClick={() => setChangePwdOpen(false)} disabled={submitting}>
                {t('common.cancel')}
              </Button>
              <Button onClick={handleChangePassword} disabled={submitting}>
                {submitting ? t('accountMenu.submitting') : t('common.confirm')}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
