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

test('AWS picker submits the chosen multi-GPU machine and its displayed price', async () => {
  const {page} = await modelPage(0,{mode:'kubernetes',capacity:{desired:0,limit:4,machines:[],hours:4,region:'us-east-2'}});
  const options=[{instance_type:'g5.xlarge',name:'NVIDIA A10G',memory_gb:22.4,gpu_count:1,vcpus:4,host_memory_gb:16,hourly_usd:1.006,blocker:''},
    {instance_type:'g6e.12xlarge',name:'NVIDIA L40S',memory_gb:44.7,gpu_count:4,vcpus:48,host_memory_gb:384,hourly_usd:10.345,blocker:''}];
  const submitted=[];
  await page.route('**/api/gpus/options',route=>route.fulfill({json:{options}}));
  await page.route('**/api/gpus',route=>{submitted.push(route.request().postDataJSON());return route.fulfill({json:{status:'accepted'}});});
  try {
    await page.getByRole('button',{name:'inference stack',exact:true}).click();
    await page.getByRole('button',{name:'Add GPU',exact:true}).click();
    await page.getByLabel('AWS GPU',{exact:true}).selectOption('g6e.12xlarge');
    assert.match(await page.locator('main').innerText(),/Price is per machine/);
    await page.getByRole('button',{name:'Add GPU · $10.345/hour',exact:true}).click();
    await page.getByRole('heading',{name:'GPUs',exact:true}).waitFor();
    assert.deepEqual(submitted,[{expected:0,desired:1,instance_type:'g6e.12xlarge',hourly_usd:10.345}]);
  } finally {await page.close();}
});

test('AWS quota blocker is visible before launching and offers the matching quota page', async () => {
  const {page} = await modelPage(0,{mode:'kubernetes',capacity:{desired:0,limit:4,machines:[],hours:4,region:'us-east-2'}});
  const blocker='Needs 16 vCPUs; 0 remain in your AWS quota.';
  await page.route('**/api/gpus/options',route=>route.fulfill({json:{options:[{instance_type:'p5.4xlarge',name:'NVIDIA H100',memory_gb:80,gpu_count:1,vcpus:16,host_memory_gb:256,hourly_usd:4,blocker,quota_url:'https://us-east-2.console.aws.amazon.com/servicequotas/home'}]}}));
  try {
    await page.getByRole('button',{name:'inference stack',exact:true}).click();
    await page.getByRole('button',{name:'Add GPU',exact:true}).click();
    await page.getByText(blocker,{exact:true}).waitFor();
    assert.equal(await page.getByRole('button',{name:'Add GPU · $4.000/hour',exact:true}).isDisabled(),true);
    assert.equal(await page.getByRole('link',{name:'AWS quotas'}).count(),1);
  } finally {await page.close();}
});

test('An AWS catalogue failure can be retried without enabling an unpriced launch', async () => {
  const {page} = await modelPage(0,{mode:'kubernetes',capacity:{desired:0,limit:4,machines:[],hours:4,region:'us-east-2'}});
  let fail=true;
  await page.route('**/api/gpus/options',route=>fail?route.fulfill({status:503,json:{detail:'Could not read AWS prices.'}}):route.fulfill({json:{options:[{instance_type:'g5.xlarge',name:'NVIDIA A10G',memory_gb:22.4,gpu_count:1,vcpus:4,host_memory_gb:16,hourly_usd:1.006,blocker:''}]}}));
  try {
    await page.getByRole('button',{name:'inference stack',exact:true}).click();
    await page.getByRole('button',{name:'Add GPU',exact:true}).click();
    await page.getByText('Could not read AWS prices.',{exact:true}).waitFor();
    assert.equal(await page.getByRole('button',{name:'Add GPU',exact:true}).isDisabled(),true);
    fail=false;await page.getByRole('button',{name:'Retry GPU check',exact:true}).click();
    await page.getByRole('button',{name:'Add GPU · $1.006/hour',exact:true}).waitFor();
    assert.equal(await page.getByRole('button',{name:'Add GPU · $1.006/hour',exact:true}).isEnabled(),true);
  } finally {await page.close();}
});

