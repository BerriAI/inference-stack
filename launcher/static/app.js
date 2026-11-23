const app = document.querySelector('#app');
const message = document.querySelector('#message');
const forwardArrow = '<svg class="stack-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14m-6-6 6 6-6 6"/></svg>';
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {status:null, info:null, query:'', results:[], cursor:'', searching:false, searchError:'',
  gpuOptions:null, gpuError:'', gpuLoading:false, selectedGpu:'',
  modelError:'', diagnostics:null, panel:'', editing:false, request:{replicas:1, gpus_per_replica:1, context:4096, simulate:false}, settingsOpen:false, command:'', routing:null, routingOpen:false};
let searchTimer, searchController, detailController, busy = false, lastStatus = '';

async function api(path, options = {}) {
  const response = await fetch(path, {headers:{'Content-Type':'application/json'}, ...options});
  if (response.status === 401) { location.replace('/login' + location.hash); throw new Error('Sign in to continue.'); }
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Check the entered values and try again.');
  return body;
}
function say(text = '') { message.textContent = text; }
function route() {
  const [page = 'home', ...rest] = location.hash.slice(1).split('/');
  return {page:page || 'home', id:decodeURIComponent(rest.join('/'))};
}
function go(page, id = '') { location.hash = page + (id ? '/' + encodeURIComponent(id) : ''); }
function button(text, action, primary = false, disabled = false) {
  return `<button type="button" class="stack-button ${primary?'primary':''}" data-action="${action}" ${disabled?'disabled':''}>${text}</button>`;
}
function textButton(text, action) { return `<button type="button" class="stack-text-button" data-action="${action}">${text.replace(/ →$/, ' ' + forwardArrow)}</button>`; }
function external(key, text, primary = false, suffix = '') {
  const base = state.status.components[key]?.url;
  const url = base ? base.replace(/\/$/, '') + suffix : '';
  if (!url || !/^https?:\/\//.test(url)) return '';
  return `<a class="${primary?'stack-button primary':'stack-text-button'}" href="${escape(url)}" target="_blank" rel="noopener noreferrer">${text} ${forwardArrow}</a>`;
}
function logo(key) {
  if (key === 'deployments') return '<svg class="stack-logo" viewBox="0 0 40 40" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><rect x="5" y="6" width="30" height="11" rx="3"/><rect x="5" y="23" width="30" height="11" rx="3"/><path d="M11 11.5h3m-3 17h3"/></svg>';
  if (key === 'litellm') return '<img class="stack-logo logo-light" src="/static/logos/litellm.svg" alt=""><img class="stack-logo logo-dark" src="/static/logos/litellmDark.svg" alt="">';
  return `<img class="stack-logo logo-${key}" src="/static/logos/${key}.${['grafana','huggingface','lens'].includes(key)?'svg':'png'}" alt="">`;
}
function hardware() {
  const h = state.status.hardware;
  if (h.count === null) return 'GPU capacity unavailable';
  if (!h.count) return 'No GPU detected';
  const types = [...new Set(h.devices.map(d => `${d.name}${d.memory_gb?' · '+d.memory_gb+' GB':''}`))];
  return `${h.count} GPU${h.count===1?'':'s'} · ${types.join(', ')}`;
}
function hardwareLink() { return `<a href="#gpus">${escape(hardware())} ${forwardArrow}</a>`; }
function awsLink(url, label) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== 'https:' || !/^[a-z0-9-]+\.console\.aws\.amazon\.com$/.test(parsed.hostname)) return '';
    return `<a class="stack-text-button" href="${escape(url)}" target="_blank" rel="noopener noreferrer">${label} ${forwardArrow}</a>`;
  } catch { return ''; }
}
function gpuDeadline(c) {
  const stop = c?.stop_at ? new Date(c.stop_at) : null;
  return stop && Number.isFinite(stop.getTime()) ? `GPUs are removed at ${escape(stop.toLocaleString())}. Running models stop then.` : `GPUs are removed automatically after ${escape(c?.hours || 4)} hours.`;
}
function selectedGpu() { return state.gpuOptions?.find(row=>row.instance_type===state.selectedGpu); }
function gpuPrice(gpu) { return Number.isFinite(gpu?.hourly_usd) ? '$'+gpu.hourly_usd.toFixed(3)+'/hour' : 'Price unavailable'; }
function gpuSpec(gpu) { return `${gpu.gpu_count || '?'} × ${gpu.name}${gpu.memory_gb?' · '+gpu.memory_gb+' GiB per GPU':''}`; }
function gpuPicker() {
  if(state.gpuLoading) return '<p class="stack-note" role="status">Checking GPUs and prices in AWS…</p>';
  if(state.gpuError) return `<p class="stack-blocker" role="alert">${escape(state.gpuError)}</p>${textButton('Retry GPU check','gpu-options')}`;
  if(!state.gpuOptions?.length) return '<p class="stack-note">No supported NVIDIA GPUs are offered in this installation’s availability zone.</p>'+textButton('Check again','gpu-options');
  const gpu=selectedGpu();
  return `<div class="stack-fields stack-gpu-picker"><label>AWS GPU<select name="gpu_type" aria-label="AWS GPU">${state.gpuOptions.map(row=>`<option value="${escape(row.instance_type)}" ${row.instance_type===state.selectedGpu?'selected':''}>${escape(gpuSpec(row))} · ${escape(row.instance_type)} · ${gpuPrice(row)}${row.blocker?' · Check availability':''}</option>`).join('')}</select></label></div>${gpu?`<p class="stack-note">${gpu.vcpus} vCPUs · ${gpu.host_memory_gb} GiB system memory · ${escape(state.status.capacity.region)}. Price is per machine.</p>${gpu.blocker?`<p class="stack-blocker" role="alert">${escape(gpu.blocker)}</p>${awsLink(gpu.quota_url,'AWS quotas')}`:''}`:''}`;
}
async function loadGpuOptions() {
  if(!state.status.capacity || state.gpuLoading) return;
  state.gpuLoading=true;state.gpuError='';render();
  try {
    state.gpuOptions=(await api('/api/gpus/options')).options;
    const fits=row=>!state.info?.estimated_gb || row.memory_gb*row.gpu_count>=state.info.estimated_gb;
    if(!state.gpuOptions.some(row=>row.instance_type===state.selectedGpu)) {
      state.selectedGpu=(state.gpuOptions.find(row=>!row.blocker && fits(row)) || state.gpuOptions.find(fits) || state.gpuOptions[0])?.instance_type || '';
      suggestGpuAllocation();
    }
  } catch(error) {state.gpuError=error.message;state.gpuOptions=null;}
  finally {state.gpuLoading=false;render();}
}
function suggestGpuAllocation() {
  const gpu=selectedGpu();
  if(route().page==='model' && gpu && state.info?.estimated_gb && !state.settingsOpen) {
    state.request.gpus_per_replica=Math.min(8,2**Math.ceil(Math.log2(Math.max(1,state.info.estimated_gb/gpu.memory_gb))));
  }
}
function addGpuPage() {
  const c=state.status.capacity,gpu=selectedGpu();
  if(!c || c.desired==null) return `<div class="stack-content"><h1 tabindex="-1">Add GPU</h1><p class="stack-blocker">${escape(c?.error || 'GPU provisioning is available on AWS installations.')}</p>${button('Check again','refresh')}</div>`;
  return `<div class="stack-content stack-narrow"><div class="stack-back">${textButton('← GPUs','gpus')}</div><h1 tabindex="-1">Add GPU</h1>${gpuPicker()}<p class="stack-note">AWS bills your account for the machine, a 200 GB disk, IPv4 and network usage. AWS confirms spare capacity when it launches. No model is installed automatically.</p><p class="stack-note">${c.desired?gpuDeadline(c):`Removed automatically after ${c.hours} hours, including its disk. Running models stop then.`}</p>${c.desired>=c.limit?'<p class="stack-blocker">This installation has reached its machine limit. Remove a GPU to add another.</p>':''}<div class="stack-actions">${button(gpu?'Add GPU · '+gpuPrice(gpu):'Add GPU','rent-gpu',true,busy || state.gpuLoading || !!state.gpuError || !gpu || !!gpu?.blocker || c.desired>=c.limit)}${textButton('Cancel','gpus')}</div></div>`;
}
function machinePage() {
  const h = state.status.hardware, m = state.status.machine, c = state.status.capacity;
  let content = '';
  if (c) {
    const machines = c.machines || [];
    const pending = Math.max(0,(c.desired || 0)-machines.filter(g=>g.state!=='removing').length);
    content = `${c.error?`<p class="stack-blocker">${escape(c.error)}</p>`:''}${machines.length?`<table class="stack-table"><thead><tr><th>GPU</th><th>State</th><th></th></tr></thead><tbody>${machines.map((g,i)=>`<tr><td>${escape(gpuSpec(g))}<small>${escape(g.instance_type)} · ${gpuPrice(g)}</small></td><td>${g.state==='ready'?(g.models?.length?'In use':'Available'):g.state==='removing'?'Removing…':'Starting…'}${g.models?.length?'<small>'+g.models.map(name=>escape(state.status.models.find(m=>m.name===name)?.served_model.split('/').at(-1) || name)).join(', ')+'</small>':''}</td><td>${g.state==='removing'?'':`<button class="stack-text-button" data-remove-gpu="${escape(g.id)}" ${busy?'disabled':''}>Remove</button>`}</td></tr>`).join('')}</tbody></table>`:!pending?'<p class="stack-note">No GPUs added yet.</p>':''}${pending?`<p class="stack-note" role="status">Starting ${pending} GPU${pending===1?'':'s'}… You can leave this page while AWS prepares the machine.</p>`:''}${c.desired?`<p class="stack-note">${gpuDeadline(c)}</p>`:''}${pending && !machines.length?`<div class="stack-actions">${textButton('Cancel pending GPUs','cancel-gpus')}</div>`:''}<p class="stack-note">Removing a GPU ends its compute and disk charges once AWS finishes termination. Remove its models first.</p>${h.count>0?`<div class="stack-actions">${button('Deploy a model','catalogue',true)}</div>`:''}`;
  } else {
    content = `${h.error?`<p class="stack-blocker">${escape(h.error)}</p>`:''}${h.devices.length?`<table class="stack-table"><thead><tr><th>GPU</th><th>Memory</th><th>${state.status.mode==='compose'?'Memory used':'Available GPUs'}</th></tr></thead><tbody>${h.devices.map(d=>`<tr><td>${escape(d.name)}${d.count>1?` × ${d.count}`:''}</td><td>${d.memory_gb?escape(d.memory_gb)+' GB':'Unknown'}</td><td>${state.status.mode==='compose'?(d.used_gb==null?'Unknown':escape(d.used_gb)+' GB'):escape(d.available)+' / '+escape(d.count)}</td></tr>`).join('')}</tbody></table>`:''}<p class="stack-note">${state.status.mode==='compose'?'GPUs on this Docker host. Memory used includes other applications. Detected GPUs may already be busy.':'GPUs on ready, schedulable cluster nodes. Availability excludes resources allocated to pods.'}</p>`;
  }
  return `<div class="stack-content"><div class="stack-back">${textButton('← Home','home')}</div><div class="stack-heading"><h1 tabindex="-1">GPUs</h1>${c?button('Add GPU','add-gpu',true,c.desired==null):textButton('Refresh','refresh')}</div>${content}${m?.provider==='aws'?`<p class="stack-note">Launcher: ${escape(m.instance_type)} · $${Number(m.hourly_usd).toFixed(4)}/hour, plus disk, IPv4 and network. It stays online when no GPUs are running.</p><div class="stack-actions">${awsLink(m.delete_url,'Delete installation')}</div>`:''}</div>`;
}
function homeSummary() {
  const s = state.status, h = s.hardware, c = s.capacity;
  const available = h.count===null ? null : h.devices.reduce((sum,d)=>sum+(d.available ?? 0),0);
  return `<div class="stack-summary"><div><a href="#models">${s.errors.length?'Deployments unavailable':s.models.length+' deployment'+(s.models.length===1?'':'s')}</a><span aria-hidden="true">·</span><a href="#gpus">${h.count===null?'GPUs unavailable':h.count+' GPU'+(h.count===1?'':'s')}</a>${s.mode==='kubernetes' && available!==null?`<span class="stack-muted">· ${available} available</span>`:''}${c?.pending?`<span class="stack-muted">· ${c.pending} machine${c.pending===1?'':'s'} starting</span>`:''}</div>${c?textButton('Add GPU →','add-gpu'):textButton('View GPUs →','gpus')}</div>${c?.error?`<p class="stack-blocker">${escape(c.error)}</p>`:''}`;
}
function selectedModel() { return state.status.models.find(m => m.served_model === route().id); }
function selectedDeployment() { return state.status.deployment.request?.model === route().id ? state.status.deployment : null; }
function header() {
  const page = route().page;
  const section = page === 'home' ? '' : page === 'routing' ? 'llm-d' : ['gpus','add-gpu'].includes(page) ? 'GPUs' : 'Deployments';
  return `<header class="stack-header"><button class="stack-brand" data-action="home">inference stack</button>${section?`<nav class="stack-crumb" aria-label="Breadcrumb"><span aria-hidden="true">/</span><button data-action="${section==='llm-d'?'routing':section==='GPUs'?'gpus':'vllm'}">${section}</button></nav>`:''}<button class="stack-text-button stack-signout" data-action="logout">Sign out</button></header>`;
}
function home() {
  const s = state.status;
  const cards = [
    ['deployments','Deployments','Deploy models and check health','vllm',s.errors[0] || (s.deployment.error?'A deployment needs attention':s.models.some(m=>m.simulate)?'':s.hardware.count===0 && !s.capacity?'No GPU detected':s.hardware.error?'GPU check unavailable':'')],
    ['litellm','LiteLLM','Manage models and send requests','litellm',s.components.litellm.healthy?'':'LiteLLM is unreachable']
  ];
  return `${homeSummary()}<div class="stack-home">${cards.map(([brand,title,subtitle,action,problem]) => {
    const url = s.components[action]?.url;
    const open = url && /^https?:\/\//.test(url) ? `<a href="${escape(url)}" target="_blank" rel="noopener noreferrer"` : `<button type="button" data-action="${action}"`;
    return `${open} class="stack-tile">${logo(brand)}<span class="tile-arrow">${forwardArrow}</span><div><h2>${title}</h2><p>${subtitle}</p>${problem?`<div class="tile-problem">${escape(problem)}</div>`:''}</div>${url?'</a>':'</button>'}`;
  }).join('')}</div><div class="stack-secondary"><div>${external('grafana','Grafana · Usage and performance')}${!s.components.grafana.healthy?'<p class="stack-blocker">Grafana is unreachable</p>':''}</div><div>${external('lens','Lens · Agent traces')}${s.components.lens && !s.components.lens.healthy?'<p class="stack-blocker">Lens is unreachable</p>':''}</div><div>${s.models.length?textButton('Replica scheduling →','routing'):''}${!s.components.router.healthy?'<p class="stack-blocker">Routing needs attention</p>':''}</div></div>${poweredBy()}`;
}
function poweredBy() {
  const projects = [['huggingface','Hugging Face','https://huggingface.co'],['vllm','vLLM','https://vllm.ai'],['litellm','LiteLLM','https://www.litellm.ai'],['grafana','Grafana','https://grafana.com'],['lens','Lens','https://github.com/BerriAI/lens']];
  if(state.status.mode==='compose' || state.status.models.length) projects.push(['llmd','llm-d','https://llm-d.ai']);
  return `<footer class="stack-powered" aria-label="Powered by open source">${projects.map(([key,name,url])=>`<a href="${url}" title="${name}" aria-label="${name} project" target="_blank" rel="noopener noreferrer">${logo(key)}</a>`).join('')}</footer>`;
}
function fit(model) {
  if (model.blocker) return ['Unsupported task','fit-no'];
  if (!model.estimated_gb) return ['','fit-no'];
  const devices = state.status.hardware.devices;
  const memory = Math.max(0, ...devices.map(d=>(d.memory_gb || 0)*d.available));
  if (memory && model.estimated_gb > memory) return ['Needs more memory','fit-warn'];
  return ['','stack-muted'];
}
function results() {
  if (state.searching && !state.results.length) return '<p class="stack-note" role="status">Searching Hugging Face…</p>';
  if (state.searchError) return `<p class="stack-blocker" role="alert">${escape(state.searchError)}</p>${button('Retry search','search')}`;
  return `<div class="catalog-head"><span>Model</span><span>Estimated GPU memory</span></div>${state.results.map(m => {
    const [label,cls] = fit(m);
    const parts = m.id.split('/');
    return `<button class="catalog-item" data-model="${escape(m.id)}"><span class="catalog-name"><strong>${escape(parts.at(-1))}</strong><small>${escape(parts.length>1?parts[0]:'Hugging Face')}${m.gated?' · Access required':''}</small></span><span class="catalog-fit">${m.estimated_gb?'≈ '+m.estimated_gb+' GB':'Unknown'}${label?`<small class="${cls}">${label}</small>`:''}</span></button>`;
  }).join('')}${!state.results.length?'<p class="stack-note">No models found. Try another name or a full model ID.</p>':''}${state.cursor?`<div class="stack-actions">${button(state.searching?'Loading…':'Show more','more',false,state.searching)}</div>`:''}<p class="stack-note">Estimates vary with context length, quantization and vLLM support.</p>`;
}
function catalogue() {
  return `<div class="stack-content"><div class="stack-heading"><div><h1 tabindex="-1">Deploy a model</h1><p class="stack-sub">${hardwareLink()}</p></div>${state.status.models.length?textButton('Deployments','models'):''}</div><input class="stack-search" type="search" aria-label="Search Hugging Face models" placeholder="Search Hugging Face or paste a model URL" value="${escape(state.query)}" autocomplete="off"><div id="results">${results()}</div></div>`;
}
function modelList() {
  const rows = state.status.models;
  const pending = state.status.deployment;
  return `<div class="stack-content"><div class="stack-heading"><div><h1 tabindex="-1">Deployments</h1><p class="stack-sub">${hardwareLink()}</p></div>${button('Deploy a model','catalogue',true)}</div>${state.status.errors.map(e=>`<p class="stack-blocker">${escape(e)}</p>`).join('')}${rows.length?`<table class="stack-table"><thead><tr><th>Model</th><th>Allocation</th><th>Status</th></tr></thead><tbody>${rows.map(m=>`<tr><td><button class="stack-model-link" data-model="${escape(m.served_model)}">${escape(m.served_model.split('/').at(-1))}</button><small>${escape(m.served_model)}</small></td><td>${m.simulate?'Simulated':m.replicas*m.gpus_per_replica+' GPUs'} · ${m.replicas} replica${m.replicas===1?'':'s'}</td><td>${m.status==='ready'?'Ready':pending.error && pending.name===m.name?'Needs attention':'Starting…'}</td></tr>`).join('')}</tbody></table>`:'<p class="stack-note">No models deployed yet.</p>'}${pending.request && (pending.step || pending.error)?`<div class="stack-actions"><button class="stack-text-button" data-model="${escape(pending.request.model)}">${escape(pending.step || 'Review deployment')} ${forwardArrow}</button></div>`:''}</div>`;
}
function needsGpu(request = state.request) {
  const s=state.status, c=s.capacity;
  if(!c || request.simulate || selectedModel()) return false;
  const available=s.hardware.devices.reduce((sum,d)=>sum+((d.memory_gb || 0)*request.gpus_per_replica >= (state.info?.estimated_gb || 0)?Math.floor(d.available/request.gpus_per_replica):0),0);
  return request.replicas>available;
}
function allocationProblem(info, request) {
  if (request.simulate) return '';
  if (info.blocker) return info.blocker;
  const h = state.status.hardware;
  if (needsGpu(request)) {
    const c=state.status.capacity;
    if(c.desired==null) return c.error || 'AWS capacity is unavailable. Check GPUs and retry.';
    if(c.desired>=c.limit) return 'No suitable GPU is available. Free an existing machine or remove one to add another.';
    if(state.gpuLoading) return 'Checking GPUs in AWS…';
    if(state.gpuError) return state.gpuError;
    const gpu=selectedGpu();
    if(!gpu) return 'Choose an AWS GPU.';
    if(gpu.blocker) return gpu.blocker;
    if(request.gpus_per_replica*request.replicas>gpu.gpu_count) return 'This allocation needs more GPUs than the selected machine. Choose another machine or add GPUs first.';
    if(info.estimated_gb>gpu.memory_gb*request.gpus_per_replica) return 'This model needs more GPU memory. Choose another machine or more GPUs per replica.';
    if(c.pending) return 'A GPU machine is already starting. Wait for it to become available, then deploy.';
  } else if (h.count === 0) return state.status.mode==='compose' ? 'No GPU is available to Docker. Check the NVIDIA driver and Container Toolkit.' : 'No GPU is available. Add GPU capacity to this cluster.';
  if (h.count === null && state.status.mode==='compose') return h.error;
  if (!needsGpu(request) && h.count !== null && request.replicas*request.gpus_per_replica > h.count) return 'This allocation needs more GPUs than this installation has.';
  if (info.max_context && request.context > info.max_context) return `This model supports up to ${info.max_context.toLocaleString()} context tokens.`;
  return '';
}
function allocationTitle(request) {
  return `${request.simulate?'Simulated model':request.replicas * request.gpus_per_replica+' GPU'+(request.replicas*request.gpus_per_replica===1?'':'s')} · ${request.replicas} replica${request.replicas===1?'':'s'}`;
}
function updateAllocation() {
  const title = app.querySelector('.stack-allocation strong');
  const error = app.querySelector('[data-allocation-error]');
  const deploy = app.querySelector('[data-action="deploy"]');
  if (!title || !error || !deploy || !state.info) return;
  title.textContent = allocationTitle(state.request);
  const problem = allocationProblem(state.info,state.request);
  error.textContent = problem;
  error.hidden = !problem;
  deploy.disabled = !!problem || busy;
  deploy.textContent = deployLabel();
  const rental=app.querySelector('[data-rental]'); if(rental) rental.innerHTML=rentalDetails();
}
function deployLabel() {
  if(!state.status.permissions.deploy) return 'Get deployment command';
  if(needsGpu()) return 'Add GPU and deploy';
  if(state.editing) return 'Apply changes';
  if(state.status.mode==='compose' && state.status.models.some(m=>m.served_model!==route().id)) return 'Replace deployment';
  return selectedDeployment()?.error ? 'Retry deployment' : 'Deploy';
}
function rentalDetails() {
  if(!needsGpu()) return '';
  return `<div class="stack-rental">${gpuPicker()}<p>Disk, IPv4 and network are additional. AWS confirms spare capacity at launch.</p><p>${gpuDeadline(state.status.capacity)}</p></div>`;
}
function deploymentTools() {
  const d=state.diagnostics;
  return `<div class="stack-actions">${textButton(state.panel==='logs'?'Hide logs':'Logs','logs')}${textButton(state.panel==='advanced'?'Hide advanced':'Advanced','advanced')}</div>${!state.panel?'':!d?'<p class="stack-note" role="status">Loading deployment details…</p>':`<div class="stack-details">${d.error?`<p class="stack-blocker">${escape(d.error)}</p>`:''}${state.panel==='logs'?`<pre class="stack-code">${escape(d.logs)}</pre>${textButton('Refresh logs','logs-refresh')}`:`<pre class="stack-code">${escape(d.configuration)}</pre><p class="stack-note">${escape(d.note)}</p><pre class="stack-code">${escape(d.commands)}</pre>${button('Copy commands','copy-diagnostics')}${state.status.models.length?`<div class="stack-actions">${textButton('Replica scheduling →','routing')}</div>`:''}`}</div>`}`;
}
function modelPage() {
  const id=route().id, existing=selectedModel(), pending=selectedDeployment();
  const ready=existing?.status==='ready', progress=pending?.step;
  if(!state.info && !existing && !pending) return `<div class="stack-content"><h1>${escape(id)}</h1>${state.modelError?`<p class="stack-blocker">${escape(state.modelError)}</p><div class="stack-actions">${button('Retry model check','refresh-model',true)}${textButton('Choose another model','catalogue')}</div>`:'<p role="status">Checking this model…</p>'}</div>`;
  const info=state.info || {id,estimated_gb:null};
  const request=progress?pending.request:existing && !state.editing?existing:state.request;
  const allocation=`<div class="stack-allocation"><div><strong>${allocationTitle(request)}</strong><p>${request.simulate?'Canned responses · no GPU required':hardwareLink()}</p></div>${info.estimated_gb && !request.simulate?`<span class="stack-small stack-muted">≈ ${info.estimated_gb} GB per replica</span>`:''}</div>`;
  let content='';
  if(progress) content=`${allocation}<p class="stack-note" role="status">${escape(progress)}…</p><p class="stack-note">You can leave this page while it starts.</p>${progress==='Starting GPU'?`<div class="stack-actions">${textButton('Cancel deployment','cancel-deployment')}</div>`:''}`;
  else if(pending?.action==='remove' && pending.error) content=`${allocation}<p class="stack-blocker" role="alert">${escape(pending.error)}</p><div class="stack-actions">${button('Retry removal','retry-remove',true)}</div>`;
  else if(ready && !state.editing) content=`${allocation}<div class="stack-actions">${external('litellm','Try in LiteLLM',true,'/chat/')}${external('grafana','View in Grafana')}</div><p class="stack-note">${existing.simulate?'Simulation returns canned text. ':''}Select <code>${escape(existing.name)}</code> in LiteLLM Chat. Create application keys in LiteLLM.</p><div class="stack-api"><span>API base</span><code>${escape(state.status.api_base || '')}</code>${textButton('Copy','copy-api')}</div><div class="stack-actions">${textButton('Edit deployment','edit-deployment')}${textButton('Remove deployment','remove')}</div>`;
  else {
    const replacement=state.status.mode==='compose' && state.status.models.some(m=>m.served_model!==id);
    const blocker=allocationProblem(info,state.request);
    content=`${allocation}<div data-rental>${rentalDetails()}</div>${pending?.error?`<p class="stack-blocker" role="alert">${escape(pending.error)}</p>${state.status.capacity?'<p class="stack-note">Rented GPUs remain billed until removed. <a href="#gpus">View GPUs</a></p>':''}`:''}${existing && !pending?.error?'<p class="stack-note">Waiting for model readiness and its LiteLLM connection.</p>':''}<details class="stack-settings" ${state.settingsOpen?'open':''}><summary>Settings</summary><div class="stack-fields"><label>Replicas<input name="replicas" type="number" min="1" max="8" value="${state.request.replicas}" required></label><label>GPUs per replica<input name="gpus_per_replica" type="number" min="1" max="8" value="${state.request.gpus_per_replica}" required></label><label>Context length<input name="context" type="number" min="512" max="131072" value="${state.request.context}" required></label><label class="check"><input name="simulate" type="checkbox" ${state.request.simulate?'checked':''}> Simulate without a GPU (canned responses)</label></div></details><p class="stack-blocker" data-allocation-error role="alert" ${blocker?'':'hidden'}>${escape(blocker)}</p>${!state.request.simulate && !state.status.capacity && state.status.hardware.count===0?`<p class="stack-note"><a href="${state.status.mode==='compose'?'https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html':'https://github.com/NVIDIA/k8s-device-plugin#quick-start'}" target="_blank" rel="noopener noreferrer">Set up NVIDIA GPUs ${forwardArrow}</a> · <a href="#gpus">Check GPU capacity</a></p>`:''}${info.needs_token?`<div class="stack-token"><p class="stack-note"><a href="https://huggingface.co/${escape(id)}" target="_blank" rel="noopener noreferrer">Accept model terms ${forwardArrow}</a> · <a href="https://huggingface.co/settings/tokens" target="_blank" rel="noopener noreferrer">Get a read token ${forwardArrow}</a></p><label>Hugging Face token<input type="password" name="hf_token" autocomplete="off" placeholder="hf_…"></label>${button('Connect Hugging Face','save-hf')}</div>`:''}${replacement?'<p class="stack-note">Docker runs one model at a time. Deploying this model replaces the current deployment and pauses its requests.</p>':state.editing?'<p class="stack-note">Applying changes restarts the model servers and pauses requests.</p>':''}${!state.status.permissions.deploy?'<p class="stack-note">Deployment access is unavailable. Run the generated command from your Inference Stack folder, then check readiness here.</p>':''}<div class="stack-actions">${button(deployLabel(),'deploy',true,!!blocker || busy || !state.info)}${textButton(state.editing?'Cancel':'Check again',state.editing?'cancel-edit':'refresh-model')}</div>${state.command?`<pre class="stack-code"><code>${escape(state.command)}</code></pre><div class="stack-actions">${button('Copy command','copy')}${textButton('Check deployment','refresh')}</div>`:''}<p class="stack-note"><a href="https://huggingface.co/${escape(id)}" target="_blank" rel="noopener noreferrer">Model details ${forwardArrow}</a> · <a href="https://docs.vllm.ai/en/stable/models/supported_models/" target="_blank" rel="noopener noreferrer">vLLM compatibility ${forwardArrow}</a></p>`;
  }
  if((existing || pending?.error) && !ready && !progress && pending?.action!=='remove') content+=`<div class="stack-actions">${textButton('Remove deployment','remove')}</div>`;
  if(existing || pending) content+=deploymentTools();
  if(ready && state.command) content+=`<pre class="stack-code"><code>${escape(state.command)}</code></pre>${button('Copy command','copy')}`;
  return `<div class="stack-content stack-narrow"><div class="stack-back">${textButton('← Deployments','vllm')}</div><div class="stack-heading"><div><h1 tabindex="-1">${escape(id.split('/').at(-1))}</h1><p class="stack-sub">${escape(id)}</p></div>${ready && !progress?'<span class="stack-status">Ready</span>':''}</div>${content}</div>`;
}
function routingPage() {
  const models = state.status.models;
  return `<div class="stack-content"><div class="stack-heading"><h1 tabindex="-1">Replica scheduling</h1>${external('grafana','Open Grafana')}</div>${!state.status.components.router.healthy?`<p class="stack-blocker">Can’t reach llm-d. Check the router service, then retry.</p>${button('Check again','refresh')}`:''}${models.length?`<table class="stack-table"><thead><tr><th>Model</th><th>Replicas</th><th>Scheduling</th></tr></thead><tbody>${models.map(m=>`<tr><td>${escape(m.served_model.split('/').at(-1))}</td><td>${m.ready} / ${m.replicas}</td><td>${m.router_ready?(m.replicas===1?'Single replica':'Cache-aware'):'Unavailable'}</td></tr>`).join('')}</tbody></table><p class="stack-note">LiteLLM requests are routed to your healthy vLLM replicas.</p><details class="stack-settings" ${state.routingOpen?'open':''}><summary>Settings</summary>${state.status.mode==='kubernetes'?`<div class="stack-fields"><label>Model<select name="routing_model">${models.map(m=>`<option value="${escape(m.name)}" ${state.routing?.name===m.name?'selected':''}>${escape(m.served_model)}</option>`).join('')}</select></label></div>`:''}<div id="routing-fields">${routingFields()}</div></details>`:'<div class="stack-empty"><h2>No models to route yet</h2><p>Routing is configured automatically when you deploy a model.</p><div class="stack-actions">'+button('Deploy a model','catalogue',true)+'</div></div>'}</div>`;
}
function routingFields() {
  const r = state.routing;
  if (!r) return '<p class="stack-note">Loading settings…</p>';
  if (r.error) return `<p class="stack-blocker">${escape(r.error)}</p>`;
  return `<div class="stack-fields stack-routing-fields"><label>Prefix cache weight<input name="prefix_cache" type="number" min="0" max="5" value="${r.prefix_cache}" ${state.status.permissions.deploy?'':'disabled'} required></label><label>Queue depth weight<input name="queue" type="number" min="0" max="5" value="${r.queue}" ${state.status.permissions.deploy?'':'disabled'} required></label></div><p class="stack-note">Higher weights give that signal more influence when choosing a replica. Applying changes restarts the router and may briefly pause requests.</p>${state.status.permissions.deploy?`<div class="stack-actions">${button('Save changes','save-routing',true,busy || !!state.status.deployment.step)}</div>`:'<p class="stack-note">A cluster administrator can update these weights in the model’s Helm values.</p>'}${state.status.deployment.step==='Applying routing settings'?'<p class="stack-note" role="status">Applying routing settings…</p>':''}`;
}
function render(focus = false) {
  if (!state.status) return;
  const page = route().page;
  app.innerHTML = header()+`<main class="stack-main">${page==='home'?home():page==='catalogue'?catalogue():page==='models'?modelList():page==='model'?modelPage():page==='routing'?routingPage():page==='gpus'?machinePage():page==='add-gpu'?addGpuPage():home()}</main>`;
  app.querySelector('details')?.addEventListener('toggle', e => {
    if (page==='routing') state.routingOpen=e.target.open;
    else state.settingsOpen=e.target.open;
  });
  if (focus) app.querySelector('h1')?.focus();
}
async function search(more = false) {
  searchController?.abort(); searchController = new AbortController();
  state.searching=true; state.searchError='';
  if (!more) state.results=[];
  if (route().page==='catalogue') document.querySelector('#results').innerHTML=results();
  try {
    const data = await api('/api/catalog?'+new URLSearchParams({q:state.query,cursor:more?state.cursor:''}), {signal:searchController.signal});
    state.results=more?[...state.results,...data.models]:data.models; state.cursor=data.cursor;
  } catch(e) { if(e.name==='AbortError')return; state.searchError=e.message; }
  state.searching=false;
  if(route().page==='catalogue') document.querySelector('#results').innerHTML=results();
}
async function loadModel() {
  const id = route().id;
  detailController?.abort(); detailController=new AbortController();
  state.info=null; state.modelError=''; state.command=''; state.settingsOpen=false; state.panel=''; state.diagnostics=null; state.editing=false;
  let draft; try { draft=JSON.parse(sessionStorage.getItem('deploymentDraft')); } catch {}
  const saved=selectedDeployment()?.request || selectedModel() || (draft?.model===id?draft:null);
  state.selectedGpu=saved?.instance_type || '';
  state.request={replicas:saved?.replicas||1,gpus_per_replica:saved?.gpus_per_replica||1,context:saved?.context||4096,simulate:saved?.simulate||false};
  render(true);
  try {
    state.info=await api('/api/catalog/'+encodeURIComponent(id), {signal:detailController.signal});
    if (!saved) {state.request.gpus_per_replica=state.info.suggested_gpus; state.request.context=state.info.suggested_context;}
    if(saved && !selectedModel() && !selectedDeployment()?.step && !state.status.permissions.deploy) {
      const result=await api('/api/models',{method:'POST',body:JSON.stringify({...state.request,model:id})});state.command=result.command || '';
    }
    if(needsGpu()) await loadGpuOptions();
    render();
  } catch(e) { if(e.name!=='AbortError'){state.modelError=e.message;say(e.message); render();} }
}
async function loadRouting(name) {
  name = name || state.status.models[0]?.name || '';
  try {state.routing={...await api('/api/routing?'+new URLSearchParams({name})),name};}
  catch(e) {state.routing={name,error:e.message};}
  if(route().page==='routing') render();
}
async function refresh() {
  try {
    const data = await api('/api/status');
    const changed = JSON.stringify(data)!==lastStatus;
    lastStatus=JSON.stringify(data); state.status=data;
    if(changed && !busy && !app.querySelector('input:focus,select:focus')) render();
  } catch(e) {
    say('Can’t reach the launcher. Reconnecting…');
    if(!state.status) app.innerHTML='<main class="stack-main"><p>Can’t load this installation.</p>'+button('Retry','refresh')+'</main>';
  }
}
async function navigate() {
  say();
  if (!state.status) await refresh();
  if (!state.status) return;
  const page=route().page;
  if(page==='model') await loadModel();
  else {render(true); if(page==='catalogue')search(); if(page==='add-gpu')loadGpuOptions(); if(page==='routing'){state.routing=null;loadRouting();}}
}
app.addEventListener('input', e => {
  if(e.target.matches('.stack-search')) {state.query=e.target.value;clearTimeout(searchTimer);searchTimer=setTimeout(()=>search(),250);}
  if(['replicas','gpus_per_replica','context'].includes(e.target.name)) {state.request[e.target.name]=Number(e.target.value);updateAllocation();}
  if(['prefix_cache','queue'].includes(e.target.name) && state.routing) state.routing[e.target.name]=Number(e.target.value);
});
app.addEventListener('change', e => {
  if(e.target.name==='gpu_type') {state.selectedGpu=e.target.value;suggestGpuAllocation();render();}
  if(e.target.name==='simulate') {state.request.simulate=e.target.checked;state.settingsOpen=true;render();if(needsGpu() && !state.gpuOptions)loadGpuOptions();}
  if(e.target.name==='routing_model') loadRouting(e.target.value);
});
app.addEventListener('click', async e => {
  const model=e.target.closest('[data-model]'); if(model){go('model',model.dataset.model);return;}
  const gpu=e.target.closest('[data-remove-gpu]');
  const action=gpu?'remove-gpu':e.target.closest('[data-action]')?.dataset.action; if(!action)return;
  if(['home','catalogue','models','routing','gpus','add-gpu'].includes(action)){go(action);return;}
  if(action==='vllm'){go(state.status.models.length || state.status.deployment.request?'models':'catalogue');return;}
  if(action==='search'||action==='more'){search(action==='more');return;}
  if(action==='gpu-options'){await loadGpuOptions();return;}
  if(action==='refresh'){await refresh();return;}
  if(action==='refresh-model'){await refresh();await loadModel();return;}
  if(action==='edit-deployment' || action==='cancel-edit'){state.editing=action==='edit-deployment';state.settingsOpen=state.editing;render();return;}
  if(['logs','advanced','logs-refresh'].includes(action)){
    state.panel=action==='logs-refresh'?'logs':state.panel===action?'':action;render();
    if(state.panel){try{state.diagnostics=await api('/api/models/'+encodeURIComponent(selectedModel()?.name || selectedDeployment()?.name)+'/diagnostics');}catch(error){state.diagnostics={error:error.message,logs:'',commands:'',configuration:'',note:''};}render();}
    return;
  }
  if(action==='logout'){await api('/api/logout',{method:'POST'});location.replace('/login');return;}
  if(busy)return;
  say();
  if([...app.querySelectorAll('input')].some(input=>!input.reportValidity())) return;
  busy=true;
  const clicked=e.target.closest('button'); if(clicked)clicked.disabled=true;
  try {
    if(action==='save-hf') {
      const token=app.querySelector('[name=hf_token]').value;
      await api('/api/huggingface',{method:'POST',body:JSON.stringify({token})});await loadModel();say('Hugging Face connected.');
    } else if(action==='cancel-deployment') {
      await api('/api/deployment/cancel',{method:'POST'});say('Cancelling deployment. Check GPUs to remove any rented machines.');
    } else if(action==='copy-api' || action==='copy-diagnostics') {
      await navigator.clipboard.writeText(action==='copy-api'?state.status.api_base:state.diagnostics.commands);say('Copied.');
    } else if(action==='rent-gpu' || action==='cancel-gpus') {
      const expected=state.status.capacity.desired;
      if(action==='cancel-gpus' && !confirm('Cancel the pending GPU machines?')) return;
      await api('/api/gpus',{method:'POST',body:JSON.stringify({expected,desired:action==='rent-gpu'?expected+1:0,instance_type:selectedGpu()?.instance_type || '',hourly_usd:selectedGpu()?.hourly_usd || null})});
      await refresh();go('gpus');
    } else if(action==='remove-gpu') {
      if(!confirm('Remove this GPU and its disk? Its models must be removed first.')) return;
      await api('/api/gpus/'+encodeURIComponent(gpu.dataset.removeGpu),{method:'DELETE'});
      await refresh();
    } else if(action==='deploy') {
      const body={...state.request,model:route().id,replace_existing:true,add_gpu:needsGpu(),expected_capacity:needsGpu()?state.status.capacity.desired:null,instance_type:needsGpu()?selectedGpu()?.instance_type:'',hourly_usd:needsGpu()?selectedGpu()?.hourly_usd:null};
      const result=await api('/api/models',{method:'POST',body:JSON.stringify(body)});
      state.editing=false; state.command=result.command || ''; sessionStorage.setItem('deploymentDraft',JSON.stringify(body)); await refresh();
    } else if(action==='remove' || action==='retry-remove') {
      if(action==='remove' && !confirm('Remove this deployment and stop serving requests? Any rented GPU remains billed until you remove it from GPUs.'))return;
      const result=await api('/api/models/'+encodeURIComponent(selectedModel()?.name || selectedDeployment()?.name),{method:'DELETE'});
      if(result.command){state.command=result.command;say('Run this command to remove the model. Registration is retained until removal completes.');}
      else {await refresh();go('models');}
    } else if(action==='copy') {
      await navigator.clipboard.writeText(state.command);say('Copied.');
    } else if(action==='save-routing') {
      await api('/api/routing',{method:'PUT',body:JSON.stringify(state.routing)});await refresh();
    }
  } catch(e) {say(e.message);if(action==='rent-gpu' || action==='deploy' && needsGpu()) await loadGpuOptions();}
  finally {busy=false;render();}
});
window.addEventListener('hashchange',navigate);
async function poll() {await refresh();setTimeout(poll,5000);}
(async()=>{await navigate();setTimeout(poll,5000);})();
