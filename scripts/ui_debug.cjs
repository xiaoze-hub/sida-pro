const { chromium } = require('playwright');
const fs = require('fs');
const TOKEN = fs.readFileSync('C:/Users/tianxiang/sida-work/data/admin_token.txt', 'utf8').trim();
(async () => {
  const b = await chromium.launch({ headless: true });
  const p = await b.newPage({ viewport: { width: 1440, height: 900 } });
  const fails = [];
  p.on('response', async (r) => {
    if (r.url().includes('/api/') && r.status() >= 400) fails.push(r.status() + ' ' + r.url());
    if (r.url().includes('market/phase') || r.url().includes('mainline')) {
      console.log('RES', r.status(), r.url(), r.headers()['content-type']);
    }
  });
  await p.goto('http://127.0.0.1:8000/login', { waitUntil: 'domcontentloaded' });
  await p.evaluate((tok) => {
    localStorage.setItem('token', tok);
    localStorage.setItem('token_expires', new Date(Date.now() + 8 * 3600 * 1000).toISOString());
  }, TOKEN);
  await p.goto('http://127.0.0.1:8000/', { waitUntil: 'networkidle', timeout: 45000 });
  await p.waitForTimeout(4000);
  const text = await p.evaluate(() => document.body.innerText || '');
  text.split('\n').forEach((l) => {
    if (/失败|超时|同步中|不可用/.test(l)) console.log('LINE:', l.slice(0, 100));
  });
  console.log('HTTP_FAILS', fails);
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
