import { useEffect, useRef } from 'react'

/**
 * PanWatch PC 快捷键体系(2026-08-12)。
 *
 * useHotkeys 在 window 上注册全局 keydown 监听,支持:
 *  - 组合键:'mod+k' / 'mod+,' / '?'('mod' = macOS Cmd,其它平台 Ctrl;兼容 meta)
 *  - 两键序列:['g','d'](默认 1.5s 内依次按下触发,超时自动取消)
 *  - 防重复:长按产生的自动重复 keydown 直接忽略(e.repeat)
 *  - 输入框保护:焦点在 input/textarea/select/contenteditable 时,
 *    无修饰键的普通键快捷键不触发(带修饰键的组合键仍生效)
 *  - 桌面端限制:desktopOnly(默认 true)时,视口 < 768px(移动端 md 以下)自动禁用
 *  - 卸载清理:组件卸载时移除监听并清空序列定时器
 */

/** 桌面端判定断点,与 Tailwind md(768px)一致 */
export const DESKTOP_MIN_WIDTH = 768

export interface HotkeyBinding {
  /** 组合键,如 'mod+k' / 'mod+,' / '?',与 sequence 二选一 */
  combo?: string
  /** 两键序列,如 ['g','d'](依次按下),与 combo 二选一 */
  sequence?: string[]
  /** 序列两次按键的最大间隔(毫秒),默认 1500 */
  sequenceTimeout?: number
  /** 触发回调,参数为原生 KeyboardEvent */
  handler: (e: KeyboardEvent) => void
  /** 仅桌面端生效(视口宽度 >= 768px),默认 true */
  desktopOnly?: boolean
  /** 命中后调用 e.preventDefault(),默认 false */
  preventDefault?: boolean
  /** 焦点在输入控件内时忽略(仅对无修饰键的普通键生效),默认 true */
  ignoreInInput?: boolean
}

const KEY_ALIASES: Record<string, string> = {
  esc: 'Escape',
  escape: 'Escape',
  enter: 'Enter',
  return: 'Enter',
  space: ' ',
  up: 'ArrowUp',
  down: 'ArrowDown',
  left: 'ArrowLeft',
  right: 'ArrowRight',
  tab: 'Tab',
  backspace: 'Backspace',
  delete: 'Delete',
  home: 'Home',
  end: 'End',
  pageup: 'PageUp',
  pagedown: 'PageDown',
}

/** 纯修饰键按下不算一次有效按键(如单独的 Ctrl / Shift) */
const MODIFIER_KEYS = new Set(['Control', 'Meta', 'Alt', 'Shift'])

interface ParsedCombo {
  mod: boolean
  ctrl: boolean
  alt: boolean
  shift: boolean
  key: string
}

interface PendingSequence {
  firstKey: string
  expiresAt: number
  timeout: number
}

function normalizeKey(key: string): string {
  const lower = key.toLowerCase()
  return KEY_ALIASES[lower] ?? lower
}

function parseCombo(combo: string): ParsedCombo {
  const parsed: ParsedCombo = { mod: false, ctrl: false, alt: false, shift: false, key: '' }
  const keyParts: string[] = []
  for (const raw of combo.split('+')) {
    const part = raw.trim().toLowerCase()
    if (part === 'mod' || part === 'cmd' || part === 'command' || part === 'meta') parsed.mod = true
    else if (part === 'ctrl' || part === 'control') parsed.ctrl = true
    else if (part === 'alt' || part === 'option') parsed.alt = true
    else if (part === 'shift') parsed.shift = true
    else if (part) keyParts.push(part)
  }
  parsed.key = normalizeKey(keyParts.join('+'))
  return parsed
}

function matchesCombo(parsed: ParsedCombo, e: KeyboardEvent): boolean {
  if (parsed.mod && !(e.metaKey || e.ctrlKey)) return false
  if (parsed.ctrl && !e.ctrlKey) return false
  if (parsed.alt && !e.altKey) return false
  if (parsed.shift && !e.shiftKey) return false
  if (parsed.key && normalizeKey(e.key) !== parsed.key) return false
  return true
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  return (
    target.isContentEditable ||
    target.tagName === 'INPUT' ||
    target.tagName === 'TEXTAREA' ||
    target.tagName === 'SELECT'
  )
}

function isDesktopViewport(): boolean {
  return window.matchMedia(`(min-width: ${DESKTOP_MIN_WIDTH}px)`).matches
}

export function useHotkeys(bindings: HotkeyBinding[]) {
  // bindings 每次渲染都是新数组字面量,用 ref 持有最新副本,
  // 保证事件监听只注册一次、handler 永远拿到最新闭包
  const bindingsRef = useRef(bindings)
  useEffect(() => {
    bindingsRef.current = bindings
  }, [bindings])

  const pendingRef = useRef<PendingSequence | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    const clearPending = () => {
      pendingRef.current = null
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current)
        timerRef.current = null
      }
    }

    const onKeyDown = (e: KeyboardEvent) => {
      // 防重复:长按自动重复的 keydown 一律忽略
      if (e.repeat) return
      // 纯修饰键(单独按 Ctrl/Shift/...)不算有效按键
      if (MODIFIER_KEYS.has(e.key)) return

      const desktop = isDesktopViewport()
      const editable = isEditableTarget(e.target)
      const key = normalizeKey(e.key)
      const list = bindingsRef.current

      // 1) 先处理挂起的两键序列:本次按键作为第二键
      const pending = pendingRef.current
      if (pending) {
        if (Date.now() > pending.expiresAt) {
          clearPending()
        } else if (key === pending.firstKey) {
          // 再次按首键(如 g g):重置时间窗口
          pending.expiresAt = Date.now() + pending.timeout
          if (timerRef.current !== null) clearTimeout(timerRef.current)
          timerRef.current = setTimeout(clearPending, pending.timeout)
          return
        } else {
          const completing = list.find(
            b =>
              b.sequence &&
              b.sequence.length >= 2 &&
              (b.desktopOnly === false || desktop) &&
              normalizeKey(b.sequence[0]) === pending.firstKey &&
              normalizeKey(b.sequence[1]) === key,
          )
          if (completing) {
            clearPending()
            if (completing.preventDefault) e.preventDefault()
            completing.handler(e)
            return
          }
          // 非目标第二键:取消挂起,继续按普通按键处理(该键可能本身是另一个序列首键)
          clearPending()
        }
      }

      // 2) 组合键匹配(mod+k / mod+, / ?)
      for (const b of list) {
        if (!b.combo) continue
        if (b.desktopOnly !== false && !desktop) continue
        const parsed = parseCombo(b.combo)
        const hasModifier = parsed.mod || parsed.ctrl || parsed.alt || parsed.shift
        if (editable && !hasModifier && b.ignoreInInput !== false) continue
        if (matchesCombo(parsed, e)) {
          if (b.preventDefault) e.preventDefault()
          b.handler(e)
          return
        }
      }

      // 3) 序列首键匹配(如 g → 等待 d / p)
      for (const b of list) {
        if (!b.sequence || b.sequence.length < 2) continue
        if (b.desktopOnly !== false && !desktop) continue
        if (editable && b.ignoreInInput !== false) continue
        if (normalizeKey(b.sequence[0]) !== key) continue
        const timeout = b.sequenceTimeout ?? 1500
        pendingRef.current = { firstKey: key, expiresAt: Date.now() + timeout, timeout }
        if (timerRef.current !== null) clearTimeout(timerRef.current)
        timerRef.current = setTimeout(clearPending, timeout)
        return
      }
    }

    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      clearPending()
    }
  }, [])

  return bindings
}
