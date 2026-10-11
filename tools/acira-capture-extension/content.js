(() => {
  const ROOT_ID = "acira-capture-panel";
  const VERSION = "0.6.5";
  const STORAGE_PREFIX = "acira.capture.draft";
  const EXISTING = window.__ACIRA_CAPTURE__;
  if (EXISTING) {
    if (EXISTING.version === VERSION) {
      EXISTING.toggle();
      return;
    }
    try {
      if (typeof EXISTING.destroy === "function") EXISTING.destroy();
      else if (typeof EXISTING.close === "function") EXISTING.close();
    } catch (_error) {
      // Continue with a clean injection below.
    }
    try {
      delete window.__ACIRA_CAPTURE__;
    } catch (_error) {
      window.__ACIRA_CAPTURE__ = null;
    }
  }

  document
    .querySelectorAll(`#${ROOT_ID}, .acira-capture-shield, .acira-capture-hover, .acira-capture-selected, .acira-capture-label`)
    .forEach((node) => node.remove());

  const SCHEMA = "acira.specific_skill.v2";
  const LOGO_URL =
    typeof chrome !== "undefined" && chrome.runtime?.getURL
      ? chrome.runtime.getURL("assets/acira-logo.svg")
      : "";
  const ICONS = {
    mark: "<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M12 3.5l7 3v5.2c0 4.4-2.9 7.5-7 8.8-4.1-1.3-7-4.4-7-8.8V6.5l7-3z'/><path d='M12 8v5'/><path d='M12 16h.01'/></svg>",
    cursor: "<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M5 3l14 7-6.4 1.9-2.9 6.1L5 3z'/></svg>",
    pause: "<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M8 5v14'/><path d='M16 5v14'/></svg>",
    clear: "<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M4 7h16'/><path d='M10 11v6'/><path d='M14 11v6'/><path d='M6 7l1 13h10l1-13'/><path d='M9 7V4h6v3'/></svg>",
    close: "<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M6 6l12 12'/><path d='M18 6L6 18'/></svg>",
    block: "<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M12 3.5l7 3v5.2c0 4.4-2.9 7.5-7 8.8-4.1-1.3-7-4.4-7-8.8V6.5l7-3z'/><path d='M8.8 12h6.4'/></svg>",
    ask: "<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M12 20a8 8 0 1 0 0-16 8 8 0 0 0 0 16z'/><path d='M9.8 9a2.4 2.4 0 0 1 4.7.8c0 1.9-2.5 1.8-2.5 3.6'/><path d='M12 16.5h.01'/></svg>",
    copy: "<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M8 8h10v12H8z'/><path d='M6 16H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1'/></svg>",
    download: "<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M12 4v10'/><path d='M8 10l4 4 4-4'/><path d='M5 20h14'/></svg>",
    code: "<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M8 9l-4 3 4 3'/><path d='M16 9l4 3-4 3'/><path d='M14 5l-4 14'/></svg>",
    list: "<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M8 6h12'/><path d='M8 12h12'/><path d='M8 18h12'/><path d='M4 6h.01'/><path d='M4 12h.01'/><path d='M4 18h.01'/></svg>"
  };
  const INTERACTIVE_SELECTOR = [
    "button",
    "a[href]",
    "input",
    "label",
    "select",
    "textarea",
    "[role='button']",
    "[role='link']",
    "[role='menuitem']",
    "[role='menuitemcheckbox']",
    "[role='menuitemradio']",
    "[role='option']",
    "[role='checkbox']",
    "[role='radio']",
    "[role='switch']",
    "[role='combobox']",
    "[role='listbox']",
    "[role='option']",
    "[role='slider']",
    "[role='spinbutton']",
    "[role='tab']",
    "[role='textbox']",
    "[contenteditable='true']"
  ].join(",");

  const state = {
    visible: false,
    capturing: false,
    restored: false,
    pendingElement: null,
    pendingFingerprint: null,
    targets: [],
    policyId: null,
    scopePatterns: null,
    storageKey: null,
    pendingPage: null,
    pendingPatterns: null,
    selectionReady: false,
    selectionTimer: null
  };

  const panel = document.createElement("section");
  panel.id = ROOT_ID;
  panel.hidden = true;
  panel.setAttribute("aria-label", "Acira Capture");
  panel.innerHTML = [
    "<header class='acira-capture-header'>",
    "  <div class='acira-brand'>",
    `    <span class='acira-brand-mark'>${LOGO_URL ? `<img src='${LOGO_URL}' alt='' />` : icon("mark")}</span>`,
    "    <div>",
    "      <div class='acira-capture-kicker'>Acira</div>",
    "      <div class='acira-capture-title'>Policy Capture</div>",
    "    </div>",
    "  </div>",
    "  <div class='acira-capture-header-actions'>",
    "    <span id='acira-capture-pill' class='acira-capture-pill'>Paused</span>",
    `    <button class='acira-icon-button' data-action='close' aria-label='Close Acira Capture' title='Close'>${icon("close")}</button>`,
    "  </div>",
    "</header>",
    "<div id='acira-capture-guide' class='acira-capture-guide'>Hover to preview. Click to select.</div>",
    "<div class='acira-capture-toolbar'>",
    `  <button class='acira-primary-button' data-action='toggle'>${icon("cursor")}<span>Start capture</span></button>`,
    `  <button data-action='clear'>${icon("clear")}<span>Clear</span></button>`,
    "</div>",
    "<section class='acira-capture-card'>",
    "  <div class='acira-section-label'>Selection</div>",
    "  <div id='acira-capture-selected-summary' class='acira-empty-state'>No target selected</div>",
    "  <div class='acira-capture-actions'>",
    `    <button data-action='mark' data-kind='block' disabled>${icon("block")}<span>Block target</span></button>`,
    `    <button data-action='mark' data-kind='ask' disabled>${icon("ask")}<span>Ask first</span></button>`,
    "  </div>",
    "</section>",
    "<section class='acira-capture-card'>",
    `  <div class='acira-section-label'>${icon("list")}<span>Targets</span></div>`,
    "  <div id='acira-capture-targets' class='acira-target-list'></div>",
    "</section>",
    "<details class='acira-capture-details'>",
    `  <summary>${icon("code")}<span>Raw fingerprint</span></summary>`,
    "  <pre id='acira-capture-facts'></pre>",
    "</details>",
    "<footer class='acira-capture-footer'>",
    `  <button data-action='copy'>${icon("copy")}<span>Copy JSON</span></button>`,
    `  <button data-action='download'>${icon("download")}<span>Download</span></button>`,
    "  <button data-action='dashboard'>Save to dashboard</button>",
    "  <button data-action='connection'>Connection settings</button>",
    "</footer>",
    "<div id='acira-capture-status' class='acira-capture-status' role='status'></div>"
  ].join("");

  const hoverBox = document.createElement("div");
  hoverBox.className = "acira-capture-hover";
  hoverBox.hidden = true;

  const selectedBox = document.createElement("div");
  selectedBox.className = "acira-capture-selected";
  selectedBox.hidden = true;

  const hoverLabel = document.createElement("div");
  hoverLabel.className = "acira-capture-label";
  hoverLabel.hidden = true;

  const selectedLabel = document.createElement("div");
  selectedLabel.className = "acira-capture-label acira-capture-label-selected";
  selectedLabel.hidden = true;

  // Native input inside a separate extension document never reaches the page's
  // earlier window listeners. The bridge sends coordinates, not page content.
  const shield = document.createElement("iframe");
  shield.className = "acira-capture-shield";
  shield.title = "Acira target selection";
  shield.src = chrome.runtime.getURL("selection.html");
  shield.hidden = true;
  const shieldOrigin = "chrome-extension://" + chrome.runtime.id;

  document.documentElement.append(shield, panel, hoverBox, selectedBox, hoverLabel, selectedLabel);

  const pill = panel.querySelector("#acira-capture-pill");
  const status = panel.querySelector("#acira-capture-status");
  const facts = panel.querySelector("#acira-capture-facts");
  const targetList = panel.querySelector("#acira-capture-targets");
  const selectedSummary = panel.querySelector("#acira-capture-selected-summary");
  const toggleButton = panel.querySelector("[data-action='toggle']");
  const markButtons = Array.from(panel.querySelectorAll("[data-action='mark']"));

  panel.addEventListener("click", handlePanelClick);
  document.addEventListener("mousemove", handleMouseMove, true);
  window.addEventListener("pointerdown", suppressCaptureActivation, true);
  window.addEventListener("mousedown", suppressCaptureActivation, true);
  window.addEventListener("click", handleDocumentClick, true);
  document.addEventListener("keydown", handleKeyDown, true);
  window.addEventListener("scroll", refreshOverlays, true);
  window.addEventListener("resize", refreshOverlays, true);
  window.addEventListener("message", handleSelectionMessage);

  const api = { version: VERSION, open, close, toggle, destroy };
  window.__ACIRA_CAPTURE__ = api;

  if (typeof chrome !== "undefined" && chrome.runtime?.onMessage) {
    chrome.runtime.onMessage.addListener(handleRuntimeMessage);
  }

  open();

  function open() {
    state.visible = true;
    panel.hidden = false;
    setCapturing(true);
    restoreDraft();
    render();
  }

  function close() {
    destroy();
  }

  function destroy() {
    state.visible = false;
    state.capturing = false;
    panel.removeEventListener("click", handlePanelClick);
    document.removeEventListener("mousemove", handleMouseMove, true);
    window.removeEventListener("pointerdown", suppressCaptureActivation, true);
    window.removeEventListener("mousedown", suppressCaptureActivation, true);
    window.removeEventListener("click", handleDocumentClick, true);
    document.removeEventListener("keydown", handleKeyDown, true);
    window.removeEventListener("scroll", refreshOverlays, true);
    window.removeEventListener("resize", refreshOverlays, true);
    window.removeEventListener("message", handleSelectionMessage);
    if (typeof chrome !== "undefined" && chrome.runtime?.onMessage) {
      chrome.runtime.onMessage.removeListener(handleRuntimeMessage);
    }
    window.clearTimeout(setStatus.timer);
    window.clearTimeout(state.selectionTimer);
    panel.remove();
    shield.remove();
    hoverBox.remove();
    selectedBox.remove();
    hoverLabel.remove();
    selectedLabel.remove();
    if (window.__ACIRA_CAPTURE__ === api) {
      try {
        delete window.__ACIRA_CAPTURE__;
      } catch (_error) {
        window.__ACIRA_CAPTURE__ = null;
      }
    }
  }

  function toggle() {
    if (state.visible) destroy();
    else open();
  }

  function setCapturing(capturing) {
    state.capturing = Boolean(capturing);
    shield.hidden = !state.capturing;
    if (state.capturing) shield.focus();
    window.clearTimeout(state.selectionTimer);
    if (state.capturing && !state.selectionReady) {
      setStatus("Preparing target selection…");
      state.selectionTimer = window.setTimeout(() => {
        if (!state.selectionReady && state.visible && state.capturing) {
          setCapturing(false);
          setStatus("Capture couldn’t start safely on this page. Selection is paused; you can still export previous targets.");
        }
      }, 5000);
    }
    if (!state.capturing) hideHover();
    if (state.visible && !panel.isConnected) {
      document.documentElement.append(shield, panel, hoverBox, selectedBox, hoverLabel, selectedLabel);
    }
    render();
  }

  function handlePanelClick(event) {
    if (!event.isTrusted) return;
    const button = event.target?.closest?.("button");
    if (!button) return;
    const action = button.getAttribute("data-action");
    if (action === "toggle") {
      setCapturing(!state.capturing);
    } else if (action === "close") {
      close();
    } else if (action === "mark") {
      markPending(button.getAttribute("data-kind"));
    } else if (action === "copy") {
      copySkill();
    } else if (action === "download") {
      downloadSkill();
    } else if (action === "connection") {
      chrome.runtime.sendMessage({type:"acira.settings"});
    } else if (action === "dashboard") {
      saveToDashboard();
    } else if (action === "clear") {
      clearTargets();
    } else if (action === "remove") {
      removeTarget(Number(button.getAttribute("data-index")));
    }
  }

  function handleMouseMove(event) {
    if (!state.visible || !state.capturing || isAciraNode(event.target)) return;
    const element = interactiveElement(event.target);
    if (!element) {
      hideHover();
      return;
    }
    placeOverlay(hoverBox, hoverLabel, overlayElementFor(element), labelForElement(element));
  }

  function handleSelectionMessage(event) {
    if (!event.isTrusted || event.source !== shield.contentWindow || event.origin !== shieldOrigin
        || !state.visible || event.data?.type !== "acira.selection") return;
    const {kind, x, y, dx, dy} = event.data;
    if (kind === "ready") { state.selectionReady = true; shield.dataset.ready = "true"; window.clearTimeout(state.selectionTimer); if (state.capturing) setStatus("Click a control to select it without activating it."); return; }
    if (!state.capturing) return;
    if (kind === "escape") { setCapturing(false); toggleButton.focus(); return; }
    if (!["move", "select", "scroll"].includes(kind) || !Number.isFinite(x) || !Number.isFinite(y)
        || x < 0 || y < 0 || x > innerWidth || y > innerHeight) return;
    const node = document.elementsFromPoint(x, y).find(item => !isAciraNode(item));
    if (kind === "scroll") {
      if (!Number.isFinite(dx) || !Number.isFinite(dy) || Math.abs(dx) > innerWidth * 4 || Math.abs(dy) > innerHeight * 4) return;
      let scrollable = node;
      while (scrollable && scrollable !== document.documentElement) {
        const css = getComputedStyle(scrollable);
        if ((/(auto|scroll)/.test(css.overflowY) && scrollable.scrollHeight > scrollable.clientHeight)
            || (/(auto|scroll)/.test(css.overflowX) && scrollable.scrollWidth > scrollable.clientWidth)) break;
        scrollable = scrollable.parentElement;
      }
      (scrollable || window).scrollBy(dx, dy);
      return;
    }
    const element = interactiveElement(node);
    if (!element) { hideHover(); return; }
    if (kind === "select") selectElement(element);
    else placeOverlay(hoverBox, hoverLabel, overlayElementFor(element), labelForElement(element));
  }

  function suppressCaptureActivation(event) {
    if (!state.visible || !state.capturing || isAciraNode(event.target) || !interactiveElement(event.target)) return;
    // Native selects and applications can act on pointerdown/mousedown before
    // click. Selecting a capture target must suppress those target handlers too.
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
  }

  function handleDocumentClick(event) {
    if (!state.visible || !state.capturing || isAciraNode(event.target)) return;
    const element = interactiveElement(event.target);
    if (!element) return;
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
    selectElement(element);
  }

  function handleKeyDown(event) {
    if (!state.visible || event.key !== "Escape") return;
    if (state.capturing) {
      setCapturing(false);
    } else {
      close();
    }
  }

  function handleRuntimeMessage(message) {
    if (!message || message.type !== "ACIRA_CAPTURE_TOGGLE") return false;
    toggle();
    return false;
  }

  function selectElement(element) {
    if (state.targets.length && state.targets[0].capture?.url !== normalizedPageUrl()) {
      setStatus("Page changed. Download this capture or Clear before selecting targets on another page.");
      return;
    }
    state.pendingElement = element;
    state.pendingPage = normalizedPageUrl();
    state.pendingPatterns = urlPatternsForCurrentPage();
    state.pendingFingerprint = collectFingerprint(element);
    placeOverlay(selectedBox, selectedLabel, overlayElementFor(element), labelForElement(element));
    facts.textContent = JSON.stringify(state.pendingFingerprint, null, 2);
    setStatus("Target selected");
    render();
  }

  function markPending(decision) {
    if (!state.pendingFingerprint || !["block", "ask"].includes(decision)) {
      setStatus("Select a target first");
      return;
    }
    if (state.pendingPage !== normalizedPageUrl()) {
      setStatus("Page changed since selection. Select the target again before adding it.");
      return;
    }
    if (!state.targets.length) {
      state.policyId = newPolicyId();
      state.scopePatterns = state.pendingPatterns;
      state.storageKey = pageStorageKey();
    }
    const targetId = `target-${state.targets.length + 1}-${slug(
      state.pendingFingerprint.text || state.pendingFingerprint.id || state.pendingFingerprint.tag || "element"
    )}`;
    state.targets.push({
      id: targetId,
      decision,
      reason: `${decision === "block" ? "Block" : "Ask before"} activation of captured target.`,
      threshold: 70,
      fingerprint: state.pendingFingerprint,
      capture: {
        url: state.pendingPage,
        title: document.title,
        captured_at: new Date().toISOString(),
        tool: "acira-capture-webextension"
      }
    });
    persistDraft();
    setStatus(`${decision === "block" ? "Block" : "Ask"} target added`);
    render();
  }

  function removeTarget(index) {
    if (!Number.isInteger(index) || index < 0 || index >= state.targets.length) return;
    state.targets.splice(index, 1);
    persistDraft();
    setStatus("Target removed");
    render();
  }

  function clearTargets() {
    state.targets = [];
    state.pendingElement = null;
    state.pendingFingerprint = null;
    state.pendingPage = null;
    state.pendingPatterns = null;
    state.scopePatterns = null;
    facts.textContent = "";
    hideSelected();
    persistDraft();
    setStatus("Cleared");
    render();
  }

  function buildSkill() {
    return sanitizeCapture({
      schema: SCHEMA,
      id: state.policyId || (state.policyId = newPolicyId()),
      name: `Acira captured targets for ${document.title || location.host || "page"}`,
      version: 2,
      semantic_profiles: ["acira.web_business_controls.v1"],
      scope: {
        url_patterns: state.scopePatterns || urlPatternsForCurrentPage()
      },
      targets: state.targets
    });
  }

  function newPolicyId() {
    // getRandomValues is also available on ordinary HTTP pages, where Chrome
    // does not expose randomUUID. This ID routes captures; it is not a token.
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return "skill-" + Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
  }

  async function restoreDraft() {
    if (state.restored) return;
    state.restored = true;
    const draft = await readDraft();
    if (!draft || !Array.isArray(draft.targets)) return;
    state.targets = sanitizeCapture(draft.targets.filter(isPersistedTarget));
    state.storageKey = pageStorageKey();
    state.scopePatterns = Array.isArray(draft.url_patterns) && draft.url_patterns.every(pattern => typeof pattern === "string")
      ? draft.url_patterns : urlPatternsForCurrentPage();
    state.policyId = typeof draft.policy_id === "string" && /^[A-Za-z0-9_.-]{1,128}$/.test(draft.policy_id)
      ? draft.policy_id : newPolicyId();
    if (state.targets.length) {
      await persistDraft();
      setStatus(`Restored ${state.targets.length} captured target${state.targets.length === 1 ? "" : "s"}`);
      render();
    }
  }

  async function persistDraft() {
    const draft = {
      schema: SCHEMA,
      policy_id: state.policyId,
      version: 2,
      semantic_profiles: ["acira.web_business_controls.v1"],
      page_key: state.storageKey || pageStorageKey(),
      url_patterns: state.scopePatterns || urlPatternsForCurrentPage(),
      title: document.title,
      updated_at: new Date().toISOString(),
      targets: state.targets
    };
    if (!state.targets.length) {
      await removeDraft();
      return;
    }
    await writeDraft(draft);
  }

  async function readDraft() {
    const key = pageStorageKey();
    try {
      if (typeof chrome !== "undefined" && chrome.storage?.local) {
        const values = await chrome.storage.local.get(key);
        return values?.[key] || null;
      }
    } catch (_error) {
      // Fall back to page storage below.
    }

    try {
      const raw = window.localStorage.getItem(key);
      return raw ? JSON.parse(raw) : null;
    } catch (_error) {
      return null;
    }
  }

  async function writeDraft(draft) {
    draft = sanitizeCapture(draft);
    const key = state.storageKey || pageStorageKey();
    try {
      if (typeof chrome !== "undefined" && chrome.storage?.local) {
        await chrome.storage.local.set({ [key]: draft });
        return;
      }
    } catch (_error) {
      // Fall back to page storage below.
    }

    try {
      window.localStorage.setItem(key, JSON.stringify(draft));
    } catch (_error) {
      setStatus("Could not persist captured targets");
    }
  }

  async function removeDraft() {
    const key = state.storageKey || pageStorageKey();
    try {
      if (typeof chrome !== "undefined" && chrome.storage?.local) {
        await chrome.storage.local.remove(key);
      }
    } catch (_error) {
      // Continue to localStorage cleanup below.
    }

    try {
      window.localStorage.removeItem(key);
    } catch (_error) {
      // Nothing useful to do here.
    }
  }

  function pageStorageKey() {
    return `${STORAGE_PREFIX}:${location.protocol}//${location.hostname}${location.pathname}`;
  }

  function isPersistedTarget(target) {
    return (
      target &&
      typeof target === "object" &&
      typeof target.id === "string" &&
      ["block", "ask"].includes(target.decision) &&
      target.fingerprint &&
      typeof target.fingerprint === "object"
    );
  }

  function urlPatternsForCurrentPage() {
    const exact = normalizedPageUrl();
    const host = location.hostname;
    if (host === "127.0.0.1" || host === "localhost" || host === "::1") {
      const loopback = host === "::1" ? "[::1]" : host;
      const scoped = `${location.protocol}//${loopback}:*${location.pathname}`;
      return [scoped, exact];
    }
    return [exact];
  }

  function normalizedPageUrl() {
    return `${location.origin}${location.pathname || "/"}`;
  }

  async function saveToDashboard() {
    if (!state.targets.length) { setStatus("Add at least one target"); return; }
    try {
      const destination = await chrome.runtime.sendMessage({type:"acira.destination"});
      if (!destination.destination) { setStatus("Choose a destination in Connection settings first"); return; }
      if (!window.confirm("Save this captured policy to " + destination.destination + "?")) return;
      const result = await chrome.runtime.sendMessage({type:"acira.capture", project_id:destination.project_id, agent:destination.agent, document:buildSkill()});
      if (result.error) throw new Error(result.error);
      setStatus((result.duplicate ? "Already saved to " : "Saved to ") + destination.destination);
    } catch (error) { setStatus(error.message + ". You can still copy or download JSON."); }
  }

  async function copySkill() {
    if (!state.targets.length) {
      setStatus("Add at least one target");
      return;
    }
    const text = JSON.stringify(buildSkill(), null, 2);
    try {
      await navigator.clipboard.writeText(text);
      setStatus("Skill JSON copied");
    } catch (_error) {
      fallbackCopy(text);
      setStatus("Skill JSON copied");
    }
  }

  function downloadSkill() {
    if (!state.targets.length) {
      setStatus("Add at least one target");
      return;
    }
    const text = JSON.stringify(buildSkill(), null, 2);
    const blob = new Blob([text], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `acira-skill-${slug(location.host || "page")}.json`;
    link.click();
    URL.revokeObjectURL(url);
    setStatus("Skill JSON downloaded");
  }

  function sanitizeCapture(value, key = "") {
    if (Array.isArray(value)) return value.map(item => sanitizeCapture(item));
    if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([name, item]) => [name, sanitizeCapture(item, name)]));
    if (typeof value !== "string") return value;
    if (["href", "form_action", "formaction", "url"].includes(key) || key === "action" && /^(?:https?:|\/|javascript:|data:)/i.test(value)) {
      if (!value) return null;
      try {
        const url = new URL(value, location.href);
        if (!["http:", "https:"].includes(url.protocol)) return null;
        // Signed query strings, fragments and URL credentials are operation data,
        // not stable target identity. Never persist them as policy fingerprints.
        return /^https?:/i.test(value) ? url.origin + url.pathname : url.pathname;
      } catch { return null; }
    }
    return value;
  }

  function collectFingerprint(element) {
    const attr = (name) => element.getAttribute(name) || "";
    const tag = element.tagName.toLowerCase();
    const form = element.closest("form");
    const label = nearbyLabel(element);
    const heading = nearbyHeading(element);
    const text = safeElementText(element, label || heading);
    const structure = structuralContext(element);
    return sanitizeCapture({
      selector: selectorFor(element),
      id: element.id || null,
      classes: Array.from(element.classList || []).slice(0, 16),
      tag,
      type: attr("type").toLowerCase() || null,
      role: attr("role").toLowerCase() || null,
      text: text.slice(0, 160) || null,
      aria_label: attr("aria-label") || null,
      title: attr("title") || null,
      name: attr("name") || null,
      href: attr("href") || null,
      control_kind: controlKind(element),
      labels: labelFacts(element, label, heading),
      stable_attributes: stableAttributes(element),
      nearby_text: label || heading || null,
      ancestor_text: form ? visibleContainerText(form).slice(0, 220) : null,
      section_heading: structure.section_heading,
      parent_label: structure.parent_label,
      control_index: structure.control_index,
      same_kind_index: structure.same_kind_index,
      previous_control_text: structure.previous_control_text,
      next_control_text: structure.next_control_text,
      dom_path: structure.dom_path,
      structural_context: structure,
      structure_signature: structureSignature(element, structure),
      form: form
        ? {
            id: form.id || null,
            name: form.getAttribute("name") || null,
            action: form.getAttribute("action") || null
          }
        : null,
      options: optionFacts(element),
      action_signature: actionSignature(element, label, heading),
      semantic_hints: semanticHints(element, label, heading),
      identity_tokens: identityTokens(element, label, heading),
      value_sample: ""
    });
  }

  function structuralContext(element) {
    const form = element.closest("form");
    const section = element.closest("section,fieldset,main,article") || form || document.body;
    const heading = nearbyHeadingElement(element, section);
    const label = nearbyLabel(element);
    const controls = controlsIn(section || form || document.body);
    const index = controls.indexOf(element);
    const sameKind = controls.filter((candidate) => controlKind(candidate) === controlKind(element));
    const sameKindIndex = sameKind.indexOf(element);
    const previous = index > 0 ? controls[index - 1] : null;
    const next = index >= 0 && index < controls.length - 1 ? controls[index + 1] : null;
    return {
      section_heading: heading ? visibleContainerText(heading).slice(0, 160) : null,
      parent_label: label || null,
      control_index: index >= 0 ? index : null,
      same_kind_index: sameKindIndex >= 0 ? sameKindIndex : null,
      previous_control_text: previous ? safeElementText(previous, nearbyLabel(previous) || nearbyHeading(previous)).slice(0, 160) : null,
      next_control_text: next ? safeElementText(next, nearbyLabel(next) || nearbyHeading(next)).slice(0, 160) : null,
      dom_path: domPath(element)
    };
  }

  function structureSignature(element, structure) {
    const form = element.closest("form");
    const section = element.closest("section,fieldset,main,article") || form || document.body;
    const controls = controlsIn(section || form || document.body);
    const index = controls.indexOf(element);
    const sameKind = controls.filter((candidate) => controlKind(candidate) === controlKind(element));
    const rect = element.getBoundingClientRect();
    const rootRect = section?.getBoundingClientRect?.() || { left: 0, top: 0, width: window.innerWidth || 1, height: window.innerHeight || 1 };
    const safeBand = (value, span, buckets = 12) => {
      if (!Number.isFinite(value) || !Number.isFinite(span) || span <= 0) return null;
      return Math.max(0, Math.min(buckets - 1, Math.floor((value / span) * buckets)));
    };
    const ordered = controls
      .map((candidate) => {
        const candidateRect = candidate.getBoundingClientRect();
        return { element: candidate, top: candidateRect.top, left: candidateRect.left };
      })
      .sort((a, b) => (Math.abs(a.top - b.top) > 8 ? a.top - b.top : a.left - b.left));
    const visualIndex = ordered.findIndex((item) => item.element === element);
    const rows = [];
    ordered.forEach((item) => {
      let row = rows.find((candidate) => Math.abs(candidate.top - item.top) <= 14);
      if (!row) {
        row = { top: item.top, items: [] };
        rows.push(row);
      }
      row.items.push(item);
    });
    const rowIndex = rows.findIndex((row) => row.items.some((item) => item.element === element));
    const colIndex = rowIndex >= 0 ? rows[rowIndex].items.findIndex((item) => item.element === element) : null;
    const windowItems = [];
    for (let offset = -2; offset <= 2; offset += 1) {
      const candidate = controls[index + offset];
      if (!candidate) continue;
      windowItems.push({
        offset,
        kind: controlKind(candidate),
        tag: candidate.tagName?.toLowerCase?.() || null,
        type: (candidate.getAttribute?.("type") || "").toLowerCase() || null,
        role: (candidate.getAttribute?.("role") || "").toLowerCase() || null,
        text: safeElementText(candidate, nearbyLabel(candidate) || nearbyHeading(candidate)).slice(0, 96) || null
      });
    }
    return {
      version: 1,
      control_index: index >= 0 ? index : null,
      same_kind_index: sameKind.indexOf(element) >= 0 ? sameKind.indexOf(element) : null,
      control_count: controls.length,
      same_kind_count: sameKind.length,
      target_kind: controlKind(element),
      kind_sequence: controls.map(controlKind).slice(0, 48),
      element_state: {
        disabled: Boolean(element.disabled || element.getAttribute("aria-disabled") === "true"),
        hidden: Boolean(element.hidden),
        required: Boolean(element.required || element.getAttribute("aria-required") === "true")
      },
      layout: {
        visual_index: visualIndex >= 0 ? visualIndex : null,
        row: rowIndex >= 0 ? rowIndex : null,
        col: colIndex >= 0 ? colIndex : null,
        x_band: safeBand(rect.left - rootRect.left, rootRect.width || window.innerWidth || 1),
        y_band: safeBand(rect.top - rootRect.top, rootRect.height || window.innerHeight || 1),
        width_band: safeBand(rect.width, rootRect.width || window.innerWidth || 1, 8),
        height_band: safeBand(rect.height, rootRect.height || window.innerHeight || 1, 8)
      },
      control_window: windowItems,
      ancestor_path: ancestorPath(element),
      anchors: nearbyAnchorSignature(element)
    };
  }

  function ancestorPath(element) {
    const parts = [];
    let node = element;
    while (node && node.nodeType === Node.ELEMENT_NODE && parts.length < 7) {
      parts.unshift({
        tag: node.tagName.toLowerCase(),
        role: (node.getAttribute("role") || "").toLowerCase() || null,
        kind: node === element ? controlKind(element) : null
      });
      node = node.parentElement;
    }
    return parts;
  }

  function nearbyAnchorSignature(element) {
    const rect = element.getBoundingClientRect();
    const candidates = Array.from(document.querySelectorAll("label,h1,h2,h3,h4,legend,p,span,a,button,[role='heading']"))
      .filter((candidate) => candidate !== element && !candidate.contains(element) && !isAciraNode(candidate))
      .map((candidate) => {
        const text = visibleContainerText(candidate).slice(0, 96);
        if (!text) return null;
        const box = candidate.getBoundingClientRect();
        const dx = box.left + box.width / 2 - (rect.left + rect.width / 2);
        const dy = box.top + box.height / 2 - (rect.top + rect.height / 2);
        return {
          text,
          kind: candidate.tagName.toLowerCase(),
          direction: Math.abs(dx) > Math.abs(dy) ? (dx < 0 ? "left" : "right") : dy < 0 ? "above" : "below",
          distance: Math.round(Math.sqrt(dx * dx + dy * dy))
        };
      })
      .filter(Boolean)
      .sort((a, b) => a.distance - b.distance);
    return candidates.slice(0, 6).map(({ distance, ...item }) => item);
  }

  function controlsIn(root) {
    if (!root?.querySelectorAll) return [];
    const controls = Array.from(root.querySelectorAll(INTERACTIVE_SELECTOR))
      .map(normalizeInteractiveElement)
      .filter(Boolean)
      .filter((candidate) => !isAciraNode(candidate));
    return Array.from(new Set(controls));
  }

  function nearbyHeadingElement(element, root = null) {
    const container = root || element.closest("section,main,article,form,div");
    if (!container) return null;
    return container.querySelector("h1,h2,h3,h4,legend,[role='heading']");
  }

  function domPath(element) {
    const parts = [];
    let node = element;
    while (node && node.nodeType === Node.ELEMENT_NODE && parts.length < 8) {
      const tag = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (!parent) {
        parts.unshift(tag);
        break;
      }
      const siblings = Array.from(parent.children).filter((candidate) => candidate.tagName === node.tagName);
      const index = siblings.indexOf(node) + 1;
      parts.unshift(`${tag}:nth-of-type(${Math.max(1, index)})`);
      node = parent;
    }
    return parts.join(">");
  }

  function stableAttributes(element) {
    const attrs = {};
    ["id", "name", "aria-label", "aria-labelledby", "aria-describedby", "title", "placeholder", "autocomplete", "data-testid", "data-test", "data-cy", "href", "role"].forEach(
      (name) => {
        const value = element.getAttribute(name);
        if (value) attrs[name] = value;
      }
    );
    const type = element.getAttribute("type");
    if (type) attrs.type = type;
    if (element.getAttribute("contenteditable") === "true") attrs.contenteditable = "true";
    return attrs;
  }

  function labelFacts(element, label = "", heading = "") {
    return {
      accessible: element.getAttribute("aria-label") || textFromIds(element, "aria-labelledby") || element.getAttribute("title") || label || null,
      associated: label || null,
      heading: heading || null,
      described_by: textFromIds(element, "aria-describedby") || null,
      placeholder: element.getAttribute("placeholder") || null,
      source: label ? "label" : element.getAttribute("aria-label") ? "aria-label" : element.getAttribute("placeholder") ? "placeholder" : null
    };
  }

  function optionFacts(element) {
    const tag = element.tagName.toLowerCase();
    const role = (element.getAttribute("role") || "").toLowerCase();
    if (tag === "select") {
      return Array.from(element.options || [])
        .map((option) => ({ text: (option.text || "").trim().slice(0, 80) }))
        .filter((option) => option.text)
        .slice(0, 40);
    }
    if (!["combobox", "listbox"].includes(role)) return [];
    const root = element.closest("section,fieldset,form,main,article") || document;
    return Array.from(root.querySelectorAll("[role='option']"))
      .map((option) => ({
        text: visibleContainerText(option).slice(0, 80)
      }))
      .filter((option) => option.text)
      .slice(0, 40);
  }

  function nearbyLabel(element) {
    const direct = textFromIds(element, "aria-labelledby") || element.getAttribute("aria-label");
    if (direct) return direct.slice(0, 160);
    const label = labelForControl(element);
    if (label) return visibleLabelText(label).slice(0, 160);
    const tag = element.tagName.toLowerCase();
    const type = (element.getAttribute("type") || "").toLowerCase();
    const role = (element.getAttribute("role") || "").toLowerCase();
    // Match the runtime normalizer: neighboring links are context, not this
    // link's accessible label. Borrowed labels create ambiguous fingerprints.
    if (tag === "a" || role === "link" || tag === "button" || role === "button" || ["button", "submit", "reset"].includes(type)) {
      const ownText = visibleContainerText(element) || element.value || "";
      if (ownText) return ownText.slice(0, 160);
    }
    const loose = looseNearbyLabel(element);
    return loose ? loose.slice(0, 160) : "";
  }

  function textFromIds(element, attrName) {
    const raw = element.getAttribute(attrName);
    if (!raw) return "";
    return raw
      .split(/\s+/)
      .map((id) => document.getElementById(id))
      .filter(Boolean)
      .map((node) => visibleContainerText(node))
      .filter(Boolean)
      .join(" ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function looseNearbyLabel(element) {
    const candidates = [];
    let previous = element.previousElementSibling;
    while (previous && candidates.length < 4) {
      candidates.push(previous);
      previous = previous.previousElementSibling;
    }
    const parent = element.parentElement;
    if (parent) {
      candidates.push(...Array.from(parent.children).filter((candidate) => candidate !== element && !candidate.matches(INTERACTIVE_SELECTOR)));
    }
    for (const candidate of candidates) {
      const text = visibleLabelText(candidate).slice(0, 160);
      if (text && text.length <= 120) return text;
    }
    return "";
  }

  function nearbyHeading(element) {
    const container = element.closest("section,main,article,form,div");
    if (!container) return "";
    const heading = container.querySelector("h1,h2,h3,h4,legend,[role='heading']");
    return heading ? visibleContainerText(heading).slice(0, 160) : "";
  }

  function semanticHints(element, label = "", heading = "") {
    const tag = element.tagName.toLowerCase();
    const type = (element.getAttribute("type") || "").toLowerCase();
    const role = (element.getAttribute("role") || "").toLowerCase();
    const text = [
      safeElementText(element),
      element.id,
      element.getAttribute("name"),
      element.getAttribute("aria-label"),
      textFromIds(element, "aria-labelledby"),
      textFromIds(element, "aria-describedby"),
      element.getAttribute("title"),
      element.getAttribute("placeholder"),
      label,
      heading
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    const hints = new Set([tag]);
    if (type) hints.add(type);
    if (role) hints.add(role);
    ["submit", "send", "continue", "confirm", "save", "pay", "checkout", "approve", "authorize", "select"].forEach((word) => {
      if (text.includes(word) || type === word) hints.add(word);
    });
    identityTokens(element, label, heading).forEach((token) => hints.add(token));
    return Array.from(hints);
  }

  function actionSignature(element, label = "", heading = "") {
    const form = element.closest("form");
    const type = (element.getAttribute("type") || "").toLowerCase();
    const role = (element.getAttribute("role") || "").toLowerCase();
    const kind = controlKind(element);
    const attrs = {};
    ["formaction", "formmethod", "href", "aria-controls", "data-action", "data-route", "data-testid", "data-test", "data-cy"].forEach((name) => {
      const value = element.getAttribute(name);
      if (value) attrs[name] = value.slice(0, 140);
    });
    const handlerText = ["onclick", "onchange", "onsubmit"].map((name) => element.getAttribute(name) || "").join(" ")
      .replace(/"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`/g, " ");
    const formAction = sanitizeCapture(element.getAttribute("formaction") || form?.getAttribute("action") || "", "form_action");
    const formMethod = element.getAttribute("formmethod") || form?.getAttribute("method") || "";
    const values = [
      kind,
      type,
      role,
      safeElementText(element),
      label,
      heading,
      form?.id,
      form?.getAttribute("name"),
      formAction,
      formMethod,
      ...Object.values(sanitizeCapture(attrs)),
      handlerText
    ];
    return {
      kind,
      type,
      role,
      form_id: form?.id || null,
      form_name: form?.getAttribute("name") || null,
      form_action: formAction || null,
      form_method: formMethod || null,
      attrs,
      handler_tokens: tokensFromValues([handlerText]).slice(0, 12),
      intent_tokens: tokensFromValues(values).slice(0, 24),
      text: safeElementText(element, label || heading).slice(0, 120) || null
    };
  }

  function identityTokens(element, label = "", heading = "") {
    const form = element.closest("form");
    const values = [
      element.id,
      element.getAttribute("name"),
      element.getAttribute("type"),
      element.getAttribute("role"),
      element.getAttribute("aria-label"),
      textFromIds(element, "aria-labelledby"),
      textFromIds(element, "aria-describedby"),
      element.getAttribute("title"),
      element.getAttribute("placeholder"),
      element.getAttribute("autocomplete"),
      element.getAttribute("data-testid"),
      element.getAttribute("data-test"),
      element.getAttribute("data-cy"),
      element.getAttribute("href"),
      safeElementText(element),
      label,
      heading,
      ...optionFacts(element).map((option) => option.text || "")
    ];
    return tokensFromValues(values).slice(0, 18);
  }

  function tokensFromValues(values) {
    const generic = new Set(["button", "click", "control", "data", "field", "form", "input", "item", "name", "select", "text", "value"]);
    const tokens = new Set();
    values
      .filter(Boolean)
      .join(" ")
      .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
      .match(/[a-z0-9]+/gi)
      ?.forEach((token) => {
        const normalized = token.toLowerCase();
        if (normalized.length >= 3 && !generic.has(normalized)) tokens.add(normalized);
      });
    return Array.from(tokens);
  }

  function controlKind(element) {
    const tag = element.tagName.toLowerCase();
    const type = (element.getAttribute("type") || "").toLowerCase();
    const role = (element.getAttribute("role") || "").toLowerCase();
    const text = `${safeElementText(element)} ${element.getAttribute("aria-label") || ""} ${element.getAttribute("title") || ""}`.toLowerCase();
    if (tag === "a" || role === "link" || element.getAttribute("href")) return "link";
    if (tag === "input" && type === "file") return "file";
    if ((tag === "input" && type === "range") || role === "slider") return "range";
    if (tag === "select" || ["combobox", "listbox", "option"].includes(role)) return "select";
    if ((tag === "input" && type === "radio") || role === "radio") return "radio";
    if ((tag === "input" && type === "checkbox") || ["checkbox", "switch"].includes(role)) return "checkbox";
    if (tag === "textarea" || ["textbox", "searchbox"].includes(role) || element.getAttribute("contenteditable") === "true") return "text_entry";
    if (tag === "input" && !["button", "submit", "reset", "checkbox", "radio", "file", "range", "hidden", "image"].includes(type)) return "text_entry";
    if (tag === "button" || role === "button" || ["button", "submit", "reset"].includes(type)) {
      return type === "submit" || /\b(submit|send|continue|confirm|checkout|pay|approve|authorize)\b/.test(text) ? "submit_button" : "button";
    }
    return tag;
  }

  function interactiveElement(node) {
    if (!node || node.nodeType !== Node.ELEMENT_NODE) return null;
    const element = node.closest(INTERACTIVE_SELECTOR);
    if (!element || isAciraNode(element)) return null;
    const normalized = normalizeInteractiveElement(element);
    return normalized && !isAciraNode(normalized) ? normalized : null;
  }

  function normalizeInteractiveElement(element) {
    if (!element) return null;
    if (element.tagName?.toLowerCase() === "label") {
      return labelControl(element);
    }
    return element;
  }

  function labelControl(label) {
    if (!label) return null;
    if (label.control) return label.control;
    const forId = label.getAttribute("for");
    if (forId) {
      const explicit = document.getElementById(forId);
      if (explicit) return explicit;
    }
    return label.querySelector("input,select,textarea,[role='checkbox'],[role='radio'],[role='switch'],[role='combobox']");
  }

  function labelForControl(element) {
    if (!element) return null;
    const wrapped = element.closest("label");
    if (wrapped) return wrapped;
    if (element.id) {
      return Array.from(document.querySelectorAll("label")).find((label) => label.htmlFor === element.id) || null;
    }
    return null;
  }

  function overlayElementFor(element) {
    if (isChoiceControl(element)) {
      return labelForControl(element) || element;
    }
    return element;
  }

  function isChoiceControl(element) {
    const tag = element?.tagName?.toLowerCase?.() || "";
    const type = (element?.getAttribute?.("type") || "").toLowerCase();
    const role = (element?.getAttribute?.("role") || "").toLowerCase();
    return (tag === "input" && ["radio", "checkbox"].includes(type)) || ["radio", "checkbox", "switch"].includes(role);
  }

  function isAciraNode(node) {
    return Boolean(node?.closest?.(`#${ROOT_ID}, .acira-capture-shield, .acira-capture-hover, .acira-capture-selected, .acira-capture-label`));
  }

  function selectorFor(element) {
    if (element.id) return `${element.tagName.toLowerCase()}#${CSS.escape(element.id)}`;
    const parts = [];
    let node = element;
    while (node && node.nodeType === Node.ELEMENT_NODE && parts.length < 4) {
      let part = node.tagName.toLowerCase();
      const stable = node.getAttribute("data-testid") || node.getAttribute("data-test") || node.getAttribute("data-cy");
      if (stable) {
        part += `[data-testid='${cssString(stable)}']`;
        parts.unshift(part);
        break;
      }
      if (node.classList.length) part += `.${Array.from(node.classList).slice(0, 2).map((item) => CSS.escape(item)).join(".")}`;
      parts.unshift(part);
      node = node.parentElement;
    }
    return parts.join(" > ");
  }

  function refreshOverlays() {
    if (state.pendingElement && state.visible) {
      placeOverlay(selectedBox, selectedLabel, overlayElementFor(state.pendingElement), labelForElement(state.pendingElement));
    }
  }

  function placeOverlay(box, label, element, text) {
    const rect = element.getBoundingClientRect();
    const left = Math.max(4, rect.left);
    const top = Math.max(4, rect.top);
    box.hidden = false;
    box.style.left = `${left}px`;
    box.style.top = `${top}px`;
    box.style.width = `${Math.max(1, rect.width)}px`;
    box.style.height = `${Math.max(1, rect.height)}px`;

    label.hidden = false;
    label.textContent = text;
    label.style.left = `${left}px`;
    label.style.top = `${Math.max(4, top - 28)}px`;
  }

  function hideHover() {
    hoverBox.hidden = true;
    hoverLabel.hidden = true;
  }

  function hideSelected() {
    selectedBox.hidden = true;
    selectedLabel.hidden = true;
  }

  function labelForElement(element) {
    const tag = element.tagName.toLowerCase();
    const name =
      safeElementText(element, nearbyLabel(element) || nearbyHeading(element)) ||
      element.getAttribute("aria-label") ||
      element.id ||
      element.getAttribute("name") ||
      tag;
    return `${tag} - ${truncate(name, 48)}`;
  }

  function safeElementText(element, fallback = "") {
    const tag = element.tagName.toLowerCase();
    const type = (element.getAttribute("type") || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") {
      if (element.getAttribute("aria-label")) return element.getAttribute("aria-label");
      if (fallback) return fallback;
      if (["button", "submit", "reset"].includes(type) && element.value) return element.value;
      if (element.getAttribute("placeholder")) return element.getAttribute("placeholder");
      if (element.getAttribute("name")) return element.getAttribute("name");
      return type || tag;
    }
    return visibleContainerText(element) || fallback;
  }

  function visibleContainerText(element) {
    return (element.innerText || element.textContent || "").toString().replace(/\s+/g, " ").trim();
  }

  function visibleLabelText(element) {
    if (!element?.cloneNode) return visibleContainerText(element);
    const clone = element.cloneNode(true);
    clone
      .querySelectorAll("button,input,select,textarea,option,[role='button'],[role='checkbox'],[role='radio'],[role='switch'],[role='combobox'],[role='listbox'],[role='option'],[contenteditable='true']")
      .forEach((child) => child.remove());
    return visibleContainerText(clone) || visibleContainerText(element);
  }

  function render() {
    pill.textContent = state.capturing ? "Capturing" : "Paused";
    pill.dataset.state = state.capturing ? "capturing" : "paused";
    toggleButton.innerHTML = state.capturing ? `${icon("pause")}<span>Pause capture</span>` : `${icon("cursor")}<span>Start capture</span>`;
    const guide = panel.querySelector("#acira-capture-guide");
    if (guide) {
      guide.textContent = state.capturing
        ? "Hover to preview. Click to select."
        : "Paused. Start capture to select elements.";
    }

    const selected = state.pendingFingerprint;
    selectedSummary.textContent = selected ? summaryForFingerprint(selected) : "No target selected";
    selectedSummary.classList.toggle("acira-empty-state", !selected);
    markButtons.forEach((button) => {
      button.disabled = !selected;
    });

    targetList.innerHTML = "";
    if (!state.targets.length) {
      const empty = document.createElement("div");
      empty.className = "acira-empty-state";
      empty.textContent = "No captured targets";
      targetList.appendChild(empty);
    } else {
      state.targets.forEach((target, index) => {
        const row = document.createElement("div");
        row.className = "acira-target-row";
        row.innerHTML = [
          `<span class='acira-decision acira-decision-${target.decision}'>${target.decision}</span>`,
          `<span class='acira-target-name'>${escapeHtml(summaryForFingerprint(target.fingerprint))}</span>`,
          `<button class='acira-icon-button' data-action='remove' data-index='${index}' aria-label='Remove target' title='Remove'>${icon("close")}</button>`
        ].join("");
        targetList.appendChild(row);
      });
    }
  }

  function summaryForFingerprint(fingerprint) {
    const id = fingerprint.id ? `#${fingerprint.id}` : "";
    const role = fingerprint.role ? `[${fingerprint.role}]` : "";
    const label = fingerprint.text || fingerprint.aria_label || fingerprint.name || fingerprint.href || "target";
    return `${fingerprint.tag || "element"}${id}${role} - ${truncate(label, 48)}`;
  }

  function setStatus(message) {
    status.textContent = message;
    window.clearTimeout(setStatus.timer);
    setStatus.timer = window.setTimeout(() => {
      status.textContent = "";
    }, 2600);
  }

  function fallbackCopy(text) {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "true");
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    document.documentElement.appendChild(textarea);
    textarea.select();
    document.execCommand("copy");
    textarea.remove();
  }

  function truncate(value, max) {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    return text.length > max ? `${text.slice(0, max - 1)}...` : text;
  }

  function slug(value) {
    return String(value || "target").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 48) || "target";
  }

  function cssString(value) {
    return String(value).replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  }

  function icon(name) {
    return ICONS[name] || "";
  }
})();
