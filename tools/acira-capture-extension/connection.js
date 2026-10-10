"use strict";
const $ = (id) => document.getElementById(id);
let projects = [];
function option(value, label) {
  const node = document.createElement("option");
  node.value = value;
  node.textContent = label;
  return node;
}
function agents() {
  const project = projects.find((p) => p.id === $("project").value);
  $("agent").replaceChildren(
    ...(project?.agents || []).map((name) => option(name, name)),
  );
}
async function load() {
  const result = await chrome.runtime.sendMessage({
    type: "acira.destinations",
  });
  if (result.error) throw new Error(result.error);
  projects = result.projects;
  $("project").replaceChildren(...projects.map((p) => option(p.id, p.name)));
  agents();
  const { aciraConnection } = await chrome.storage.local.get("aciraConnection");
  if (aciraConnection) {
    $("url").value = aciraConnection.url;
    if (aciraConnection.project_id) {
      $("project").value = aciraConnection.project_id;
      agents();
      $("agent").value = aciraConnection.agent;
    }
  }
}
$("project").onchange = agents;
$("pair").onsubmit = async (event) => {
  event.preventDefault();
  try {
    const url = new URL($("url").value);
    if (
      url.protocol !== "http:" ||
      url.hostname !== "127.0.0.1" ||
      url.pathname !== "/" ||
      url.username ||
      url.password
    )
      throw new Error("Use the exact http://127.0.0.1 address shown by Acira.");
    const allowed = await chrome.permissions.request({
      origins: ["http://127.0.0.1/*"],
    });
    if (!allowed)
      throw new Error(
        "Local connection permission was declined. Offline export is still available.",
      );
    const response = await fetch(url.origin + "/api/pair", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: $("code").value.trim() }),
      redirect: "error",
    });
    const raw = await response.text();
    let data;
    try {
      data = JSON.parse(raw);
    } catch {
      data = { error: raw || "Local service did not respond" };
    }
    if (!response.ok)
      throw new Error(data.error || "Pairing failed. Generate a new code.");
    await chrome.storage.local.set({
      aciraConnection: {
        url: url.origin,
        credential: data.credential,
        proof_key: data.proof_key,
        extension_id: data.extension_id,
      },
    });
    $("code").value = "";
    await load();
    $("status").textContent =
      "Paired. Select the project and agent for captures.";
  } catch (error) {
    $("status").textContent = error.message;
  }
};
$("save").onclick = async () => {
  const { aciraConnection } = await chrome.storage.local.get("aciraConnection");
  if (!aciraConnection) return;
  const project = projects.find((p) => p.id === $("project").value);
  if (!project || !$("agent").value) return;
  await chrome.storage.local.set({
    aciraConnection: {
      ...aciraConnection,
      project_id: project.id,
      project_name: project.name,
      agent: $("agent").value,
    },
  });
  $("status").textContent =
    "Captures will save to " + project.name + " / " + $("agent").value;
};
$("disconnect").onclick = async () => {
  await chrome.storage.local.remove("aciraConnection");
  $("status").textContent =
    "Disconnected. You can revoke this credential in dashboard Settings.";
};
load().catch((error) => {
  $("status").textContent = error.message;
});
