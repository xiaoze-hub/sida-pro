import { useEffect, useRef } from 'react'

/**
 * 对话工作区使用**实际可见区域**计算高度，状态换行和手机底部导航不能挡住输入。
 *
 * 2026-10-27 修「手机输入法弹起遮住输入框」：
 *  1. 原来的算式只用 `visualViewport.height`，漏了 `offsetTop`。iOS 上键盘弹起时
 *     可视窗口既缩高也会被浏览器整体上推，漏掉 offsetTop 会把面板高度算大，
 *     底部那段（正好是输入框）就被键盘盖住。
 *  2. 只监听 `resize`。部分浏览器（微信/iOS Safari）键盘弹起只稳定发 `scroll`，
 *     不发 resize —— 面板高度就停在键盘弹起前的旧值。现在 resize/scroll 都听。
 *  3. 高度下限从 240 放宽到 160：手机横屏或小屏键盘弹起后可用区域可能不足 240，
 *     硬撑 240 会把输入框顶到键盘下面。可见区域优先，宁可消息区矮一点。
 *  4. 用 rAF 合并同一帧内的多次事件，避免键盘动画期间每个像素都触发一次布局。
 */
export function useViewportPanel(enabled = true) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const panel = ref.current
    if (!enabled) {
      panel?.style.removeProperty('height')
      return
    }
    if (!panel || typeof ResizeObserver === 'undefined') return

    // jsdom / 极老浏览器没有 rAF 时退化成同步执行，不要因为调度器缺失就整段不生效
    const raf = (cb: () => void) =>
      typeof window.requestAnimationFrame === 'function'
        ? window.requestAnimationFrame(cb)
        : window.setTimeout(cb, 0)
    const cancelRaf = (id: number) =>
      typeof window.cancelAnimationFrame === 'function'
        ? window.cancelAnimationFrame(id)
        : window.clearTimeout(id)

    let frame = 0
    const apply = () => {
      frame = 0
      const rect = panel.getBoundingClientRect()
      if (!rect.width) return // keep-alive 隐藏页签不覆盖上次尺寸
      const navigation = document.querySelector('[data-mobile-navigation]')
      const navigationHeight = navigation?.getBoundingClientRect().height ?? 0
      const vv = window.visualViewport
      // 可见区域底边在**布局坐标**里的位置: offsetTop + height(键盘弹起时会被上推)
      const visualHeight = vv ? vv.height : window.innerHeight
      const visualOffsetTop = vv ? vv.offsetTop : 0
      const available = visualHeight + visualOffsetTop - Math.max(0, rect.top) - navigationHeight - 16
      const height = Math.max(160, available)
      if (panel.style.height !== `${height}px`) panel.style.height = `${height}px`
    }
    const update = () => {
      if (frame) return
      frame = raf(apply)
    }

    const observer = new ResizeObserver(update)
    observer.observe(panel)
    for (const selector of ['[data-workspace-status]', '[data-testid="workspace-tabs"]']) {
      const element = document.querySelector(selector)
      if (element) observer.observe(element)
    }
    update()
    window.addEventListener('resize', update)
    window.addEventListener('orientationchange', update)
    window.visualViewport?.addEventListener('resize', update)
    // 键盘弹起时常见只有 scroll 事件(页面被上推), 只听 resize 会漏
    window.visualViewport?.addEventListener('scroll', update)
    return () => {
      if (frame) cancelRaf(frame)
      observer.disconnect()
      window.removeEventListener('resize', update)
      window.removeEventListener('orientationchange', update)
      window.visualViewport?.removeEventListener('resize', update)
      window.visualViewport?.removeEventListener('scroll', update)
    }
  }, [enabled])
  return ref
}
