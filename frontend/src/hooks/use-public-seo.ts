import { useEffect } from 'react'
import site from '../../public/site-seo.json'

export function usePublicSeo(path: keyof typeof site.pages) {
  useEffect(() => {
    const page = site.pages[path]
    const origin = document.querySelector<HTMLMetaElement>('meta[name="sida-site-url"]')?.content || site.siteUrl
    const canonical = new URL(path, origin).href
    const serverIndexable = document.querySelector<HTMLMetaElement>('meta[name="sida-site-indexable"]')?.content
    const local = ['localhost', '127.0.0.1', '::1', '[::1]'].includes(window.location.hostname)
    const indexable = !import.meta.env.DEV && (serverIndexable === 'true' || (serverIndexable !== 'false' && !local))
    document.title = page.title
    const setMeta = (attribute: 'name' | 'property', key: string, content: string) => {
      let meta = document.querySelector<HTMLMetaElement>(`meta[${attribute}="${key}"]`)
      if (!meta) { meta = document.createElement('meta'); meta.setAttribute(attribute, key); document.head.append(meta) }
      meta.content = content
      meta.dataset.publicSeo = 'true'
    }
    setMeta('name', 'description', page.description)
    setMeta('name', 'robots', indexable ? 'index,follow,max-image-preview:large' : 'noindex,follow')
    for (const [key, value] of Object.entries({ title: page.title, description: page.description, url: canonical, type: 'website', site_name: site.siteName, locale: 'zh_CN', image: new URL(site.image, origin).href, 'image:alt': 'SIDA Pro 个股研究工作台真实界面', 'image:width': '1440', 'image:height': '1100' })) setMeta('property', `og:${key}`, value)
    for (const [key, value] of Object.entries({ card: 'summary_large_image', title: page.title, description: page.description, image: new URL(site.image, origin).href })) setMeta('name', `twitter:${key}`, value)
    let link = document.querySelector<HTMLLinkElement>('link[rel="canonical"]')
    if (!link) { link = document.createElement('link'); link.rel = 'canonical'; document.head.append(link) }
    link.href = canonical
    link.dataset.publicSeo = 'true'
    // 首次直达时使用服务端 JSON-LD；客户端换页时同步 WebPage 和面包屑。
    let schema = document.querySelector<HTMLScriptElement>('#public-site-schema')
    if (!schema) { schema = document.createElement('script'); schema.id = 'public-site-schema'; schema.type = 'application/ld+json'; document.head.append(schema) }
    const graph: object[] = [
      { '@type': 'WebSite', '@id': `${origin}/#website`, name: site.siteName, url: `${origin}/`, sameAs: site.repository },
      { '@type': 'WebPage', '@id': `${canonical}#webpage`, name: page.title, description: page.description, url: canonical, inLanguage: 'zh-CN', isPartOf: { '@id': `${origin}/#website` } },
    ]
    if (path === '/') graph.push({ '@type': 'WebApplication', name: site.siteName, url: canonical, applicationCategory: 'BusinessApplication', operatingSystem: 'Web', description: page.description, screenshot: new URL(site.image, origin).href })
    else graph.push({ '@type': 'BreadcrumbList', itemListElement: [{ '@type': 'ListItem', position: 1, name: '产品概览', item: `${origin}/` }, { '@type': 'ListItem', position: 2, name: page.heading, item: canonical }] })
    schema.textContent = JSON.stringify({ '@context': 'https://schema.org', '@graph': graph })
    return () => {
      document.title = site.siteName
      document.querySelectorAll('[data-public-seo="true"]').forEach(node => node.remove())
      document.querySelector('#public-site-schema')?.remove()
      const robots = document.createElement('meta'); robots.name = 'robots'; robots.content = 'noindex,follow'; document.head.append(robots)
    }
  }, [path])
}
