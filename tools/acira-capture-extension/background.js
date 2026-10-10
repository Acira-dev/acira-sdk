chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.id || !isInjectableUrl(tab.url || "")) return;

  try {
    await chrome.scripting.insertCSS({
      target: { tabId: tab.id },
      files: ["capture.css"],
    });
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: ["content.js"],
    });
  } catch (_error) {
    // Restricted browser pages such as chrome:// cannot host content scripts.
  }
});

function isInjectableUrl(url) {
  return /^(https?|file):/i.test(url);
}

// Credentials remain in the extension worker, never in page/content-script DOM.
async function aciraRequest(path, body) {
  const { aciraConnection: connection } =
    await chrome.storage.local.get("aciraConnection");
  if (!connection)
    throw new Error("Pair the extension in its connection settings first.");
  const url = new URL(connection.url);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1")
    throw new Error("Invalid local connection.");
  const nonce = Array.from(crypto.getRandomValues(new Uint8Array(24)), (b) =>
    b.toString(16).padStart(2, "0"),
  ).join("");
  const proofResponse = await fetch(
    url.origin +
      "/handshake?nonce=" +
      nonce +
      "&extension=" +
      encodeURIComponent(connection.extension_id),
    { redirect: "error", signal: AbortSignal.timeout(3000) },
  );
  const proof = await proofResponse.json();
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(connection.proof_key),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = new Uint8Array(
    await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(nonce)),
  );
  const expected = Array.from(signature, (b) =>
    b.toString(16).padStart(2, "0"),
  ).join("");
  if (proof.proof !== expected)
    throw new Error(
      "The local server could not prove its identity. Reconnect from Acira Settings.",
    );
  const response = await fetch(url.origin + path, {
    method: body ? "POST" : "GET",
    headers: {
      Authorization: "Bearer " + connection.credential,
      "Content-Type": "application/json",
    },
    body: body ? JSON.stringify(body) : undefined,
    redirect: "error",
    signal: AbortSignal.timeout(5000),
  });
  const raw = await response.text();
  let result;
  try {
    result = JSON.parse(raw);
  } catch {
    result = { error: raw || "Local dashboard request failed" };
  }
  if (!response.ok)
    throw new Error(result.error || "Local dashboard request failed.");
  return result;
}
chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (
    sender.id !== chrome.runtime.id ||
    !String(message.type || "").startsWith("acira.")
  )
    return false;
  (async () => {
    if (message.type === "acira.settings") {
      await chrome.runtime.openOptionsPage();
      return { opened: true };
    }
    if (message.type === "acira.destinations")
      return await aciraRequest("/api/destinations");
    const { aciraConnection: connection } =
      await chrome.storage.local.get("aciraConnection");
    if (message.type === "acira.destination") {
      const destinations = connection?.project_id
        ? await aciraRequest("/api/destinations")
        : null;
      const project = destinations?.projects.find(
        (project) => project.id === connection.project_id,
      );
      const affected = project?.capture_scopes?.[connection.agent] || [];
      const label = project
        ? project.name +
          " / " +
          connection.agent +
          (affected.length > 1
            ? " (shared with " +
              affected
                .filter((agent) => agent !== connection.agent)
                .join(", ") +
              ")"
            : "")
        : null;
      return {
        destination: label,
        project_id: connection?.project_id,
        agent: connection?.agent,
      };
    }
    if (message.type === "acira.capture") {
      if (!connection?.project_id || !connection.agent)
        throw new Error(
          "Select a destination in the extension connection settings.",
        );
      if (
        message.project_id !== connection.project_id ||
        message.agent !== connection.agent
      )
        throw new Error(
          "The capture destination changed. Review the destination and save again.",
        );
      return await aciraRequest("/api/captures", {
        project_id: connection.project_id,
        agent: connection.agent,
        document: message.document,
      });
    }
    throw new Error("Unknown Acira request.");
  })()
    .then(respond)
    .catch((error) => respond({ error: error.message }));
  return true;
});
