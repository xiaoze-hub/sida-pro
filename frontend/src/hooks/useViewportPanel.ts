import { useEffect, useRef } from 'react'

/** 对话工作区使用实际顶栏高度计算可见区域，状态换行和手机底部导航不能挡住输入。 */
export function useViewportPanel(enabled = true) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const panel = ref.current
    if (!enabled) { panel?.style.removeProperty('height'); return }
    if (!panel || typeof ResizeObserver === 'undefined') return
    const update = () => {
      const rect = panel.getBoundingClientRect()
      if (!rect.width) return // keep-alive 隐藏页签不覆盖上次尺寸
      const navigation = document.querySelector('[data-mobile-navigation]')
      const navigationHeight = navigation?.getBoundingClientRect().height ?? 0
      const viewportHeight = window.visualViewport?.height ?? window.innerHeight
      const height = Math.max(240, viewportHeight - Math.max(0, rect.top) - navigationHeight - 16)
      if (panel.style.height !== `${height}px`) panel.style.height = `${height}px`
    }
    const observer = new ResizeObserver(update)
    observer.observe(panel)
    for (const selector of ['[data-workspace-status]', '[data-testid="workspace-tabs"]']) {
      const element = document.querySelector(selector)
      if (element) observer.observe(element)
    }
    update()
    window.addEventListener('resize', update)
    window.visualViewport?.addEventListener('resize', update)
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', update)
      window.visualViewport?.removeEventListener('resize', update)
    }
  }, [enabled])
  return ref
}
