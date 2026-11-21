const $ = (selector) => document.querySelector(selector);
let status = { models: [] };

async function api(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || response.statusText);
  return data;
}

function show(element, text) {
  element.hidden = false;
  element.textContent = text;
}

function tile({ name, healthy, url, detail }) {
  const tag = url ? "a" : "div";
  const link = url ? `href="${url}" target="_blank"` : "";
  return `<${tag} class="tile" ${link}>
    <span class="dot ${healthy ? "healthy" : ""}"></span>${name}<small>${detail}</small>
  </${tag}>`;
}

function render() {
  const servers = status.model_servers;
  const tiles = status.components.map((c) => ({ ...c, detail: c.url ? "Open" : c.healthy ? "Routing" : "Down" }));
  if (status.mode === "compose") {
    tiles.splice(-1, 0, {
      name: "vLLM",
      healthy: Boolean(servers) && servers.ready === servers.replicas,
      detail: servers ? `${servers.ready}/${servers.replicas} replicas ready` : "No model yet",
    });
  }
  $("#components").innerHTML = tiles.map(tile).join("");

  $("#models").innerHTML = status.models.length
    ? status.models.map((m) => `<div class="model"><span><b>${m.name}</b> (${m.served_model})</span>
        <button class="remove" data-name="${m.name}">Remove</button></div>`).join("")
    : `<p class="note">No models yet. Add one below.</p>`;

  const { step, error } = status.deployment;
  $("#deployment").textContent = error || step;
  $("#deployment").className = error ? "error" : "note";

  if (!$("#catalog").options.length) {
    $("#catalog").innerHTML = status.catalog
      .map((m) => `<option value="${m.id}">${m.id} (${m.vram_gb} GB GPU)</option>`).join("");
    $("#add-model [name=simulate]").checked = !status.gpu;
  }
}

async function refresh() {
  status = await api("GET", "/api/status");
  render();
}

$("#add-model").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const result = await api("POST", "/api/models", {
    model: form.get("model"),
    replicas: Number(form.get("replicas")),
    simulate: form.get("simulate") === "on",
  });
  if (result.command) show($("#command"), `Run this in your cluster, then the model appears here:\n\n${result.command}`);
  refresh();
});

$("#models").addEventListener("click", async (event) => {
  if (!event.target.dataset.name) return;
  const result = await api("DELETE", `/api/models/${event.target.dataset.name}`);
  if (result.command) show($("#command"), result.command);
  refresh();
});

$("#chat").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!status.models.length) return show($("#reply"), "Add a model first.");
  show($("#reply"), "...");
  try {
    const message = new FormData(event.target).get("message");
    const { reply } = await api("POST", "/api/chat", { model: status.models[0].name, message });
    show($("#reply"), reply);
  } catch (error) {
    show($("#reply"), error.message);
  }
});

$("#create-key").addEventListener("click", async () => {
  const { key } = await api("POST", "/api/keys");
  const model = status.models[0]?.name ?? "<model>";
  show($("#snippet"), `curl ${status.litellm_public_url}/v1/chat/completions \\
  -H "Authorization: Bearer ${key}" \\
  -H "Content-Type: application/json" \\
  -d '{"model": "${model}", "messages": [{"role": "user", "content": "Hello"}]}'`);
});

refresh();
setInterval(refresh, 3000);