async function modelPage(gpus = 8, overrides = {}) {
  const page = await browser.newPage();
  page.setDefaultTimeout(5000);
  const submitted = [];
  const status = {
    mode: 'compose', hardware: {count: gpus, devices: [], error: ''},
    permissions: {deploy: true, read: true}, errors: [], models: [],
    deployment: {request: null, step: '', error: ''},
    components: {litellm: {healthy: true, url: 'https://example.com/litellm/ui'}, grafana: {healthy: true, url: 'https://example.com/grafana'}, router: {healthy: true}},
    ...overrides,
  };
  const info = {id: 'example/model', suggested_gpus: 1, suggested_context: 4096, max_context: 32768};
  await page.route('http://inference-stack.test/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/gpus/options') return route.fulfill({json: {options: [{instance_type:'g5.2xlarge',name:'NVIDIA A10G',memory_gb:24,gpu_count:1,vcpus:8,host_memory_gb:32,hourly_usd:1.212,blocker:''}]}});
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
  if (!status.models.length) await page.getByText('Settings', {exact: true}).click();
  return {page, submitted, status};
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

test('GPU details distinguish detected memory use and reject unsafe AWS lifecycle links', async () => {
  const {page} = await modelPage(1, {
    hardware: {count: 1, error: '', devices: [{name: 'NVIDIA A10G', memory_gb: 22.5, used_gb: 10, count: 1, available: 1}]},
    machine: {provider: 'aws', instance_type: 'g5.2xlarge', hourly_usd: 1.212, region: 'us-east-2',
      stop_at: '2026-11-22T16:00:00Z', manage_url: 'https://us-east-2.console.aws.amazon.com/ec2/home', delete_url: 'javascript:alert(1)'},
  });
  try {
    await page.getByRole('link', {name: /1 GPU/}).click();
    await page.getByRole('heading', {name: 'GPUs', exact: true}).waitFor();
    assert.equal(await page.getByRole('cell', {name: '10 GB', exact: true}).count(), 1);
    assert.match(await page.locator('main').innerText(), /Detected GPUs may already be busy/);
    assert.match(await page.locator('main').innerText(), /stays online when no GPUs are running/);
    assert.equal(await page.getByRole('link', {name: 'Delete installation', exact: true}).count(), 0);
  } finally { await page.close(); }
});

test('Ready model opens the native Chat and identifies its API model name', async () => {
  const {page} = await modelPage(1, {
    models: [{served_model: 'example/model', name: 'example-model', status: 'ready', replicas: 1, gpus_per_replica: 1, simulate: true}],
    components: {litellm: {healthy: true, url: 'https://example.com/litellm/ui'}, grafana: {healthy: true}, router: {healthy: true}},
  });
  try {
    assert.equal(await page.getByRole('link', {name: 'Try in LiteLLM'}).getAttribute('href'), 'https://example.com/litellm/ui/chat/');
    assert.match(await page.locator('main').innerText(), /Select example-model/);
    assert.match(await page.locator('main').innerText(), /Simulation returns canned text/);
  } finally { await page.close(); }
});


test('Empty AWS home exposes zero counts and a priced GPU action without installing a model', async () => {
  const {page, submitted} = await modelPage(0, {
    mode: 'kubernetes', capacity: {desired: 0, limit: 4, machines: [], hours: 4, hourly_usd: 1.212, name: 'NVIDIA A10G', memory_gb: 24, instance_type: 'g5.2xlarge'},
  });
  const rents = [];
  await page.route('http://inference-stack.test/api/gpus', async route => {
    rents.push(route.request().postDataJSON());
    return route.fulfill({status: 409, json: {detail: 'AWS GPU quota is 0. No GPU was added.'}});
  });
  try {
    await page.getByRole('button', {name: 'inference stack', exact: true}).click();
    await page.getByRole('link', {name: '0 deployments', exact: true}).waitFor();
    assert.equal(await page.getByRole('link', {name: '0 GPUs', exact: true}).count(), 1);
    assert.equal(await page.getByRole('link', {name: 'Grafana Monitor usage and performance', exact: true}).getAttribute('href'), 'https://example.com/grafana');
    assert.equal(await page.getByText('No GPU detected', {exact: true}).count(), 0);
    await page.getByRole('button', {name: 'Add GPU', exact: true}).click();
    await page.getByRole('heading', {name: 'Add GPU', exact: true}).waitFor();
    assert.match(await page.locator('main').innerText(), /Removed automatically after 4 hours/);
    assert.equal(rents.length, 0);
    await page.getByRole('button', {name: 'Add GPU · $1.212/hour', exact: true}).click();
    await page.getByText('AWS GPU quota is 0. No GPU was added.', {exact: true}).waitFor();
    assert.deepEqual(rents, [{expected: 0, desired: 1, instance_type: 'g5.2xlarge', hourly_usd: 1.212}]);
    assert.equal(submitted.length, 0);
    assert.equal(await page.getByRole('button', {name: 'Add GPU · $1.212/hour', exact: true}).isEnabled(), true);
  } finally { await page.close(); }
});

test('Home opens Lens and Grafana directly in their native interfaces', async () => {
  const {page} = await modelPage(0, {
    components: {
      litellm: {healthy: true, url: 'https://example.com/litellm/ui'},
      lens: {healthy: true, url: 'https://example.com/lens/ui/'},
      grafana: {healthy: true, url: 'https://example.com/grafana/d/inference-stack-overview'},
      router: {healthy: true},
    },
  });
  await page.context().route('https://example.com/**', route => route.fulfill({body: '<h1>Native application</h1>', contentType: 'text/html'}));
  try {
    await page.getByRole('button', {name: 'inference stack', exact: true}).click();
    for (const [name, url] of [
      ['Lens Trace agent runs', 'https://example.com/lens/ui/'],
      ['Grafana Monitor usage and performance', 'https://example.com/grafana/d/inference-stack-overview'],
    ]) {
      const link = page.getByRole('link', {name, exact: true});
      const popupReady = page.waitForEvent('popup');
      await link.click();
      const popup = await popupReady;
      await popup.waitForURL(url);
      await popup.close();
      assert.equal(new URL(page.url()).hash, '#home');
    }
    await page.getByRole('button', {name: /^Deployments Deploy models and check health/}).click();
    await page.getByRole('heading', {name: 'Deploy a model', exact: true}).waitFor();
  } finally { await page.close(); }
});

test('Home distinguishes disabled Lens from unreachable Lens and fits narrow screens', async () => {
  const {page, status} = await modelPage();
  try {
    await page.getByRole('button', {name: 'inference stack', exact: true}).click();
    assert.equal(await page.getByRole('heading', {name: 'Lens', exact: true}).count(), 0);
    assert.equal(await page.getByText('Lens is unreachable', {exact: true}).count(), 0);
    status.components.lens = {healthy: false, url: 'https://example.com/lens/ui/'};
    await page.reload();
    await page.getByText('Lens is unreachable', {exact: true}).waitFor();
    assert.equal(await page.getByRole('link', {name: 'Lens Trace agent runs Lens is unreachable', exact: true}).getAttribute('href'), status.components.lens.url);
    for (const width of [390, 768, 1280]) {
      await page.setViewportSize({width, height: 800});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      if (width >= 768) {
        const grafana = await page.getByRole('link', {name: 'Grafana Monitor usage and performance', exact: true}).boundingBox();
        assert.ok(grafana.y + grafana.height <= 800, 'All primary destinations fit in the first desktop viewport');
      }
    }
  } finally { await page.close(); }
});

test('GPU rental moves to pending capacity and refresh shows availability', async () => {
  const {page, status} = await modelPage(0, {
    mode: 'kubernetes', capacity: {desired: 0, limit: 4, machines: [], hours: 4, hourly_usd: 1.212, name: 'NVIDIA A10G', memory_gb: 24, instance_type: 'g5.2xlarge'},
  });
  await page.route('http://inference-stack.test/api/gpus', async route => {
    status.capacity.desired = route.request().postDataJSON().desired;
    return route.fulfill({json: {status: 'accepted'}});
  });
  try {
    await page.getByRole('button', {name: 'inference stack', exact: true}).click();
    await page.getByRole('button', {name: 'Add GPU', exact: true}).click();
    await page.getByRole('button', {name: 'Add GPU · $1.212/hour', exact: true}).click();
    await page.getByRole('heading', {name: 'GPUs', exact: true}).waitFor();
    assert.match(await page.locator('main').innerText(), /Starting 1 GPU/);
    status.capacity.machines = [{id: 'i-0123456789abcdef0', state: 'ready', models: [], instance_type:'g5.2xlarge',name:'NVIDIA A10G',memory_gb:24,gpu_count:1,hourly_usd:1.212}];
    status.hardware = {count: 1, devices: [{name: 'NVIDIA A10G', count: 1, available: 1, memory_gb: 24}], error: ''};
    await page.getByRole('cell', {name: 'Available', exact: true}).waitFor({timeout: 10000});
    assert.equal(await page.getByRole('button', {name: 'Deploy a model', exact: true}).isVisible(), true);
    await page.getByRole('button', {name: 'inference stack', exact: true}).click();
    await page.getByRole('link', {name: '1 GPU', exact: true}).waitFor();
    assert.equal(await page.getByRole('link', {name: '1 GPU', exact: true}).count(), 1);
    assert.match(await page.locator('.stack-summary').innerText(), /1 available/);
  } finally { await page.close(); }
});

test('Model-first AWS flow shows price before provisioning and preserves selection on quota failure', async () => {
  const {page,status}=await modelPage(0,{mode:'kubernetes',capacity:{desired:0,limit:4,machines:[],hours:4,hourly_usd:1.212,name:'NVIDIA A10G',memory_gb:24,instance_type:'g5.2xlarge'}});
  const requests=[];
  await page.route('http://inference-stack.test/api/models',async route=>{
    requests.push(route.request().postDataJSON());
    await route.fulfill({status:409,json:{detail:'AWS GPU quota is zero. No GPU was added.'}});
  });
  try {
    assert.match(await page.locator('[data-rental]').innerText(),/\$1\.212\/hour/);
    assert.equal(requests.length,0);
    await page.getByRole('button',{name:'Add GPU and deploy',exact:true}).click();
    await page.getByText('AWS GPU quota is zero. No GPU was added.',{exact:true}).waitFor();
    assert.equal(requests.length,1);
    assert.equal(requests[0].add_gpu,true);
    assert.equal(requests[0].expected_capacity,0);
    assert.equal(requests[0].model,'example/model');
    assert.equal(await page.getByRole('button',{name:'Add GPU and deploy',exact:true}).isEnabled(),true);
    status.hardware={count:1,devices:[{count:1,available:1,memory_gb:24,name:'NVIDIA A10G'}],error:''};
    status.capacity.desired=1;
    await page.getByRole('button',{name:'Deploy',exact:true}).waitFor({timeout:10000});
    assert.equal(await page.locator('[data-rental]').innerText(),'');
  } finally {await page.close();}
});

test('Deployment details expose logs and contextual commands without replacing LiteLLM',async()=>{
  const {page}=await modelPage(1,{api_base:'https://example.com/litellm/v1',models:[{served_model:'example/model',name:'example-model',status:'ready',replicas:1,gpus_per_replica:1,context:4096,simulate:true}],components:{litellm:{healthy:true,url:'https://example.com/litellm/ui'},grafana:{healthy:true},router:{healthy:true}}});
  await page.route('http://inference-stack.test/api/models/example-model/diagnostics',route=>route.fulfill({json:{logs:'Model is serving',configuration:'{"engine":"simulator"}',commands:'docker logs --tail 200 vllm-0',note:'Run on the Docker host.',error:''}}));
  try {
    await page.getByRole('button',{name:'Logs',exact:true}).click();
    await page.getByText('Model is serving',{exact:true}).waitFor();
    await page.getByRole('button',{name:'Advanced',exact:true}).click();
    await page.getByText('docker logs --tail 200 vllm-0',{exact:true}).waitFor();
    assert.equal(await page.getByRole('link',{name:'Try in LiteLLM'}).getAttribute('href'),'https://example.com/litellm/ui/chat/');
    assert.match(await page.locator('.stack-api').innerText(),/https:\/\/example.com\/litellm\/v1/);
  } finally {await page.close();}
});
