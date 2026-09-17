/**
 * SIDA 全站视觉巡检脚本
 * 运行: cd frontend && node ../scripts/visual_audit.mjs
 */
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'fs'
import { join } from 'path'

const BASE = 'http://100.91.30.35:8000'
const SHOTS = join(process.cwd(), '..', 'shots')
mkdirSync(SHOTS, { recursive: true })

const PAGES = [
  { path: '/', name: 'landing', needLogin: false },
  { path: '/login', name: 'login', needLogin: false },
  { path: '/login?mode=register', name: 'register', needLogin: false },
  { path: '/developers', name: 'developers', needLogin: false },
  { path: '/', name: 'dashboard', needLogin: true },
  { path: '/stocks/000001?type=index', name: 'stocks', needLogin: true },
  { path: '/heatmap', name: 'heatmap', needLogin: true },
  { path: '/opportunities', name: 'opportunities', needLogin: true },
  { path: '/dark-fund-top', name: 'dark-fund-top', needLogin: true },
  { path: '/reports', name: 'reports', needLogin: true },
  { path: '/portfolio', name: 'portfolio', needLogin: true },
  { path: '/shadow', name: 'shadow', needLogin: true },
  { path: '/notifications', name: 'notifications', needLogin: true },
  { path: '/settings', name: 'settings', needLogin: true },
  { path: '/profile', name: 'profile', needLogin: true },
  { path: '/api-keys', name: 'api-keys', needLogin: true },
]

const results = []

async function main() {
  const browser = await chromium.launch({ headless: true })
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: 'zh-CN',
  })
  const page = await ctx.newPage()

  const consoleErrors = []
  const failedRequests = []
  page.on('console', msg => {
    if (msg.type() === 'error') consoleErrors.push(msg.text().slice(0, 200))
  })
  page.on('requestfailed', req => {
    failedRequests.push(`${req.method()} ${req.url()} - ${req.failure()?.errorText}`)
  })
  page.on('response', resp => {
    if (resp.status() >= 400) failedRequests.push(`${resp.status()} ${resp.url()}`)
  })

  // 访问落地页
  console.log('访问落地页...')
  await page.goto(BASE + '/', { waitUntil: 'networkidle', timeout: 30000 })
  await page.waitForTimeout(2000)

  const isLoginPage = page.url().includes('/login')
  console.log('当前URL:', page.url(), 'isLogin:', isLoginPage)

  for (const p of PAGES) {
    console.log(`\n访问 ${p.name} (${p.path})...`)
    consoleErrors.length = 0
    failedRequests.length = 0

    try {
      await page.goto(BASE + p.path, { waitUntil: 'networkidle', timeout: 30000 })
      await page.waitForTimeout(3000)

      const url = page.url()
      const title = await page.title()
      const innerText = await page.evaluate(() => document.body.innerText.slice(0, 200))
      const screenshot = join(SHOTS, `${p.name}.png`)
      await page.screenshot({ path: screenshot, fullPage: false })

      const isLoginRedirect = url.includes('/login')
      const isEmpty = innerText.trim().length < 10

      results.push({
        page: p.name,
        path: p.path,
        url,
        title,
        innerText: innerText.replace(/\n/g, ' | ').slice(0, 200),
        isLoginRedirect,
        isEmpty,
        consoleErrors: [...consoleErrors],
        failedRequests: [...failedRequests],
        screenshot: `${p.name}.png`,
      })

      console.log(`  URL: ${url}`)
      console.log(`  标题: ${title}`)
      console.log(`  内容: ${innerText.slice(0, 80).replace(/\n/g, ' ')}...`)
      console.log(`  登录重定向: ${isLoginRedirect}, 空白: ${isEmpty}`)
      console.log(`  控制台错误: ${consoleErrors.length}, 请求失败: ${failedRequests.length}`)
      if (consoleErrors.length > 0) console.log(`    错误: ${consoleErrors[0].slice(0, 100)}`)
      if (failedRequests.length > 0) console.log(`    失败: ${failedRequests[0].slice(0, 100)}`)
    } catch (e) {
      results.push({ page: p.name, path: p.path, error: e.message })
      console.log(`  错误: ${e.message}`)
    }
  }

  await browser.close()

  const report = { timestamp: new Date().toISOString(), baseUrl: BASE, pages: results }
  writeFileSync(join(SHOTS, 'audit_report.json'), JSON.stringify(report, null, 2))
  
  // 汇总
  console.log('\n\n======== 巡检汇总 ========')
  for (const r of results) {
    const issues = []
    if (r.error) issues.push(`ERROR: ${r.error}`)
    if (r.isLoginRedirect) issues.push('登录重定向')
    if (r.isEmpty) issues.push('空白页')
    if (r.consoleErrors?.length) issues.push(`控制台错误×${r.consoleErrors.length}`)
    if (r.failedRequests?.length) issues.push(`请求失败×${r.failedRequests.length}`)
    const status = issues.length === 0 ? '✅' : '❌'
    console.log(`${status} ${r.page}: ${issues.join(', ') || '正常'}`)
  }
}

main().catch(e => { console.error(e); process.exit(1) })
