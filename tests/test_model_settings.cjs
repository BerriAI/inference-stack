const {test, before, after} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
let browser;
before(async () => {
  browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined});
});
after(async () => { await browser?.close(); });

async function modelPage(gpus = 8) {
  const page = await browser.newPage();
  page.setDefaultTimeout(5000);
  const submitted = [];
  const status = {
    mode: 'compose', hardware: {count: gpus, devices: [], error: ''},
    permissions: {deploy: true, read: true}, errors: [], models: [],
    deployment: {request: null, step: '', error: ''},
    components: {litellm: {healthy: true}, grafana: {healthy: true}, router: {healthy: true}},
  };
  const info = {id: 'example/model', suggested_gpus: 1, suggested_context: 4096, max_context: 32768};
  await page.route('http://inference-stack.test/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/status') return route.fulfill({json: status});
    if (url.pathname.startsWith('/api/catalog/')) return route.fulfill({json: info});
    if (url.pathname === '/api/models' && route.request().method() === 'POST') {
      submitted.push(route.request().postDataJSON());
      return route.fulfill({json: {name: 'example-model'}});
    }
    const file = url.pathname.startsWith('/static/') ? 'launcher' + url.pathname : 'launcher/static/index.html';
    const types = {'.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.html': 'text/html'};
    return route.fulfill({body: fs.readFileSync(path.join(root, file)), contentType: types[path.extname(file)] || 'image/png'});
  });
  await page.goto('http://inference-stack.test/#model/example%2Fmodel');
  await page.getByText('Settings', {exact: true}).click();
  return {page, submitted};
}

for (const [label, field, value] of [['Replicas', 'replicas', 2], ['GPUs per replica', 'gpus_per_replica', 2], ['Context length', 'context', 8192]]) {
  test(`Deploy submits on the first click after changing ${label}`, async () => {
    const {page, submitted} = await modelPage();
    try {
      await page.getByLabel(label, {exact: true}).fill(String(value));
      const request = page.waitForResponse(r => r.url().endsWith('/api/models') && r.request().method() === 'POST');
      await page.getByRole('button', {name: 'Deploy', exact: true}).click();
      await request;
      assert.equal(submitted.length, 1);
      assert.equal(submitted[0][field], value);
    } finally { await page.close(); }
  });
}

test('GPU allocation updates and recovers without replacing the focused input', async () => {
  const {page, submitted} = await modelPage(2);
  try {
    const replicas = page.getByLabel('Replicas', {exact: true});
    await replicas.fill('3');
    assert.equal(await replicas.evaluate(el => el === document.activeElement), true);
    assert.equal(await page.locator('.stack-allocation strong').textContent(), '3 GPUs · 3 replicas');
    assert.equal(await page.getByRole('button', {name: 'Deploy', exact: true}).isDisabled(), true);
    assert.equal(await page.locator('[data-allocation-error]').isVisible(), true);
    await replicas.fill('2');
    assert.equal(await page.locator('[data-allocation-error]').isVisible(), false);
    const request = page.waitForResponse(r => r.url().endsWith('/api/models') && r.request().method() === 'POST');
    await page.getByRole('button', {name: 'Deploy', exact: true}).click();
    await request;
    assert.equal(submitted.length, 1);
    assert.equal(submitted[0].replicas, 2);
  } finally { await page.close(); }
});
