/* SIDA 全站走查: 登录 + 逐页截图 + 空态/报错采集 */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = process.env.SIDA_BASE || 'http://127.0.0.1:8000';
const TOKEN = fs.readFileSync(process.env.SIDA_TOKEN || 'C:/Users/tianxiang/sida-work/data/admin_token.txt', 'utf8').trim();
const OUT = process.env.SIDA_OUT || 'C:/Users/tianxiang/sida-work/docs/screenshots/ui-sweep-20260918';
fs.mkdirSync(OUT, { recursive: true });

const PAGES = [
  { name: '01-login', url: '/login' },
  { name: '02-dashboard', url: '/' },
  { name: '03-opportunities', url: '/opportunities' },
  { name: '04-dark-fund-top', url: '/dark-fund-top' },
  { name: '05-heatmap', url: '/heatmap' },
  { name: '06-theme-mood', url: '/theme-mood' },
  { name: '07-portfolio', url: '/portfolio' },
  { name: '08-workbench-600519', url: '/stocks/600519' },
  { name: '09-workbench-000001-index', url: '/stocks/000001?type=index' },
  { name: '10-system', url: '/system' },
  { name: '11-system-agents', url: '/system?tab=agents' },
  { name: '12-system-datasources', url: '/system?tab=datasources' },
  { name: '13-reports', url: '/reports' },
  { name: '14-shadow', url: '/shadow' },
  { name: '15-notifications', url: '/notifications' },
  { name: '16-profile', url: '/profile' },
  { name: '17-settings', url: '/settings' },
  { name: '18-settings-help', url: '/settings?tab=help' },
];

const EMPTY_PATTERNS = [
  /暂无/g, /无数据/g, /加载失败/g, /加载中/g, /--/g, /失败/g, /不可用/g,
  /deprecated/g, /Error/g, /undefined/g, /NaN/g, /null/g
];

(async () => {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
  });
  const page = await ctx.newPage();
  const report = [];

  // 注入 token 后再打开任意页
  await page.goto(BASE + '/login', { waitUntil: 'domcontentloaded' });
  await page.evaluate((tok) => {
    localStorage.setItem('token', tok);
    // client.ts isAuthenticated 用 Date.parse — 必须 ISO8601, 数字串→NaN→logout
    localStorage.setItem('token_expires', new Date(Date.now() + 8 * 3600 * 1000).toISOString());
  }, TOKEN);

  for (const p of PAGES) {
    const item = { name: p.name, url: p.url, console: [], pageErrors: [], emptyHits: [], title: '' };
    const onConsole = (msg) => {
      if (msg.type() === 'error' || msg.type() === 'warning') {
        item.console.push(`[${msg.type()}] ${msg.text().slice(0, 300)}`);
      }
    };
    const onErr = (err) => item.pageErrors.push(String(err).slice(0, 300));
    page.on('console', onConsole);
    page.on('pageerror', onErr);
    try {
      await page.goto(BASE + p.url, { waitUntil: 'networkidle', timeout: 45000 });
    } catch (e) {
      item.gotoError = String(e).slice(0, 200);
      try { await page.goto(BASE + p.url, { waitUntil: 'domcontentloaded', timeout: 30000 }); } catch {}
    }
    await page.waitForTimeout(2500); // 等数据/骨架
    item.title = await page.title().catch(() => '');
    // 采集可见文本里的空态/失败提示
    try {
      const text = await page.evaluate(() => document.body.innerText || '');
      const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
      for (const line of lines) {
        if (line.length > 80) continue;
        if (/暂无|无数据|加载失败|失败|不可用|Error|错误|同步中|请先/.test(line)) {
          item.emptyHits.push(line.slice(0, 80));
        }
      }
      item.emptyHits = [...new Set(item.emptyHits)].slice(0, 20);
      // 粗测大块空白: 可见高度内超过 60% 无文本的纵向区间
      item.bodyChars = text.length;
    } catch {}
    const shot = path.join(OUT, p.name + '.png');
    await page.screenshot({ path: shot, fullPage: true }).catch(async () => {
      await page.screenshot({ path: shot }).catch(() => {});
    });
    item.shot = shot;
    page.off('console', onConsole);
    page.off('pageerror', onErr);
    report.push(item);
    console.log('OK', p.name, 'chars=' + (item.bodyChars||0), 'empty=' + item.emptyHits.length, 'console=' + item.console.length);
  }

  fs.writeFileSync(path.join(OUT, 'report.json'), JSON.stringify(report, null, 2));
  console.log('DONE', OUT);
  await browser.close();
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
