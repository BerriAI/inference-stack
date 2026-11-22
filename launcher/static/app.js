const app = document.querySelector('#app');
const message = document.querySelector('#message');
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {status:null, info:null, query:'', results:[], cursor:'', searching:false, searchError:'',
  modelError:'', request:{replicas:1, gpus_per_replica:1, context:4096, simulate:false}, settingsOpen:false, command:'', routing:null, routingOpen:false};
let searchTimer, searchController, detailController, busy = false, lastStatus = '';

async function api(path, options = {}) {
  const response = await fetch(path, {headers:{'Content-Type':'application/json'}, ...options});
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
function textButton(text, action) { return `<button type="button" class="stack-text-button" data-action="${action}">${text}</button>`; }
function external(key, text, primary = false) {
  const url = state.status.components[key]?.url;
  if (!url || !/^https?:\/\//.test(url)) return '';
  return `<a class="${primary?'stack-button primary':'stack-text-button'}" href="${escape(url)}" target="_blank" rel="noopener noreferrer">${text} ↗</a>`;
}
function logo(key) {
  if (key === 'litellm') return '<img class="stack-logo logo-light" src="/static/logos/litellm.svg" alt=""><img class="stack-logo logo-dark" src="/static/logos/litellmDark.svg" alt="">';
  return `<img class="stack-logo logo-${key}" src="/static/logos/${key}.${key==='grafana'?'svg':'png'}" alt="">`;
}
function hardware() {
  const h = state.status.hardware;
  if (h.count === null) return 'GPU capacity unavailable';
  if (!h.count) return 'No GPU detected';
  const types = [...new Set(h.devices.map(d => `${d.name}${d.memory_gb?' · '+d.memory_gb+' GB':''}`))];
  return `${h.count} GPU${h.count===1?'':'s'} · ${types.join(', ')}`;
}
function selectedModel() { return state.status.models.find(m => m.served_model === route().id); }
function selectedDeployment() { return state.status.deployment.request?.model === route().id ? state.status.deployment : null; }
function header() {
  const section = route().page === 'home' ? '' : route().page === 'routing' ? 'llm-d' : 'vLLM';
  return `<header class="stack-header"><button class="stack-brand" data-action="home">inference stack</button>${section?`<nav class="stack-crumb" aria-label="Breadcrumb"><span aria-hidden="true">/</span><button data-action="${section==='llm-d'?'routing':'vllm'}">${section}</button></nav>`:''}</header>`;
}
function home() {
  const s = state.status;
  const cards = [
    ['vllm','vLLM','Add and manage models','vllm',s.errors[0] || (s.deployment.error?'A deployment needs attention':s.models.some(m=>m.simulate)?'':s.hardware.count===0?'No GPU detected':s.hardware.error?'GPU check unavailable':'')],
    ['litellm','LiteLLM','Send requests. Manage keys and teams.','litellm',s.components.litellm.healthy?'':'LiteLLM is unreachable'],
    ['llmd','llm-d','Manage routing','routing',s.components.router.healthy?'':'Router is unreachable'],
    ['grafana','Grafana','Monitor the stack','grafana',s.components.grafana.healthy?'':'Grafana is unreachable']
  ];
  return `<div class="stack-home">${cards.map(([brand,title,subtitle,action,problem]) => {
    const url = s.components[action]?.url;
    const open = url && /^https?:\/\//.test(url) ? `<a href="${escape(url)}" target="_blank" rel="noopener noreferrer"` : `<button type="button" data-action="${action}"`;
    return `${open} class="stack-tile">${logo(brand)}<span class="tile-arrow" aria-hidden="true">${url?'↗':'→'}</span><div><h2>${title}</h2><p>${subtitle}</p>${problem?`<div class="tile-problem">${escape(problem)}</div>`:''}</div>${url?'</a>':'</button>'}`;
  }).join('')}</div>`;
}
function fit(model) {
  if (model.blocker) return ['Unsupported task','fit-no'];
  if (!model.estimated_gb) return ['Requirements unknown','fit-no'];
  const devices = state.status.hardware.devices;
  const memory = devices.reduce((sum,d) => sum + (d.memory_gb || 0) * d.available, 0);
  if (memory && model.estimated_gb > memory) return ['Needs more memory','fit-warn'];
  return ['Estimate · verify support','stack-muted'];
}
function results() {
  if (state.searching && !state.results.length) return '<p class="stack-note" role="status">Searching Hugging Face…</p>';
  if (state.searchError) return `<p class="stack-blocker" role="alert">${escape(state.searchError)}</p>${button('Retry search','search')}`;
  return `<div class="catalog-head"><span>Model</span><span>Estimated GPU memory</span></div>${state.results.map(m => {
    const [label,cls] = fit(m);
    const parts = m.id.split('/');
    return `<button class="catalog-item" data-model="${escape(m.id)}"><span class="catalog-name"><strong>${escape(parts.at(-1))}</strong><small>${escape(parts.length>1?parts[0]:'Hugging Face')}${m.gated?' · Access required':''}</small></span><span class="catalog-fit">${m.estimated_gb?'≈ '+m.estimated_gb+' GB':'Unknown'}<small class="${cls}">${label}</small></span></button>`;
  }).join('')}${!state.results.length?'<p class="stack-note">No models found. Try another name or a full model ID.</p>':''}${state.cursor?`<div class="stack-actions">${button(state.searching?'Loading…':'Show more','more',false,state.searching)}</div>`:''}<p class="stack-note">Estimates vary with context length, quantization and vLLM support.</p>`;
}
function catalogue() {
  return `<div class="stack-content"><div class="stack-heading"><div><h1 tabindex="-1">Add a model</h1><p class="stack-sub">${escape(hardware())}</p></div>${state.status.models.length?textButton('Your models','models'):''}</div><input class="stack-search" type="search" aria-label="Search Hugging Face models" placeholder="Search Hugging Face models" value="${escape(state.query)}" autocomplete="off"><div id="results">${results()}</div></div>`;
}
function modelList() {
  const rows = state.status.models;
  const pending = state.status.deployment;
  return `<div class="stack-content"><div class="stack-heading"><div><h1 tabindex="-1">Models</h1><p class="stack-sub">${escape(hardware())}</p></div>${button('Add model','catalogue',true)}</div>${state.status.errors.map(e=>`<p class="stack-blocker">${escape(e)}</p>`).join('')}${rows.length?`<table class="stack-table"><thead><tr><th>Model</th><th>Allocation</th><th>Status</th></tr></thead><tbody>${rows.map(m=>`<tr><td><button class="stack-model-link" data-model="${escape(m.served_model)}">${escape(m.served_model.split('/').at(-1))}</button><small>${escape(m.served_model)}</small></td><td>${m.simulate?'Simulated':m.replicas*m.gpus_per_replica+' GPUs'} · ${m.replicas} replica${m.replicas===1?'':'s'}</td><td>${m.status==='ready'?'Ready':'Starting…'}</td></tr>`).join('')}</tbody></table>`:'<p class="stack-note">No models deployed yet.</p>'}${pending.request && (pending.step || pending.error)?`<div class="stack-actions"><button class="stack-text-button" data-model="${escape(pending.request.model)}">${escape(pending.step || 'Review deployment')} →</button></div>`:''}</div>`;
}
function allocationProblem(info, request) {
  if (request.simulate) return '';
  if (info.blocker) return info.blocker;
  const h = state.status.hardware;
  if (h.count === 0) return 'No GPU detected. Check the NVIDIA driver and device plugin, or try simulation in Settings.';
  if (h.count === null && state.status.mode==='compose') return h.error;
  if (h.count !== null && request.replicas*request.gpus_per_replica > h.count) return 'This allocation needs more GPUs than this installation has.';
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
}
function modelPage() {
  const id = route().id, existing = selectedModel(), pending = selectedDeployment();
  const ready = existing?.status === 'ready';
  const progress = pending?.step;
  if (!state.info && !existing && !pending) return `<div class="stack-content"><h1>${escape(id)}</h1>${state.modelError?`<p class="stack-blocker">${escape(state.modelError)}</p><div class="stack-actions">${button('Retry model check','refresh-model',true)}${textButton('Choose another model','catalogue')}</div>`:'<p role="status">Checking this model…</p>'}</div>`;
  const info = state.info || {id, estimated_gb:null};
  const request = existing || (progress ? pending.request : state.request);
  const allocation = `<div class="stack-allocation"><div><strong>${allocationTitle(request)}</strong><p>${request.simulate?'No GPU required':escape(hardware())}</p></div>${info.estimated_gb && !request.simulate?`<span class="stack-small stack-muted">≈ ${info.estimated_gb} GB per replica</span>`:''}</div>`;
  let content = '';
  if (progress) content = `${allocation}<p class="stack-note" role="status">${escape(progress)}…</p><p class="stack-note">You can leave this page while it starts.</p>`;
  else if (pending?.action==='remove' && pending.error) content = `${allocation}<p class="stack-blocker" role="alert">${escape(pending.error)}</p><div class="stack-actions">${button('Retry removal','retry-remove',true)}</div>`;
  else if (ready) content = `${allocation}<div class="stack-actions">${external('litellm','Open LiteLLM',true)}${external('grafana','View in Grafana')}</div><p class="stack-note">${existing.simulate?'Simulation returns canned text. ':''}LiteLLM routes to this model; llm-d selects a replica.</p><div class="stack-actions">${textButton('Remove model','remove')}</div>`;
  else {
    const replacement = state.status.mode==='compose' && state.status.models.some(m=>m.served_model!==id);
    const blocker = allocationProblem(info,state.request);
    content = `${allocation}${pending?.error?`<p class="stack-blocker" role="alert">${escape(pending.error)}</p>`:''}${existing && !pending?.error?'<p class="stack-note">Waiting for model readiness and registration in LiteLLM.</p>':''}<details class="stack-settings" ${state.settingsOpen?'open':''}><summary>Settings</summary><div class="stack-fields"><label>Replicas<input name="replicas" type="number" min="1" max="8" value="${state.request.replicas}" required></label><label>GPUs per replica<input name="gpus_per_replica" type="number" min="1" max="8" value="${state.request.gpus_per_replica}" required></label><label>Context length<input name="context" type="number" min="512" max="131072" value="${state.request.context}" required></label><label class="check"><input name="simulate" type="checkbox" ${state.request.simulate?'checked':''}> Simulate without a GPU (canned responses)</label></div></details><p class="stack-blocker" data-allocation-error role="alert" ${blocker?'':'hidden'}>${escape(blocker)}</p>${info.note?`<p class="stack-note">${escape(info.note)} <a href="https://docs.vllm.ai/en/stable/models/supported_models/" target="_blank" rel="noopener noreferrer">vLLM compatibility ↗</a></p>`:''}${info.gated?`<p class="stack-note"><a href="https://huggingface.co/${escape(id)}" target="_blank" rel="noopener noreferrer">Model access on Hugging Face ↗</a></p>`:''}${replacement?'<p class="stack-note">Deploying this model replaces the running model. Requests pause while it starts.</p>':''}${!state.status.permissions.deploy?'<p class="stack-note">This launcher cannot deploy directly. Run the generated command from your repository checkout, then check readiness here.</p>':''}<div class="stack-actions">${button(state.status.permissions.deploy?(replacement?'Replace model':pending?.error?'Retry deployment':'Deploy'):'Get deployment command','deploy',true,!!blocker || busy || !state.info)}${textButton('Check again','refresh-model')}</div>${state.command?`<pre class="stack-code"><code>${escape(state.command)}</code></pre><div class="stack-actions">${button('Copy command','copy')}${textButton('Check deployment','refresh')}</div>`:''}`;
  }
  if(existing && !ready && !progress && pending?.action!=='remove') content+=`<div class="stack-actions">${textButton('Remove model','remove')}</div>`;
  if(ready && state.command) content+=`<pre class="stack-code"><code>${escape(state.command)}</code></pre><div class="stack-actions">${button('Copy command','copy')}</div>`;
  return `<div class="stack-content stack-narrow"><div class="stack-back">${textButton('← Models','vllm')}</div><div class="stack-heading"><div><h1 tabindex="-1">${escape(id.split('/').at(-1))}</h1><p class="stack-sub">${escape(id)}</p></div>${ready && !progress?'<span class="stack-status">Ready</span>':''}</div>${content}</div>`;
}
function routingPage() {
  const models = state.status.models;
  return `<div class="stack-content"><div class="stack-heading"><h1 tabindex="-1">Routing</h1>${external('grafana','Open Grafana')}</div>${!state.status.components.router.healthy?`<p class="stack-blocker">Can’t reach llm-d. Check the router service, then retry.</p>${button('Check again','refresh')}`:''}${models.length?`<table class="stack-table"><thead><tr><th>Model</th><th>Replicas</th><th>Routing</th></tr></thead><tbody>${models.map(m=>`<tr><td>${escape(m.served_model.split('/').at(-1))}</td><td>${m.ready} / ${m.replicas}</td><td>${m.router_ready?(m.replicas===1?'Single replica':'Cache-aware'):'Unavailable'}</td></tr>`).join('')}</tbody></table><p class="stack-note">LiteLLM requests are routed to your healthy vLLM replicas.</p><details class="stack-settings" ${state.routingOpen?'open':''}><summary>Settings</summary>${state.status.mode==='kubernetes'?`<div class="stack-fields"><label>Model<select name="routing_model">${models.map(m=>`<option value="${escape(m.name)}" ${state.routing?.name===m.name?'selected':''}>${escape(m.served_model)}</option>`).join('')}</select></label></div>`:''}<div id="routing-fields">${routingFields()}</div></details>`:'<div class="stack-empty"><h2>No models to route yet</h2><p>Routing is configured automatically when you deploy a model.</p><div class="stack-actions">'+button('Add a model','catalogue',true)+'</div></div>'}</div>`;
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
  app.innerHTML = header()+`<main class="stack-main">${page==='home'?home():page==='catalogue'?catalogue():page==='models'?modelList():page==='model'?modelPage():page==='routing'?routingPage():home()}</main>`;
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
  state.info=null; state.modelError=''; state.command=''; state.settingsOpen=false;
  let draft; try { draft=JSON.parse(sessionStorage.getItem('deploymentDraft')); } catch {}
  const saved=selectedDeployment()?.request || selectedModel() || (draft?.model===id?draft:null);
  state.request={replicas:saved?.replicas||1,gpus_per_replica:saved?.gpus_per_replica||1,context:saved?.context||4096,simulate:saved?.simulate||false};
  render(true);
  try {
    state.info=await api('/api/catalog/'+encodeURIComponent(id), {signal:detailController.signal});
    if (!saved) {state.request.gpus_per_replica=state.info.suggested_gpus; state.request.context=state.info.suggested_context;}
    if(saved && !selectedModel() && !selectedDeployment()?.step && !state.status.permissions.deploy) {
      const result=await api('/api/models',{method:'POST',body:JSON.stringify({...state.request,model:id})});state.command=result.command || '';
    }
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
    if(changed && !app.querySelector('input:focus,select:focus')) render();
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
  else {render(true); if(page==='catalogue')search(); if(page==='routing'){state.routing=null;loadRouting();}}
}
app.addEventListener('input', e => {
  if(e.target.matches('.stack-search')) {state.query=e.target.value;clearTimeout(searchTimer);searchTimer=setTimeout(()=>search(),250);}
  if(['replicas','gpus_per_replica','context'].includes(e.target.name)) {state.request[e.target.name]=Number(e.target.value);updateAllocation();}
  if(['prefix_cache','queue'].includes(e.target.name) && state.routing) state.routing[e.target.name]=Number(e.target.value);
});
app.addEventListener('change', e => {
  if(e.target.name==='simulate') {state.request.simulate=e.target.checked;state.settingsOpen=true;render();}
  if(e.target.name==='routing_model') loadRouting(e.target.value);
});
app.addEventListener('click', async e => {
  const model=e.target.closest('[data-model]'); if(model){go('model',model.dataset.model);return;}
  const action=e.target.closest('[data-action]')?.dataset.action; if(!action)return;
  if(['home','catalogue','models','routing'].includes(action)){go(action);return;}
  if(action==='vllm'){go(state.status.models.length || state.status.deployment.request?'models':'catalogue');return;}
  if(action==='search'||action==='more'){search(action==='more');return;}
  if(action==='refresh'){await refresh();return;}
  if(action==='refresh-model'){await refresh();await loadModel();return;}
  if(busy)return;
  say();
  if([...app.querySelectorAll('input')].some(input=>!input.reportValidity())) return;
  busy=true;
  const clicked=e.target.closest('button'); if(clicked)clicked.disabled=true;
  try {
    if(action==='deploy') {
      const body={...state.request,model:route().id,replace_existing:true};
      const result=await api('/api/models',{method:'POST',body:JSON.stringify(body)});
      state.command=result.command || ''; sessionStorage.setItem('deploymentDraft',JSON.stringify(body)); await refresh();
    } else if(action==='remove' || action==='retry-remove') {
      if(action==='remove' && !confirm('Remove this model and stop serving requests to it?'))return;
      const result=await api('/api/models/'+encodeURIComponent(selectedModel()?.name || selectedDeployment()?.name),{method:'DELETE'});
      if(result.command){state.command=result.command;say('Run this command to remove the model. Registration is retained until removal completes.');}
      else {await refresh();go('models');}
    } else if(action==='copy') {
      await navigator.clipboard.writeText(state.command);say('Copied.');
    } else if(action==='save-routing') {
      await api('/api/routing',{method:'PUT',body:JSON.stringify(state.routing)});await refresh();
    }
  } catch(e) {say(e.message);}
  finally {busy=false;render();}
});
window.addEventListener('hashchange',navigate);
async function poll() {await refresh();setTimeout(poll,5000);}
(async()=>{await navigate();setTimeout(poll,5000);})();
