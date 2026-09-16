/**
 * SafeMarkdown (2026-09-15 安全审计 P2)
 *
 * 统一 ReactMarkdown 的 sanitize 策略, 替换散落各页的裸 ReactMarkdown:
 * - remarkGfm 默认开启(表格/删除线/任务列表等)
 * - urlTransform: 仅放行 http/https/相对路径, 拒绝 javascript:/data:/vbscript: 等
 * - 外链自动 target="_blank" rel="noopener noreferrer"
 * - 调用方可通过 components 覆盖任意渲染器(a 除外的默认样式也可覆盖)
 */
import { type ComponentPropsWithoutRef } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'

/**
 * 只允许 http/https/相对路径(含 #hash / ?query / /path)。
 * 协议相对 URL(//evil.com)与 javascript:/data: 等一律返回空串。
 */
export function safeUrlTransform(url: string): string {
  if (!url) return ''
  const trimmed = url.trim()
  if (!trimmed) return ''
  // 协议相对 //host 会继承当前页协议, 无法在前端可靠判定目标, 直接拒绝
  if (trimmed.startsWith('//')) return ''

  const colon = trimmed.indexOf(':')
  const slash = trimmed.indexOf('/')
  const question = trimmed.indexOf('?')
  const hash = trimmed.indexOf('#')
  // 冒号出现在 path/query/hash 之后 → 相对路径(如 /a:b, ./x?y=z)
  if (
    colon === -1 ||
    (slash !== -1 && colon > slash) ||
    (question !== -1 && colon > question) ||
    (hash !== -1 && colon > hash)
  ) {
    return trimmed
  }

  const protocol = trimmed.slice(0, colon).toLowerCase()
  if (protocol === 'http' || protocol === 'https') return trimmed
  return ''
}

function isExternalUrl(url: string): boolean {
  if (!url) return false
  if (url.startsWith('//')) return true
  try {
    const resolved = new URL(url, typeof window !== 'undefined' ? window.location.href : 'http://localhost')
    if (typeof window === 'undefined') return true
    return resolved.origin !== window.location.origin
  } catch {
    return false
  }
}

const defaultAnchor: Components['a'] = ({ href, children, ...props }) => {
  const safeHref = href || ''
  const external = isExternalUrl(safeHref)
  return (
    <a
      href={safeHref || undefined}
      {...(external ? { target: '_blank', rel: 'noopener noreferrer' } : {})}
      className="break-all text-primary underline underline-offset-2"
      {...props}
    >
      {children}
    </a>
  )
}

export interface SafeMarkdownProps
  extends Omit<ComponentPropsWithoutRef<typeof ReactMarkdown>, 'children' | 'components'> {
  children: string | null | undefined
  /** 覆盖/追加渲染器; 覆盖 a 时请自行保证协议安全(urlTransform 已剥离危险 href) */
  components?: Components
}

export default function SafeMarkdown({ children, components, remarkPlugins, ...rest }: SafeMarkdownProps) {
  const merged: Components = {
    a: defaultAnchor,
    ...components,
  }
  const plugins = [remarkGfm, ...(remarkPlugins || [])]
  return (
    <ReactMarkdown remarkPlugins={plugins} urlTransform={safeUrlTransform} components={merged} {...rest}>
      {children ?? ''}
    </ReactMarkdown>
  )
}
