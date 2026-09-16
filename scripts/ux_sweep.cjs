/* 全站 UX 走查: 截图 + 空态/报错/布局问题采集 */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const BASE = process.env.SIDA_BASE || 'http://127.0.0.1:8000';
const TOKEN = fs.readFileSync('C:/Users/tianxiang/sida-work/data/admin_token.txt', 'utf8').trim();
const OUT = 'C:/Users/tianxiang/sida-work/docs/screenshots/ux-sweep-20260918';
fs.mkdirSync(OUT, { recursive: true });

const PAGES = [
  { name: '01-login', url: '/login' },
  { name: '02-dashboard', url: '/' },
  { name: '03-opportunities', url: '/opportunities' },
  { name: '04-dark-fund-top', url: '/dark-fund-top' },
  { name: '05-heatmap', url: '/heatmap' },
  { name: '06-theme-mood', url: '/theme-mood' },
  { name: '07-portfolio', url: '/portfolio' },
  { name: '08-workbench-stock', url: '/stocks/600519' },
  { name: '09-workbench-index', url: '/stocks/000001?type=index' },
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

const report = [];

async function dumpPage(page, item) {
  const data = {
    name: item.name, url: item.url,
    console: [], pageErrors: [], httpErrors: [],
    emptyHits: [], bodyChars: 0, title: '',
    scrollW: 0, scrollH: 0, viewportH: 900,
  };
  page.on('console', (m) => {
    if (m.type() === 'error') data.console.push(m.text().slice(0, 200));
  });
  page.on('pageerror', (e) => data.pageErrors.push(String(e).slice(0, 200)));
  page.on('response', (r) => {
    if (r.url().includes('/api/') && r.status() >= 400) {
      data.httpErrors.push(`${r.status()} ${r.url().replace(BASE, '')}`);
    }
  });
  try {
    await page.goto(BASE + item.url, { waitUntil: 'networkidle', timeout: 45000 });
  } catch (e) {
    data.gotoError = String(e).slice(0, 150);
  }
  await page.waitForTimeout(2800);
  data.title = await page.title().catch(() => '');
  try {
    const info = await page.evaluate(() => {
      const body = document.body;
      const text = body.innerText || '';
      // 布局粗测: 文档高度 vs 视口, 大块空白
      const de = document.documentElement;
      // 收集疑似问题文本
      const lines = text.split('\n').map((s) => s.trim()).filter(Boolean);
      const issues = lines.filter((l) =>
        l.length < 90 &&
        /暂无|无数据|加载失败|失败|不可用|错误|同步中|undefined|NaN|null|暂未|敬请|敬请期待|敬请关注/.test(l)
      ).slice(0, 15);
      // 找出可能的死白: 连续多行只有空白/高度大的元素
      const bigEmpty = [];
      document.querySelectorAll('div,section,main').forEach((el) => {
        const r = el.getBoundingClientRect();
        if (r.height > 220 && r.width > 300) {
          const t = (el.innerText || '').trim();
          if (t.length < 20 && !el.querySelector('canvas,img,svg')) {
            bigEmpty.push(`${el.className?.toString().slice(0, 40) || el.tagName} h=${Math.round(r.height)} text=${t.slice(0,20)}`);
          }
        }
      });
      return {
        chars: text.length,
        issues,
        scrollH: de.scrollHeight,
        scrollW: de.scrollWidth,
        bigEmpty: bigEmpty.slice(0, 8),
      };
    });
    data.bodyChars = info.chars;
    data.emptyHits = info.issues;
    data.scrollH = info.scrollH;
    data.scrollW = info.scrollW;
    data.bigEmpty = info.bigEmpty;
  } catch {}
  const shot = path.join(OUT, item.name + '.png');
  await page.screenshot({ path: shot, fullPage: true }).catch(async () => {
    await page.screenshot({ path: shot }).catch(() => {});
  });
  data.shot = shot;
  return data;
}

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
    const item = await dumpPage(page, p);
    report.push(item);
    console.log(JSON.stringify({
      n: item.name,
      chars: item.bodyChars,
      empty: item.emptyHits?.length || 0,
      http: item.httpErrors?.length || 0,
      bigEmpty: item.bigEmpty?.length || 0,
    }));
    // 重置监听器避免叠加
    page.removeAllListeners('console');
    page.removeAllListeners('pageerror');
    page.removeAllListeners('response');
  }
  fs.writeFileSync(path.join(OUT, 'ux-report.json'), JSON.stringify(report, null, 2));
  console.log('DONE', OUT);
  await browser.close();
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
