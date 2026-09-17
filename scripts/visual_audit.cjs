/* SIDA 全站视觉巡检 */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = 'http://100.91.30.35:8000';
const SHOTS = path.join(__dirname, '..', 'shots');
fs.mkdirSync(SHOTS, { recursive: true });

const PAGES = [
  { path: '/', name: 'landing' },
  { path: '/login', name: 'login' },
  { path: '/login?mode=register', name: 'register' },
  { path: '/developers', name: 'developers' },
  { path: '/', name: 'dashboard' },
  { path: '/stocks/000001?type=index', name: 'stocks' },
  { path: '/heatmap', name: 'heatmap' },
  { path: '/opportunities', name: 'opportunities' },
  { path: '/dark-fund-top', name: 'dark-fund-top' },
  { path: '/reports', name: 'reports' },
  { path: '/portfolio', name: 'portfolio' },
  { path: '/shadow', name: 'shadow' },
  { path: '/notifications', name: 'notifications' },
  { path: '/settings', name: 'settings' },
  { path: '/profile', name: 'profile' },
  { path: '/api-keys', name: 'api-keys' },
];

(async () => {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' });
  const page = await ctx.newPage();

  const consoleErrors = [];
  const failedReqs = [];
  page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 200)); });
  page.on('requestfailed', r => failedReqs.push(`${r.method()} ${r.url()}`));
  page.on('response', r => { if (r.status() >= 400) failedReqs.push(`${r.status()} ${r.url()}`); });

  console.log('访问落地页...');
  await page.goto(BASE + '/', { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(2000);

  const results = [];
  for (const p of PAGES) {
    console.log(`\n访问 ${p.name} (${p.path})...`);
    consoleErrors.length = 0; failedReqs.length = 0;
    try {
      await page.goto(BASE + p.path, { waitUntil: 'networkidle', timeout: 30000 });
      await page.waitForTimeout(3000);
      const url = page.url();
      const title = await page.title();
      const innerText = await page.evaluate(() => document.body.innerText.slice(0, 200));
      await page.screenshot({ path: path.join(SHOTS, `${p.name}.png`), fullPage: false });
      const isLogin = url.includes('/login');
      const isEmpty = innerText.trim().length < 10;
      results.push({
        page: p.name, path: p.path, url, title,
        innerText: innerText.replace(/\n/g, ' | ').slice(0, 200),
        isLogin, isEmpty,
        consoleErrors: [...consoleErrors],
        failedReqs: [...failedReqs],
      });
      console.log(`  URL: ${url}`);
      console.log(`  内容: ${innerText.slice(0, 80).replace(/\n/g, ' ')}...`);
      console.log(`  登录重定向: ${isLogin}, 空白: ${isEmpty}`);
      console.log(`  控制台错误: ${consoleErrors.length}, 请求失败: ${failedReqs.length}`);
      if (consoleErrors.length) console.log(`    首个错误: ${consoleErrors[0].slice(0, 100)}`);
      if (failedReqs.length) console.log(`    首个失败: ${failedReqs[0].slice(0, 100)}`);
    } catch (e) {
      results.push({ page: p.name, path: p.path, error: e.message });
      console.log(`  错误: ${e.message}`);
    }
  }

  await browser.close();
  fs.writeFileSync(path.join(SHOTS, 'audit_report.json'), JSON.stringify({ timestamp: new Date().toISOString(), pages: results }, null, 2));

  console.log('\n\n======== 巡检汇总 ========');
  for (const r of results) {
    const issues = [];
    if (r.error) issues.push(`ERROR`);
    if (r.isLogin) issues.push('登录重定向');
    if (r.isEmpty) issues.push('空白页');
    if (r.consoleErrors?.length) issues.push(`控制台错误×${r.consoleErrors.length}`);
    if (r.failedReqs?.length) issues.push(`请求失败×${r.failedReqs.length}`);
    console.log(`${issues.length === 0 ? '✅' : '❌'} ${r.page}: ${issues.join(', ') || '正常'}`);
  }
})();
