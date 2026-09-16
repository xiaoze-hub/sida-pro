const { chromium } = require('playwright');
const fs = require('fs');
const TOKEN = fs.readFileSync('C:/Users/tianxiang/sida-work/data/admin_token.txt', 'utf8').trim();
(async () => {
  const b = await chromium.launch({ headless: true });
  const p = await b.newPage({ viewport: { width: 1440, height: 900 } });
  await p.goto('http://127.0.0.1:8000/login', { waitUntil: 'domcontentloaded' });
  await p.evaluate((tok) => {
    localStorage.setItem('token', tok);
    localStorage.setItem('token_expires', new Date(Date.now() + 8 * 3600 * 1000).toISOString());
  }, TOKEN);
  await p.goto('http://127.0.0.1:8000/', { waitUntil: 'networkidle', timeout: 45000 });
  await p.waitForTimeout(3000);
  const text = await p.evaluate(() => document.body.innerText || '');
  const checks = {
    加载失败: /加载失败/.test(text),
    请求超时: /请求超时/.test(text),
    展开全部: /展开全部/.test(text),
    共振: /三指标共振/.test(text),
  };
  console.log(JSON.stringify(checks, null, 2));
  await p.screenshot({ path: 'C:/Users/tianxiang/sida-work/docs/screenshots/ui-sweep-20260918/v066-dashboard.png', fullPage: true });
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
