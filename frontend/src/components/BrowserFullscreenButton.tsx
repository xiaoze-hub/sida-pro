import { useEffect, useRef, useState } from 'react'
import { Maximize, Minimize } from 'lucide-react'
import { Button } from '@sida/base-ui/components/ui/button'
import { useToast } from '@sida/base-ui/components/ui/toast'
import { useI18n } from '@/hooks/useI18n'

/** 浏览器原生全屏，全站入口同步；页面/助手内部的展开状态与此无关。 */
export default function BrowserFullscreenButton({ className = '' }: { className?: string }) {
  const { t } = useI18n()
  const { toast } = useToast()
  const [active, setActive] = useState(() => Boolean(document.fullscreenElement))
  const [pending, setPending] = useState(false)
  const switching = useRef(false)
  const supported = Boolean(document.fullscreenEnabled && typeof document.documentElement.requestFullscreen === 'function' && typeof document.exitFullscreen === 'function')
  useEffect(() => {
    const sync = () => setActive(Boolean(document.fullscreenElement))
    document.addEventListener('fullscreenchange', sync)
    sync()
    return () => document.removeEventListener('fullscreenchange', sync)
  }, [])
  if (!supported) return null
  const toggle = async () => {
    if (switching.current) return
    switching.current = true
    setPending(true)
    try {
      if (document.fullscreenElement) await document.exitFullscreen()
      else await document.documentElement.requestFullscreen()
      setActive(Boolean(document.fullscreenElement))
    } catch {
      toast(t('common.fullscreenFailed'), 'error')
    } finally {
      switching.current = false
      setPending(false)
    }
  }
  const label = t(active ? 'common.exitBrowserFullscreen' : 'common.browserFullscreen')
  return <Button type="button" variant="ghost" size="icon" className={`h-11 w-11 shrink-0 md:h-10 md:w-10 ${className}`} aria-label={label} aria-pressed={active} title={active ? `${label} (Esc)` : label} disabled={pending} onClick={() => void toggle()} data-testid="browser-fullscreen">
    {active ? <Minimize className="h-4 w-4" /> : <Maximize className="h-4 w-4" />}
  </Button>
}
