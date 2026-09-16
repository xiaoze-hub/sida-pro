/* 复验 3 个已修页面 */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const BASE = 'http://127.0.0.1:8000';
const TOKEN = fs.readFileSync('C:/Users/tianxiang/sida-work/data/admin_token.txt', 'utf8').trim();
const OUT = 'C:/Users/tianxiang/sida-work/docs/screenshots/ui-sweep-20260918';
const PAGES = [
  { name: 'fix-16-profile', url: '/profile' },
  { name: 'fix-07-portfolio', url: '/portfolio' },
  { name: 'fix-09-workbench-index', url: '/stocks/000001?type=index' },
];
(async () => {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN', timezoneId: 'Asia/Shanghai' });
  const page = await ctx.newPage();
  await page.goto(BASE + '/login', { waitUntil: 'domcontentloaded' });
  await page.evaluate((tok) => {
    localStorage.setItem('token', tok);
    localStorage.setItem('token_expires', new Date(Date.now() + 8 * 3600 * 1000).toISOString());
  }, TOKEN);
  for (const p of PAGES) {
    await page.goto(BASE + p.url, { waitUntil: 'networkidle', timeout: 45000 }).catch(() => {});
    await page.waitForTimeout(2500);
    const text = await page.evaluate(() => document.body.innerText || '');
    const hits = [];
    if (/undefined/.test(text)) hits.push('HAS_UNDEFINED');
    if (/今开\s*--/.test(text)) hits.push('OPEN_EMPTY');
    if (/暂无持仓，点击/.test(text)) hits.push('OLD_EMPTY_COPY');
    console.log(p.name, 'chars=' + text.length, 'hits=' + (hits.join(',') || 'none'));
    await page.screenshot({ path: path.join(OUT, p.name + '.png'), fullPage: true });
  }
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
