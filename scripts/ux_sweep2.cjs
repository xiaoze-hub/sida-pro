const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const BASE = 'http://127.0.0.1:8000';
const TOKEN = fs.readFileSync('C:/Users/tianxiang/sida-work/data/admin_token.txt', 'utf8').trim();
const OUT = 'C:/Users/tianxiang/sida-work/docs/screenshots/ux-sweep-20260918';

const PAGES = [
  { name: '10-workbench-board', url: '/stocks/880665?type=board' },
  { name: '11-workbench-l2', url: '/stocks/600519?tab=l2' },
  { name: '12-workbench-suggest', url: '/stocks/600519?tab=suggest' },
  { name: '13-workbench-fundamental', url: '/stocks/600519?tab=fundamental' },
  { name: '14-workbench-news', url: '/stocks/600519?tab=news' },
  { name: '15-workbench-research', url: '/stocks/600519?tab=research' },
  { name: '16-workbench-forecast', url: '/stocks/600519?tab=forecast' },
  { name: '17-system-agents', url: '/system?tab=agents' },
  { name: '18-system-datasources', url: '/system?tab=datasources' },
  { name: '19-system-jobs', url: '/system?tab=jobs' },
  { name: '20-system-errors', url: '/system?tab=errors' },
  { name: '21-reports', url: '/reports' },
  { name: '22-shadow', url: '/shadow' },
  { name: '23-notifications', url: '/notifications' },
  { name: '24-alerts', url: '/notifications?tab=alerts' },
  { name: '25-profile', url: '/profile' },
  { name: '26-settings', url: '/settings' },
  { name: '27-settings-help', url: '/settings?tab=help' },
  { name: '28-settings-ai', url: '/settings?tab=ai' },
  { name: '29-settings-notify', url: '/settings?tab=notify' },
  { name: '30-settings-users', url: '/settings?tab=users' },
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
  const report = [];
  for (const item of PAGES) {
    const httpErrors = [];
    const onRes = (r) => { if (r.url().includes('/api/') && r.status() >= 400) httpErrors.push(`${r.status()} ${r.url().replace(BASE,'')}`) };
    page.on('response', onRes);
    try { await page.goto(BASE + item.url, { waitUntil: 'domcontentloaded', timeout: 20000 }); } catch {}
    await page.waitForTimeout(3500);
    const info = await page.evaluate(() => {
      const text = document.body.innerText || '';
      const issues = text.split('\n').map(s=>s.trim()).filter(Boolean)
        .filter(l => l.length < 90 && /暂无|无数据|加载失败|失败|不可用|错误|同步中|undefined|NaN|null|暂未|敬请/.test(l)).slice(0,15);
      const bigEmpty = [];
      document.querySelectorAll('div,section,main').forEach((el) => {
        const r = el.getBoundingClientRect();
        if (r.height > 220 && r.width > 300) {
          const t = (el.innerText || '').trim();
          if (t.length < 20 && !el.querySelector('canvas,img,svg')) {
            bigEmpty.push(`${(el.className||el.tagName).toString().slice(0,50)} h=${Math.round(r.height)}`);
          }
        }
      });
      return { chars: text.length, issues, bigEmpty: bigEmpty.slice(0,8), url: location.pathname+location.search };
    }).catch(() => ({ chars:0, issues:[], bigEmpty:[], url: '' }));
    const shot = path.join(OUT, item.name + '.png');
    await page.screenshot({ path: shot, fullPage: true }).catch(()=>{});
    page.off('response', onRes);
    const rec = { name: item.name, url: item.url, ...info, httpErrors: [...new Set(httpErrors)] };
    report.push(rec);
    console.log(JSON.stringify({ n: rec.name, chars: rec.chars, empty: rec.issues.length, http: rec.httpErrors.length, big: rec.bigEmpty.length }));
  }
  // merge with previous if exists
  let prev = [];
  try { prev = JSON.parse(fs.readFileSync(path.join(OUT,'ux-report.json'),'utf8')); } catch {}
  const map = new Map();
  [...prev, ...report].forEach(r => map.set(r.name, r));
  fs.writeFileSync(path.join(OUT, 'ux-report.json'), JSON.stringify([...map.values()], null, 2));
  console.log('DONE');
  await browser.close();
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
