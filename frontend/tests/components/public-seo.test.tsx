// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Link, Route, Routes } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { usePublicSeo } from '@/hooks/use-public-seo'

function SeoPage({ path }: { path: '/' | '/developers' | '/tiers' }) {
  usePublicSeo(path)
  return <><Link to="/developers?section=sec-auth">文档</Link><Link to="/settings">设置</Link></>
}
afterEach(() => { cleanup(); document.head.querySelectorAll('meta, link[rel="canonical"], #public-site-schema').forEach(node => node.remove()); vi.unstubAllEnvs() })

it('客户端换页更新唯一 canonical/标题/分享信息，并沿用服务端自部署域名', () => {
  vi.stubEnv('DEV', false)
  const origin = document.createElement('meta'); origin.name = 'sida-site-url'; origin.content = 'https://selfhost.example'; document.head.append(origin)
  render(<MemoryRouter><Routes><Route path="/" element={<SeoPage path="/" />} /><Route path="/developers" element={<SeoPage path="/developers" />} /><Route path="/settings" element={<p>设置</p>} /></Routes></MemoryRouter>)
  expect(document.querySelector('link[rel="canonical"]')?.getAttribute('href')).toBe('https://selfhost.example/')
  fireEvent.click(screen.getByText('文档'))
  expect(document.title).toContain('开发者文档')
  expect(document.querySelectorAll('link[rel="canonical"]').length).toBe(1)
  expect(document.querySelector('link[rel="canonical"]')?.getAttribute('href')).toBe('https://selfhost.example/developers')
  expect(document.querySelectorAll('meta[name="description"]').length).toBe(1)
  expect(document.querySelector('meta[property="og:url"]')?.getAttribute('content')).toBe('https://selfhost.example/developers')
  const schema = JSON.parse(document.querySelector('#public-site-schema')!.textContent!)
  expect(schema['@graph'].some((item: { '@type': string }) => item['@type'] === 'BreadcrumbList')).toBe(true)
  fireEvent.click(screen.getByText('设置'))
  expect(document.querySelector('meta[name="robots"]')?.getAttribute('content')).toBe('noindex,follow')
  expect(document.querySelector('link[rel="canonical"]')).toBeNull()
  expect(document.querySelector('#public-site-schema')).toBeNull()
})

it.each([true, false])('私有实例与无服务端标记的本地预览不索引（私有标记 %s）', explicitPrivate => {
  vi.stubEnv('DEV', false)
  if (explicitPrivate) { const flag = document.createElement('meta'); flag.name = 'sida-site-indexable'; flag.content = 'false'; document.head.append(flag) }
  render(<MemoryRouter><SeoPage path="/tiers" /></MemoryRouter>)
  expect(document.querySelector('meta[name="robots"]')?.getAttribute('content')).toBe('noindex,follow')
})
