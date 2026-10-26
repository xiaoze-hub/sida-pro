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

import { normalizeMarkdownTables } from '@/lib/markdown-normalize'

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

/** 表格/标题/列表的排版（2026-10-25）：react-markdown 默认输出裸标签，
 *  Tailwind 下既无边框也无对齐，数据表看起来像一堆错位的字。
 *  这里统一成「数据表」：表头浅底、数值右对齐由内容自己控制（tabular-nums）、
 *  横向可滚（窄屏），并保持 11–13px 的信息密度。 */
const mdComponents: Components = {
  table: ({ children, ...props }) => (
    <div className="-mx-1 my-2 overflow-x-auto rounded-md border border-border/60">
      <table className="w-full border-collapse text-[12px]" {...props}>
        {children}
      </table>
    </div>
  ),
  thead: ({ children, ...props }) => (
    <thead className="bg-s1 text-muted-foreground" {...props}>
      {children}
    </thead>
  ),
  th: ({ children, ...props }) => (
    <th
      className="whitespace-nowrap border-b border-border/60 px-2 py-1 text-left font-medium"
      {...props}
    >
      {children}
    </th>
  ),
  td: ({ children, ...props }) => (
    <td className="border-b border-border/40 px-2 py-1 align-top font-num tabular-nums" {...props}>
      {children}
    </td>
  ),
  h2: ({ children, ...props }) => (
    <h2 className="mt-3 mb-1 text-[16px] font-semibold text-foreground" {...props}>
      {children}
    </h2>
  ),
  h3: ({ children, ...props }) => (
    <h3 className="mt-2 mb-1 text-[13px] font-semibold text-foreground" {...props}>
      {children}
    </h3>
  ),
  ul: ({ children, ...props }) => (
    <ul className="my-1 list-disc space-y-0.5 pl-5 text-[13px]" {...props}>
      {children}
    </ul>
  ),
  ol: ({ children, ...props }) => (
    <ol className="my-1 list-decimal space-y-0.5 pl-5 text-[13px]" {...props}>
      {children}
    </ol>
  ),
  blockquote: ({ children, ...props }) => (
    <blockquote className="my-2 border-l-2 border-border-strong pl-2.5 text-muted-foreground" {...props}>
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-3 border-border/60" />,
  code: ({ children, ...props }) => (
    <code className="rounded bg-s1 px-1 py-0.5 font-mono text-[12px]" {...props}>
      {children}
    </code>
  ),
}

export default function SafeMarkdown({ children, components, remarkPlugins, ...rest }: SafeMarkdownProps) {
  const merged: Components = {
    ...mdComponents,
    a: defaultAnchor,
    ...components,
  }
  const plugins = [remarkGfm, ...(remarkPlugins || [])]
  return (
    <ReactMarkdown remarkPlugins={plugins} urlTransform={safeUrlTransform} components={merged} {...rest}>
      {normalizeMarkdownTables(children ?? '')}
    </ReactMarkdown>
  )
}
