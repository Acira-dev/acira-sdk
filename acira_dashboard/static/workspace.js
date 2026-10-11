"use strict";
const $ = (id) => document.getElementById(id);
const state = {
  view: "Agents",
  projects: [],
  project: null,
  agent: "default",
  catalog: [],
  busy: false,
  renderPending: false,
  review: null,
  disclosures: new Map(),
};
function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}
function notice(message, error = false) {
  $("notice").hidden = false;
  $("notice").textContent = message;
  $("notice").className = error ? "error" : "";
  if ($("dialog")?.open) {
    let feedback = $("dialog-feedback");
    if (!feedback) {
      feedback = el("p");
      feedback.id = "dialog-feedback";
      feedback.setAttribute("role", "status");
      $("dialog-content").prepend(feedback);
    }
    feedback.className = error ? "error" : "muted";
    feedback.textContent = message;
  }
}
async function api(path, body, method) {
  const response = await fetch(path, {
    method: method || (body === undefined ? "GET" : "POST"),
    headers: { "Content-Type": "application/json", "X-Acira-UI": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data;
  const raw = await response.text();
  try {
    data = JSON.parse(raw);
  } catch {
    data = { error: raw || "Request failed" };
  }
  if (!response.ok)
    throw new Error(
      data.error ||
        (response.status === 401
          ? "Run acira open to authenticate this browser tab."
          : "Request failed"),
    );
  return data;
}
function button(text, action, cls) {
  const node = el("button", text, cls);
  node.type = "button";
  node.addEventListener("click", async () => {
    node.disabled = true;
    try {
      await action();
    } catch (error) {
      notice(error.message, true);
    } finally {
      node.disabled = false;
    }
  });
  return node;
}
function pill(text) {
  const status = text.split(" · ")[0];
  return el(
    "span",
    text,
    "pill " +
      (["Active", "running", "approved"].includes(status)
        ? "ok"
        : ["Invalid configuration", "Failed to apply", "failed"].includes(status)
          ? "bad"
          : ["Applying", "pending", "paused", "waiting_for_review", "awaiting review", "waiting for review"].includes(status)
            ? "warn"
            : ""),
  );
}
function pretty(value) {
  return el("pre", JSON.stringify(value, null, 2));
}
function empty(title, message) {
  const box = el("div", undefined, "empty");
  if (state.view === "Agents") box.append(AciraReview.mascot("blip", "empty-companion"));
  box.append(el("h2", title), el("p", message));
  return box;
}
function selected() {
  return state.projects.find((project) => project.id === state.project);
}
function base() {
  if (!state.project)
    throw new Error("Initialize a project with acira init first.");
  return "/api/projects/" + encodeURIComponent(state.project);
}
function scoped() {
  return base() + "/policies?agent=" + encodeURIComponent(state.agent);
}
function option(value, label) {
  const node = el("option", label);
  node.value = value;
  return node;
}
let fileLocationId = 0;
function policyFile(path) {
  if (!path) return el("span", "Location unavailable", "muted");
  const location = String(path),
    filename = location.split(/[\\/]/).pop() || location,
    wrap = el("div", undefined, "policy-file-wrap"),
    trigger = el("button", undefined, "policy-file"),
    popup = el("div", undefined, "file-location");
  trigger.type = "button";
  trigger.title = location;
  trigger.setAttribute("aria-label", "File details for " + filename);
  trigger.setAttribute("aria-haspopup", "dialog");
  trigger.setAttribute("aria-expanded", "false");
  const icon = el("span", "{}", "policy-file-icon");
  icon.setAttribute("aria-hidden", "true");
  trigger.append(icon, el("span", filename, "policy-file-name"));
  popup.id = "file-location-" + ++fileLocationId;
  popup.setAttribute("popover", "auto");
  popup.setAttribute("role", "dialog");
  popup.setAttribute("aria-label", "File location for " + filename);
  trigger.setAttribute("aria-controls", popup.id);
  trigger.setAttribute("popovertarget", popup.id);
  const feedback = el("p", undefined, "file-location-feedback");
  feedback.setAttribute("role", "status");
  const copy = el("button", "Copy file path");
  copy.type = "button";
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(location);
      feedback.textContent = "File path copied.";
    } catch {
      feedback.textContent = "Clipboard unavailable. Select and copy the location above.";
    }
  });
  function dismiss() {
    popup.hidePopover();
    trigger.focus();
  }
  popup.append(
    el("h3", filename),
    el("p", "Location", "eyebrow"),
    el("pre", location, "file-location-path"),
    copy,
    button("Close file details", dismiss),
    feedback,
  );
  // A native popover supplies light dismissal and works inside the graph dialog.
  // The filename is the keyboard-accessible invoker; right-click opens the same UI.
  popup.addEventListener("beforetoggle", (event) => {
    trigger.setAttribute("aria-expanded", String(event.newState === "open"));
    if (event.newState !== "open") return;
    feedback.textContent = "";
    const anchor = trigger.getBoundingClientRect(),
      width = Math.min(420, window.innerWidth - 32);
    popup.style.left = Math.max(16, Math.min(anchor.left, window.innerWidth - width - 16)) + "px";
    popup.style.top = Math.max(16, Math.min(anchor.bottom + 8, window.innerHeight - 320)) + "px";
  });
  function openDetails(event) {
    event.preventDefault();
    trigger.focus();
    if (!popup.matches(":popover-open")) popup.showPopover();
    copy.focus();
  }
  trigger.addEventListener("contextmenu", openDetails);
  trigger.addEventListener("keydown", (event) => {
    if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) openDetails(event);
  });
  popup.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      dismiss();
    }
  });
  copy.autofocus = true;
  wrap.append(trigger, popup);
  return wrap;
}
function table(headers, rows) {
  const wrap = el("div", undefined, "table-wrap"),
    table = el("table"),
    head = el("thead"),
    tr = el("tr");
  headers.forEach((h) => tr.append(el("th", h)));
  head.append(tr);
  table.append(head);
  const body = el("tbody");
  rows.forEach((cells) => {
    const row = el("tr");
    cells.forEach((value) => {
      const cell = el("td");
      if ((typeof value === "string" && /^(\/|[A-Za-z]:[\\/])/.test(value)) ||
          (value instanceof Node && value.classList.contains("policy-file-wrap")))
        cell.className = "filename";
      cell.append(
        value instanceof Node
          ? value
          : document.createTextNode(String(value ?? "—")),
      );
      row.append(cell);
    });
    body.append(row);
  });
  table.append(body);
  wrap.append(table);
  return wrap;
}
function dialog(title) {
  state.review = null;
  $("dialog").classList.remove("approval-dialog");
  delete $("dialog-content").dataset.reviewState;
  $("dialog-title").textContent = title;
  $("dialog-content").replaceChildren();
  $("dialog").showModal();
  $("dialog-close").focus();
  return $("dialog-content");
}
function connectProjectDialog() {
  const body = dialog("Connect your project");
  body.append(el("p", "Use the Python environment that already runs your agent. Install the Acira SDK there, then follow these steps in your project folder."));
  const label = el("label", "Your browser framework"),
    integration = el("select");
  integration.setAttribute("aria-label", "Setup framework");
  integration.append(option("playwright", "Playwright"), option("browser_use", "browser-use"));
  label.append(integration);
  const steps = el("div", undefined, "section");
  function showSteps() {
    const adapter = integration.value;
    steps.replaceChildren();
    steps.append(
      el("h3", "1. Initialize in your project folder"),
      el("pre", "acira init --adapter " + adapter),
      el("p", "Choose your policies and automatic dashboard connection. Acira creates its configuration and policy files in this folder."),
      el("h3", "2. Add Acira to your existing async workflow"),
      el("pre", adapter === "playwright"
        ? "from acira import protect\nfrom playwright.async_api import async_playwright\n\nasync with protect() as guard:\n    async with async_playwright() as p:\n        guard.patch_playwright(p)\n        # Your existing Chromium launch/connect and workflow.\n        ..."
        : "from acira import protect\n\nasync with protect() as guard:\n    # Create your BrowserSession and Agent here with your\n    # existing browser settings and model; await agent.run().\n    ..."),
      el("p", "Use the exact project and agent snippet printed by acira init. For Playwright, patch before launching or connecting. For browser-use, create and run the session inside protect()."),
      el("h3", "3. Verify, then start your agent as usual"),
      el("pre", "acira doctor\nacira policy validate"),
      el("p", "Run your usual entry point. Configuration checks alone do not start or protect an agent. Named agents use --agent followed by their name for these checks."),
      el("h3", "4. Inspect your real run here"),
      el("p", "With automatic connection enabled, your project and run appear in Agents. Use Approvals for reviews, Policies for configuration and Activity for outcomes. You can open this dashboard later with acira open."),
    );
    const optional = el("details");
    optional.append(el("summary", "Optional separate test drive"), el("pre", adapter === "playwright" ? "acira demo" : "acira demo --adapter browser-use"), el("p", "The test drive creates separate sample projects. It is optional; your existing browser and model can be reused for your own workflow."));
    steps.append(optional);
  }
  integration.onchange = showSteps;
  body.append(label, steps);
  showSteps();
}
$("dialog-close").onclick = () => $("dialog").close();
$("dialog").addEventListener("close", () => { state.review = null; });
function detail(label, value) {
  const box = el("details");
  box.append(el("summary", label), pretty(value));
  return box;
}
// Keep a user's expanded sections open across live updates. This state belongs
// to the view/scope; it never changes a project's configuration or policy.
function disclosure(label, key, ...children) {
  const box = el("details", undefined, "disclosure");
  const identity = [state.view, state.view === "Agents" ? "all" : state.project, state.view === "Agents" ? "" : state.agent, key].join(":");
  box.dataset.disclosure = identity;
  box.open = state.disclosures.get(identity) || false;
  box.append(el("summary", label), ...children);
  box.addEventListener("toggle", () => {
    if (box.isConnected) state.disclosures.set(identity, box.open);
  });
  return box;
}
function reachableControl(node) {
  // Chromium can retain layout boxes for content inside a closed disclosure.
  // Never focus such a control: it may reopen history or strand keyboard focus.
  return node?.getClientRects().length && !node.closest("details:not([open])");
}
function affected(kind) {
  const profile = selected()?.agents.find(
    (agent) => agent.name === state.agent,
  );
  return (
    selected()
      ?.agents.filter(
        (agent) => agent.policy_roots?.[kind] === profile?.policy_roots?.[kind],
      )
      .map((agent) => agent.name) || []
  );
}
function contextLine() {
  return `${selected()?.name || "Project"} / ${state.agent}`;
}
async function refresh() {
  const data = await api("/api/projects");
  state.projects = data.projects;
  if (!state.projects.some((p) => p.id === state.project))
    state.project = state.projects[0]?.id || null;
  $("project").replaceChildren(
    ...state.projects.map((p) => option(p.id, p.name)),
  );
  $("project").value = state.project || "";
  updateAgents();
  $("connection").textContent = "Connected · Local server";
  const activation = $("policy-activation");
  if (activation) {
    const profile = selected()?.agents.find((a) => a.name === state.agent);
    activation.textContent = profile?.state || "Saved";
    activation.className = pill(profile?.state || "Saved").className;
  }
}
function updateAgents() {
  const profiles = selected()?.agents || [];
  if (!profiles.some((a) => a.name === state.agent))
    state.agent = profiles[0]?.name || "default";
  $("agent").replaceChildren(...profiles.map((a) => option(a.name, a.name)));
  $("agent").value = state.agent;
}
$("project").onchange = async () => {
  state.project = $("project").value;
  updateAgents();
  await render();
};
$("agent").onchange = async () => {
  state.agent = $("agent").value;
  await render();
};
$("refresh").onclick = async () => {
  try {
    await refresh();
    await render();
  } catch (error) {
    notice(error.message, true);
  }
};
document.querySelectorAll("[data-view]").forEach((node) =>
  node.addEventListener("click", async () => {
    state.view = node.dataset.view;
    document
      .querySelectorAll("[data-view]")
      .forEach((n) =>
        n.setAttribute("aria-current", n === node ? "page" : "false"),
      );
    await render().catch((error) => notice(error.message, true));
  }),
);

async function render() {
  if (state.busy) {
    // Coalesce a navigation or scope change while the current view is loading.
    // Its latest selection must render as soon as that request finishes.
    state.renderPending = true;
    return;
  }
  state.busy = true;
  const focusedSection = document.activeElement?.parentElement?.dataset.disclosure;
  const previousView = $("content").dataset.view;
  $("content").querySelectorAll("details[data-disclosure]").forEach((node) =>
    state.disclosures.set(node.dataset.disclosure, node.open),
  );
  try {
    $("page-title").textContent = state.view;
    $("page-description").textContent = {
      Agents: "See what’s running. Open a project to manage its agents.",
      Approvals: "A clear view of what your agent wants to do.",
      Policies: "Set the boundaries. Your agents keep them.",
      Activity: "See what was checked, decided, and actually done.",
      Backtesting: "Compare your policies against recorded actions.",
      Settings: "Your projects, connections, and local preferences.",
    }[state.view];
    document.querySelectorAll("[data-view]").forEach((node) =>
      node.setAttribute("aria-current", node.dataset.view === state.view ? "page" : "false"),
    );
    const globalView = ["Agents", "Approvals"].includes(state.view);
    document.querySelectorAll("[data-scoped]").forEach((node) => { node.hidden = globalView; });
    document.querySelector(".scope").classList.toggle("global-scope", globalView);
    document.querySelector("header").classList.toggle("global-view", globalView);
    $("more-tools").classList.toggle("has-selection", ["Activity", "Backtesting", "Settings"].includes(state.view));
    const content = $("content");
    content.replaceChildren();
    content.dataset.view = state.view;
    if (!state.projects.length) {
      content.append(
        empty(
          "Connect your first project",
          "Run acira init inside an existing browser-agent project, then start it with the Acira SDK. It will appear here automatically.",
        ),
        button("Connect a project", connectProjectDialog, "primary"),
      );
      return;
    }
    await {
      Agents: agents,
      Approvals: approvals,
      Policies: policies,
      Activity: activity,
      Backtesting: backtesting,
      Settings: settings,
    }[state.view](content);
  } catch (error) {
    $("content").append(
      empty("This view needs attention", error.message),
      el(
        "p",
        "Use acira doctor in the project to inspect invalid files or permissions. Your local guard continues to enforce its safety checks.",
        "muted",
      ),
    );
    notice(error.message, true);
  } finally {
    if (focusedSection && previousView === state.view) {
      const same = Array.from($("content").querySelectorAll("[data-disclosure]"))
        .find((node) => node.dataset.disclosure === focusedSection);
      same?.querySelector("summary")?.focus();
    }
    state.busy = false;
    if (state.renderPending) {
      state.renderPending = false;
      await render();
    }
  }
}

function runControls(run) {
  const controls = el("div", undefined, "actions");
  const runButton = (label, action, operation) => {
    const node = button(label, action);
    node.dataset.runId = run.run_id;
    node.dataset.projectId = run.project.id;
    node.dataset.runAction = operation;
    return node;
  };
  if (run.capabilities?.pause && !["ended", "failed", "disconnected"].includes(run.state))
    controls.append(runButton(run.state === "paused" ? "Resume" : "Pause", async () => {
      await api(`/api/projects/${run.project.id}/runs/${run.run_id}/control`,
        { desired: run.state === "paused" ? "running" : "paused" });
      notice("Control requested. The run will acknowledge it at its next guarded boundary.");
    }, run.state === "paused" ? "resume" : "pause"));
  controls.append(runButton("Inspect", () => {
    const body = dialog(run.agent + " · " + run.project.name);
    body.append(el("p", run.capabilities?.boundary || "Integration capabilities"),
      detail("Supported actions", run.capabilities?.guarded),
      detail("Limits to protection", run.capabilities?.limitations),
      detail("Run details", run));
  }, "inspect"));
  return controls;
}
function runCard(run) {
  const card = el("article", undefined, "run-card");
  card.dataset.runCard = run.run_id;
  card.dataset.projectId = run.project.id;
  const title = el("div", undefined, "run-heading");
  title.append(el("h3", run.agent), el("span", run.project.name, "muted"));
  const status = el("div", undefined, "run-status");
  status.append(pill(run.state.replaceAll("_", " ")));
  if (!run.connected && !["ended", "failed"].includes(run.state))
    status.append(el("span", "Dashboard disconnected", "muted"));
  const heading = el("div", undefined, "row");
  heading.append(title, status);
  card.append(heading);
  if (run.error) card.append(el("p", run.error, "error"));
  const bottom = el("div", undefined, "row run-bottom");
  bottom.append(el("span", run.last_operation?.state || "No guarded action pending", "muted"), runControls(run));
  card.append(bottom);
  return card;
}
async function agents(content) {
  const runs = state.projects.flatMap((project) => project.runs.map((run) => ({ ...run, project })));
  const live = runs.filter((run) => !["ended", "failed"].includes(run.state));
  const history = runs.filter((run) => ["ended", "failed"].includes(run.state));
  // Pending reviews remain prominent even when no SDK run is currently connected.
  const reviewSets = await Promise.allSettled(state.projects.map((project) => api(`/api/projects/${project.id}/reviews`)));
  const pending = reviewSets.reduce((n, result) => n + (result.status === "fulfilled" ? result.value.reviews.filter((review) => review.state === "pending").length : 0), 0);
  const unavailable = reviewSets.some((result) => result.status === "rejected");
  $("review-count").textContent = pending || (unavailable ? "!" : "");
  if (unavailable) content.append(el("p", "Some review requests could not be loaded. Refresh to try again; the counts below may be incomplete.", "error"));
  if (pending) {
    const attention = el("section", undefined, "attention-strip");
    attention.append(el("div", `${pending} ${pending === 1 ? "action needs" : "actions need"} your review`),
      button("Open approvals", async () => { state.view = "Approvals"; await render(); }, "primary"));
    content.append(attention);
  }
  const running = el("section", undefined, "running-section");
  running.setAttribute("aria-label", "Current runs");
  if (live.length) {
    running.append(el("h2", "Current runs"));
    const list = el("div", undefined, "run-list");
    live.forEach((run) => list.append(runCard(run)));
    running.append(list);
  } else {
    const quiet = el("div", undefined, "quiet-overview");
    const message = el("div");
    message.append(el("h2", "No agents running"), el("p", "Start your usual agent command. Its run will appear here automatically.", "muted"));
    quiet.append(AciraReview.mascot("pill", "overview-companion"), message);
    running.append(quiet);
  }
  content.append(running);
  const projects = el("section", undefined, "projects-section");
  const heading = el("div", undefined, "toolbar section-heading");
  heading.append(el("h2", "Your projects"), button("Connect a project", connectProjectDialog));
  projects.append(heading);
  const list = el("div", undefined, "project-list");
  for (const project of state.projects) {
    const row = disclosure("", "project-" + project.id);
    row.classList.add("project-row");
    const summary = row.querySelector("summary");
    const name = el("div", undefined, "project-name");
    name.append(el("h2", project.name), el("span", `${project.agents.length} ${project.agents.length === 1 ? "agent" : "agents"}`, "muted"));
    const count = live.filter((run) => run.project.id === project.id).length;
    const issue = project.error || project.agents.find((profile) => profile.error)?.error;
    summary.append(name, pill(issue ? "Needs attention" : count ? count + (count === 1 ? " current run" : " current runs") : "Ready"));
    const profiles = el("div", undefined, "project-agents");
    for (const profile of project.agents) {
      const agent = el("div", undefined, "row agent-row");
      const choose = button(profile.name, async () => {
        state.project = project.id;
        state.agent = profile.name;
        state.view = "Policies";
        await refresh();
        await render();
      }, "agent-link");
      choose.dataset.projectId = project.id;
      choose.dataset.agentName = profile.name;
      agent.append(choose, pill(profile.state === "Waiting for agent" ? "Configured" : profile.state || "Saved"));
      profiles.append(agent);
    }
    profiles.append(disclosure("Project folder", "folder-" + project.id, el("pre", project.root)));
    row.append(profiles);
    list.append(row);
    // Configuration errors must never be hidden inside a closed project row.
    const showError = (label, message, key) => {
      const warning = el("div", undefined, "project-error error");
      warning.append(el("p", label + ": configuration needs attention."),
        disclosure("Error details", key, el("pre", message)));
      list.append(warning);
    };
    if (project.error) showError(project.name, project.error, "error-" + project.id);
    project.agents.filter((profile) => profile.error).forEach((profile) =>
      showError(project.name + " / " + profile.name, profile.error, "error-" + project.id + "-" + profile.name));
  }
  projects.append(list);
  content.append(projects);
  if (history.length) {
    const previous = el("div", undefined, "run-list");
    history.forEach((run) => previous.append(runCard(run)));
    const completed = disclosure(`Previous runs (${history.length})`, "previous-runs", previous);
    completed.classList.add("previous-runs");
    content.append(completed);
  }
}

async function approvals(content) {
  const all = [];
  for (const project of state.projects) {
    const data = await api(`/api/projects/${project.id}/reviews`);
    all.push(...data.reviews.map((r) => ({ ...r, project })));
  }
  const pending = all.filter((r) => r.state === "pending");
  $("review-count").textContent = pending.length || "";
  if (!pending.length) {
    content.append(empty("No actions awaiting review", "Your agents can continue under their local policies."));
    return;
  }
  const list = el("div", undefined, "approval-list");
  for (const review of pending) {
    const card = el("article", undefined, "approval-card");
    card.dataset.reviewCard = review.review_id;
    card.dataset.runId = review.run_id;
    const facts = el("div");
    facts.append(el("p", review.project.name + " / " + review.agent, "muted"),
      el("h2", AciraReview.question(review.classification?.action, review.classification?.target?.text || "")),
      el("p", review.reason), el("small", "Expires " + new Date(review.expires_at * 1000).toLocaleTimeString(), "muted"));
    const open = button("Review operation", () => reviewDialog(review), "primary");
    open.dataset.reviewId = review.review_id;
    card.append(facts, open);
    list.append(card);
  }
  content.append(list);
}

async function reviewDialog(review) {
  const body = dialog("Review · " + review.project.name + " / " + review.agent);
  $("dialog").classList.add("approval-dialog");
  const target = review.classification?.target;
  const status = pill(review.state);
  const targetLabel = target?.text || target?.aria_label || target?.id || "";
  const headline = el("div", undefined, "review-question");
  headline.append(el("h3", AciraReview.question(review.classification?.action, targetLabel), "review-target"), status);
  body.append(headline);
  const evidence = review.evidence || {};
  const context = el("details", undefined, "review-context");
  context.append(
    el("summary", "Run and policy revision"),
    pretty({ run: review.run_id, policy_revision: review.policy_revision }),
  );
  const evidencePanel = el("section", undefined, "review-evidence");
  evidencePanel.setAttribute("aria-label", "Operation evidence");
  body.append(evidencePanel);
  const destination = el("p", undefined, "review-destination");
  destination.append(el("span", "Destination", "muted"),
    el("span", evidence.destination || "Not established"));
  evidencePanel.append(destination);
  if (evidence.fields?.length)
    evidencePanel.append(
      table(
        ["Reviewed field", "Observed value"],
        evidence.fields.map((field) => [field.name, field.display]),
      ),
    );
  if (evidence.submission_fields?.length)
    evidencePanel.append(
      el("h3", "Exact request that may be sent"),
      el("p", evidence.request_method + " " + evidence.destination),
      table(
        ["Request field", "Permitted value"],
        evidence.submission_fields.map((field) => [field.name, field.display]),
      ),
    );
  if (evidence.verified_fields && Object.keys(evidence.verified_fields).length)
    evidencePanel.append(
      el("h3", "Verified operation facts"),
      table(
        ["Fact", "Reviewed value"],
        Object.entries(evidence.verified_fields),
      ),
    );
  if (evidence.bounded_steps?.length)
    evidencePanel.append(
      el("h3", "Declared form changes"),
      table(
        ["Field", "Proposed value"],
        evidence.bounded_steps.map((step) => [step.field, step.text]),
      ),
    );
  const artifact = review.artifact || {};
  if (artifact.capture_status === "ready" && (artifact.image || artifact.id)) {
    const source = artifact.image
      ? "data:image/png;base64," + artifact.image
      : `/api/projects/${review.project.id}/artifacts/${artifact.id}`;
    body.append(AciraReview.preview(artifact, source, targetLabel));
  } else
    body.append(
      el(
        "p",
        artifact.reason ||
          "No masked screenshot is available. Review the structured operation evidence.",
        "muted",
      ),
    );
  body.append(
    context,
    detail("Operation details", { ...evidence, artifact: undefined }),
    detail("Policy explanation", review.classification),
  );
  let grant;
  if (review.grant_eligible) {
    const label = el("label", undefined, "checkbox");
    grant = el("input");
    grant.type = "checkbox";
    label.append(
      grant,
      document.createTextNode(
        "Also allow this routine scope for up to 15 minutes in this run",
      ),
    );
    body.append(label, detail("Exact reusable scope", evidence.grant_scope));
  }
  const feedback = el("p", undefined, "muted");
  feedback.setAttribute("role", "status");
  feedback.id = "review-status";
  const actions = el("fieldset", undefined, "actions");
  actions.style.border = "0";
  actions.style.padding = "0";
  actions.setAttribute("aria-label", "Review decision");
  const current = { review, body, status, feedback, actions, grant };
  state.review = current;
  actions.disabled = true;
  for (const [label, decision, cls] of [
    ["Reject", "rejected", "danger"],
    ["Approve once", "approved", "primary"],
  ])
    actions.append(
      button(
        label,
        async () => {
          await refreshOpenReview();
          if (state.review !== current) return;
          if (current.review.state !== "pending") {
            $("dialog-close").focus();
            return;
          }
          const result = await api(
            `/api/projects/${review.project.id}/reviews/${review.review_id}/decision`,
            {
              decision,
              grant: decision === "approved" && Boolean(grant?.checked),
            },
          );
          if (result.review.state !== decision) {
            updateReviewStatus(current, result.review);
            $("dialog-close").focus();
            return;
          }
          $("dialog").close();
          notice(
            decision === "approved"
              ? "Approval recorded. Acira rechecks the operation before dispatch."
              : "Action rejected.",
          );
          await render();
        },
        cls,
      ),
    );
  const decisionBar = el("div", undefined, "review-decision-bar");
  decisionBar.append(feedback, actions);
  body.append(decisionBar);
  await refreshOpenReview();
}

function updateReviewStatus(current, review) {
  const losesFocus = current.actions.contains(document.activeElement) || current.grant === document.activeElement;
  current.review = { ...current.review, ...review };
  current.body.dataset.reviewState = review.state;
  current.status.replaceWith(pill(review.state));
  current.status = current.body.querySelector(".review-question .pill");
  current.actions.disabled = review.state !== "pending";
  if (current.grant) current.grant.disabled = current.actions.disabled;
  if (current.actions.disabled && losesFocus) $("dialog-close").focus();
  const messages = {
    pending: "Awaiting your decision. Approval permits one attempt and the SDK rechecks it before dispatch.",
    expired: "This review expired. The action was blocked. Start a fresh attempt from your agent.",
    invalidated: "This review is no longer valid. Acira must reevaluate the operation before a new approval.",
    approved: "This operation was already approved. Check Activity for its execution outcome.",
    rejected: "This operation was rejected. The action was blocked.",
    unavailable: "Review status unavailable. Reconnect to the local service before deciding.",
  };
  current.feedback.textContent = messages[review.state] || "This review is closed. Check Activity for its outcome.";
}

async function refreshOpenReview() {
  const current = state.review;
  if (!current || !$("dialog").open) return;
  try {
    const data = await api(`/api/projects/${current.review.project.id}/reviews`);
    if (state.review !== current || !$("dialog").open) return;
    const found = data.reviews.find((item) => item.review_id === current.review.review_id);
    updateReviewStatus(current, { ...current.review, ...(found || { state: "unavailable" }) });
  } catch (error) {
    if (state.review === current && $("dialog").open)
      updateReviewStatus(current, { ...current.review, state: "unavailable" });
  }
}

async function policies(content) {
  const data = await api(scoped());
  const bar = el("div", undefined, "toolbar"),
    title = el("div");
  title.append(
    el("h2", contextLine()),
    el(
      "p",
      "Changes save to your project files and activate at the next guarded boundary.",
      "muted",
    ),
  );
  const actions = el("div", undefined, "actions");
  actions.append(
    button("Add protection", catalogDialog, "primary"),
    button("Capture specific policy", capturePolicyDialog),
    button("Import JSON", importDialog),
  );
  const tools = el("div", undefined, "actions");
  tools.append(
    button("Check policy updates", updatesDialog),
    button("Revision history", revisionDialog),
    button("Agent assignments", assignmentDialog),
    button("Assignments / graph", graphDialog),
  );
  const more = disclosure("More policy actions", "policy-tools", tools);
  more.classList.add("policy-tools");
  bar.append(title, actions);
  content.append(bar);
  const profile = selected().agents.find((a) => a.name === state.agent);
  const activation = pill(profile?.state || "Saved");
  activation.id = "policy-activation";
  const status = el("div", undefined, "policy-status row");
  status.append(activation, more);
  content.append(status);
  if (!data.policies.length) {
    content.append(
      empty(
        "Add your first policy",
        "Choose a general protection or import a capture from the extension.",
      ),
    );
    return;
  }
  content.append(
    table(
      ["Policy", "Type", "File", "Edit"],
      data.policies.map((policy) => [
        policy.name,
        policy.kind === "specific" ? "Specific · captured target" : "General protection",
        policyFile(policy.path),
        button("Configure", () => policyDialog(policy)),
      ]),
    ),
  );
}
function capturePolicyDialog() {
  const body = dialog("Capture a specific policy · " + contextLine());
  body.append(el("p", "A specific policy protects the website targets you select, such as a particular button or link. General protections cover broad risks."),
    el("h3", "Set up Chrome capture once"),
    el("pre", "acira extension install"),
    el("p", "Open chrome://extensions, enable Developer mode, choose Load unpacked, and select the folder printed by that command."),
    el("h3", "Select the target on your website"),
    el("p", "Click Acira Capture in the Chrome toolbar. Select a target and choose Ask first or Block. Capture selection does not activate the website control."),
    el("h3", "Save to this agent"),
    el("p", "Pair the extension in Settings and select this project and agent to save directly. Or download the JSON and import it here or in your terminal."),
    el("pre", "acira policy import capture.json\nacira policy configure"),
    button("Import capture JSON", importDialog, "primary"));
}
async function catalogDialog() {
  const data = await api("/api/catalog"),
    body = dialog("Add a protection · " + contextLine()),
    cards = el("div", undefined, "cards");
  body.append(
    el(
      "p",
      "This general policy folder affects: " + affected("general").join(", "),
    ),
  );
  for (const entry of data.policies) {
    const card = el("div", undefined, "card");
    card.append(
      el("h3", entry.document.name),
      el("p", entry.document.description || entry.name),
      button(
        "Add policy",
        async () => {
          await api(base() + "/policies", {
            agent: state.agent,
            filename: entry.filename,
            document: entry.document,
            expected: null,
          });
          $("dialog").close();
          notice(
            "Saved. Connected runs will report when the policy is active.",
          );
          await refresh();
          await render();
        },
        "primary",
      ),
    );
    cards.append(card);
  }
  body.append(cards, button("Import from a catalog", remoteCatalogDialog));
}
function importDialog() {
  const body = dialog("Import policy · " + contextLine()),
    input = el("input"),
    text = el("textarea", undefined, "code"),
    name = el("input");
  input.type = "file";
  input.accept = ".json,application/json";
  name.value = "captured-policy.json";
  input.onchange = async () => {
    if (input.files[0]) {
      text.value = await input.files[0].text();
      name.value = input.files[0].name;
    }
  };
  text.setAttribute("aria-label", "Policy JSON");
  name.setAttribute("aria-label", "Policy filename");
  body.append(
    el(
      "p",
      "Choose a JSON export or paste it below. It is validated before any file is changed. Shared folders affect every agent assigned to that folder.",
    ),
    input,
    name,
    text,
    button(
      "Validate and import",
      async () => {
        await api(base() + "/policies", {
          agent: state.agent,
          filename: name.value,
          document: JSON.parse(text.value),
          expected: null,
        });
        $("dialog").close();
        notice("Policy imported.");
        await refresh();
        await render();
      },
      "primary",
    ),
  );
}
function policyDialog(policy) {
  const body = dialog(policy.name + " · " + contextLine()),
    documentCopy = structuredClone(policy.document);
  const affected = policy.affected_agents || [state.agent];
  body.append(
    policyFile(policy.path),
    el(
      "p",
      "This file may be shared. Affected assignments: " +
        (affected.join(", ") || state.agent),
      "muted",
    ),
  );
  const editor = el("textarea", undefined, "code");
  editor.value = JSON.stringify(documentCopy, null, 2);
  editor.setAttribute("aria-label", "Advanced policy JSON");
  editor.oninput = () =>
    notice("Unsaved changes · validate and save to update the project.");
  if (policy.kind === "general") {
    documentCopy.acira_decisions ||= {};
    for (const risk of documentCopy.risks || []) {
      const row = el("div", undefined, "risk-row"),
        label = el("div"),
        select = el("select");
      label.append(
        el("strong", risk.id || risk.risk),
        el("p", risk.description || risk.reason),
      );
      ["allow", "ask", "block"].forEach((value) =>
        select.append(option(value, value)),
      );
      select.value =
        documentCopy.acira_decisions[risk.id || risk.risk] || "ask";
      select.setAttribute(
        "aria-label",
        risk.description || risk.id || risk.risk,
      );
      select.onchange = () => {
        notice("Unsaved changes · this file affects the listed agents.");
        documentCopy.acira_decisions[risk.id || risk.risk] = select.value;
        editor.value = JSON.stringify(documentCopy, null, 2);
      };
      row.append(label, select);
      body.append(row);
    }
  }
  if (policy.kind === "specific") {
    const scope = documentCopy.scope?.url_patterns || documentCopy.url_patterns || ["All websites"];
    body.append(el("p", "Applies on: " + (Array.isArray(scope) ? scope : [scope]).join(", "), "muted"));
    for (const target of documentCopy.targets || []) {
      const row = el("div", undefined, "risk-row"), label = el("div"), select = el("select"),
        fingerprint = target.fingerprint || target.match || {},
        targetName = fingerprint.text || fingerprint.aria_label || fingerprint.id || target.id;
      label.append(el("strong", targetName), el("p", "Captured target · " + target.id, "muted"));
      select.append(option("ask", "Ask for human review"), option("block", "Block action"));
      select.value = target.decision;
      select.setAttribute("aria-label", "Decision for " + targetName);
      select.onchange = () => {
        target.decision = select.value;
        if (["Block activation of captured target.", "Ask before activation of captured target."].includes(target.reason))
          target.reason = (select.value === "block" ? "Block" : "Ask before") + " activation of captured target.";
        editor.value = JSON.stringify(documentCopy, null, 2);
        notice("Unsaved changes · this file affects the listed agents.");
      };
      row.append(label, select);
      body.append(row);
    }
  }
  const advanced = el("details");
  advanced.append(el("summary", "Advanced JSON"), editor);
  body.append(advanced);
  body.append(
    button("Export draft JSON", () => {
      const url = URL.createObjectURL(
        new Blob([editor.value], { type: "application/json" }),
      );
      const link = el("a");
      link.href = url;
      link.download = policy.path.split(/[\\/]/).pop();
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }),
  );
  body.append(
    button(
      "Save policy",
      async () => {
        const result = await api(base() + "/policies", {
          agent: state.agent,
          filename: policy.path.split(/[\\/]/).pop(),
          document: JSON.parse(editor.value),
          expected: policy.hash,
        });
        $("dialog").close();
        notice("Saved · affected agents: " + result.affected_agents.join(", "));
        await refresh();
        await render();
      },
      "primary",
    ),
  );
}
async function revisionDialog() {
  const endpoint = base(),
    agent = state.agent,
    current = await api(scoped()),
    configuration = await api(endpoint + "/configuration"),
    body = dialog("Policy revisions · " + contextLine()),
    list = el("div"),
    comparison = el("div");
  let offset = 0,
    revisions = [];
  const more = button("Load older revisions", load);
  async function load() {
    const data = await api(
      endpoint +
        "/revisions?" +
        new URLSearchParams({ agent, offset: String(offset) }),
    );
    const known = new Set(revisions.map((item) => item.revision));
    revisions.push(
      ...data.revisions.filter((item) => !known.has(item.revision)),
    );
    offset = data.next_offset;
    more.hidden = offset === null;
    list.replaceChildren(
      table(
        ["Saved", "Revision", "Inspect", "Restore"],
        revisions.map((revision) => [
          new Date(revision.created_at * 1000).toLocaleString(),
          revision.revision.slice(0, 12),
          button("Compare with current", async () => {
            const snapshot = await api(
              endpoint +
                "/revisions/" +
                revision.revision +
                "?agent=" +
                encodeURIComponent(agent),
            );
            const currentFiles = Object.fromEntries(
              current.policies.map((policy) => [policy.path, policy.document]),
            );
            comparison.replaceChildren(
              el("h3", "Changes since " + revision.revision.slice(0, 12)),
              table(
                ["Changed path", "Selected revision", "Current files"],
                jsonChanges(
                  { profile: snapshot.profile, policies: snapshot.documents },
                  {
                    profile: configuration.document.agents[agent],
                    policies: currentFiles,
                  },
                ),
              ),
              detail("Raw snapshot", snapshot),
            );
          }),
          button("Restore", async () => {
            if (
              !confirm(
                "Restore this policy snapshot? Shared policy files can affect other agents. A new revision will be created.",
              )
            )
              return;
            await api(endpoint + "/rollback", {
              agent,
              revision: revision.revision,
              expected: current.revision,
            });
            $("dialog").close();
            notice("Rollback saved as a new revision.");
            await refresh();
            await render();
          }),
        ]),
      ),
    );
  }
  body.append(list, more, comparison);
  await load();
}
async function graphDialog() {
  const project = selected(),
    endpoint = base(),
    configuration = await api(endpoint + "/configuration"),
    body = dialog("Policy assignments · " + project.name);
  const groups = await Promise.all(
    project.agents.map(async (agent) => ({
      agent,
      policies: (
        await api(
          endpoint + "/policies?agent=" + encodeURIComponent(agent.name),
        )
      ).policies,
    })),
  );
  const resources = new Map();
  for (const group of groups) {
    for (const policy of group.policies)
      resources.set("policy:" + policy.path, {
        key: "policy:" + policy.path,
        kind: "policy",
        label: policy.name,
        path: policy.path,
        policy,
      });
    const profile = configuration.document.agents[group.agent.name];
    const secret =
      profile.guard?.secrets_config_path || profile.guard?.secrets_config_file;
    if (secret)
      resources.set("secret:" + secret, {
        key: "secret:" + secret,
        kind: "secret",
        label: "Secret references",
        path: secret,
      });
  }
  const nodes = [
    ...groups.map((group) => ({
      key: "agent:" + group.agent.name,
      kind: "agent",
      label: group.agent.name,
      group,
    })),
    ...resources.values(),
  ];
  const height = Math.max(1, groups.length, resources.size) * 70 + 60;
  const layoutKey = "acira.graph." + project.id;
  let stored = {};
  try {
    stored = JSON.parse(localStorage.getItem(layoutKey) || "{}");
  } catch {}
  function arrange() {
    for (const [index, group] of groups.entries())
      Object.assign(
        nodes.find((node) => node.key === "agent:" + group.agent.name),
        { x: 20, y: index * 70 + 20 },
      );
    [...resources.values()].forEach((node, index) =>
      Object.assign(node, { x: 470, y: index * 70 + 20 }),
    );
  }
  arrange();
  for (const node of nodes) {
    const point = stored[node.key];
    if (point && Number.isFinite(point.x) && Number.isFinite(point.y))
      Object.assign(node, {
        x: Math.min(10000, Math.max(0, point.x)),
        y: Math.min(10000, Math.max(0, point.y)),
      });
  }
  function persist() {
    try {
      localStorage.setItem(
        layoutKey,
        JSON.stringify(
          Object.fromEntries(
            nodes.map((node) => [node.key, { x: node.x, y: node.y }]),
          ),
        ),
      );
    } catch {}
  }
  const ns = "http://www.w3.org/2000/svg",
    svg = document.createElementNS(ns, "svg"),
    toolbar = el("div", undefined, "toolbar"),
    inspector = el("section", undefined, "card"),
    search = el("input"),
    assignmentTable = el("div");
  svg.classList.add("assignment-canvas");
  svg.setAttribute("role", "group");
  svg.setAttribute(
    "aria-label",
    "Policy assignment graph. Select nodes to inspect. Arrow keys move the selected node.",
  );
  search.placeholder = "Search agents and policies";
  search.setAttribute("aria-label", "Search assignment graph");
  let selectedNode = null,
    drag = null,
    view = { x: 0, y: 0, width: 760, height };
  const edges = groups.flatMap((group) => {
    const result = group.policies.map((policy) => ({
      from: "agent:" + group.agent.name,
      to: "policy:" + policy.path,
      disabled: group.agent.disabled_policies?.includes(policy.id),
    }));
    const profile = configuration.document.agents[group.agent.name];
    const secret =
      profile.guard?.secrets_config_path || profile.guard?.secrets_config_file;
    if (secret)
      result.push({
        from: "agent:" + group.agent.name,
        to: "secret:" + secret,
        secret: true,
      });
    return result;
  });
  function shape(tag, attributes, parent = svg, text) {
    const node = document.createElementNS(ns, tag);
    for (const [key, value] of Object.entries(attributes))
      node.setAttribute(key, value);
    if (text !== undefined) node.textContent = text;
    parent.append(node);
    return node;
  }
  function wire(edge) {
    const source = nodes.find((node) => node.key === edge.from),
      target = nodes.find((node) => node.key === edge.to);
    return (
      "M " +
      (source.x + 220) +
      " " +
      (source.y + 22) +
      " C " +
      (source.x + 300) +
      " " +
      (source.y + 22) +
      ", " +
      (target.x - 80) +
      " " +
      (target.y + 22) +
      ", " +
      target.x +
      " " +
      (target.y + 22)
    );
  }
  function updatePositions() {
    svg.querySelectorAll("[data-node]").forEach((group) => {
      const node = nodes[Number(group.dataset.node)];
      group.setAttribute(
        "transform",
        "translate(" + node.x + " " + node.y + ")",
      );
    });
    svg
      .querySelectorAll("[data-edge]")
      .forEach((path) =>
        path.setAttribute("d", wire(edges[Number(path.dataset.edge)])),
      );
  }
  function updateView() {
    svg.setAttribute(
      "viewBox",
      [view.x, view.y, view.width, view.height].join(" "),
    );
  }
  function zoom(factor) {
    const width = Math.min(15000, Math.max(250, view.width * factor));
    const ratio = width / view.width;
    view.x += (view.width - width) / 2;
    view.y += (view.height - view.height * ratio) / 2;
    view.width = width;
    view.height *= ratio;
    updateView();
  }
  async function useAgent(name, action) {
    state.project = project.id;
    state.agent = name;
    await refresh();
    await action();
  }
  async function toggle(group, policy) {
    const document = structuredClone(configuration.document),
      profile = document.agents[group.agent.name];
    const disabled = profile.disabled_policies || [];
    profile.disabled_policies = disabled.includes(policy.id)
      ? disabled.filter((id) => id !== policy.id)
      : [...disabled, policy.id];
    await api(
      endpoint + "/configuration",
      { document, expected: configuration.hash },
      "PUT",
    );
    $("dialog").close();
    notice("Assignment saved for " + group.agent.name + ".");
    await refresh();
    await render();
  }
  function inspect(node) {
    selectedNode = node;
    svg
      .querySelectorAll("[data-node]")
      .forEach((group) =>
        group.setAttribute(
          "aria-pressed",
          nodes[Number(group.dataset.node)] === node ? "true" : "false",
        ),
      );
    inspector.replaceChildren(el("h3", node.label));
    if (node.kind === "agent") {
      const name = node.group.agent.name,
        profile = configuration.document.agents[name];
      inspector.append(
        el("p", project.name + " / " + name),
        detail("Policy folders", profile.policies),
        button("Edit assignments for " + name, () =>
          useAgent(name, assignmentDialog),
        ),
        button("Edit folders for " + name, () => folderDialog(name)),
        button("Secret references for " + name, () =>
          useAgent(name, secretDialog),
        ),
      );
    } else {
      inspector.append(el("p", node.path, "filename"));
      const owners = groups.filter((group) =>
        node.kind === "policy"
          ? group.policies.some((policy) => policy.path === node.path)
          : (configuration.document.agents[group.agent.name].guard
              ?.secrets_config_path ||
              configuration.document.agents[group.agent.name].guard
                ?.secrets_config_file) === node.path,
      );
      if (node.kind === "policy") {
        inspector.append(
          el(
            "p",
            "Shared file edits affect: " +
              owners.map((group) => group.agent.name).join(", "),
          ),
        );
        inspector.append(
          button("Edit policy file", () =>
            useAgent(owners[0].agent.name, () => policyDialog(node.policy)),
          ),
        );
        for (const group of owners)
          inspector.append(
            button(
              (group.agent.disabled_policies?.includes(node.policy.id)
                ? "Enable for "
                : "Disable for ") + group.agent.name,
              () => toggle(group, node.policy),
            ),
          );
      } else {
        for (const group of owners)
          inspector.append(
            button("Manage references for " + group.agent.name, () =>
              useAgent(group.agent.name, secretDialog),
            ),
          );
      }
    }
  }
  for (const [index, edge] of edges.entries())
    shape("path", {
      "data-edge": index,
      d: wire(edge),
      stroke: edge.disabled ? "#94a3b8" : edge.secret ? "#8b5cf6" : "#3b82f6",
      "stroke-width": 2,
      "stroke-dasharray": edge.disabled ? "5 5" : "none",
      fill: "none",
    });
  nodes.forEach((node, index) => {
    const group = shape("g", {
      "data-node": index,
      role: "button",
      tabindex: "0",
      "aria-pressed": "false",
      "aria-label": "Inspect " + node.kind + " " + node.label,
      transform: "translate(" + node.x + " " + node.y + ")",
    });
    shape(
      "rect",
      {
        width: 220,
        height: 44,
        rx: 8,
        fill: node.kind === "secret" ? "#f5f3ff" : "#eff6ff",
        stroke: "#bfdbfe",
      },
      group,
    );
    shape(
      "text",
      { x: 12, y: 27, fill: "#15233b", "font-size": 13 },
      group,
      node.label.slice(0, 30),
    );
    shape(
      "title",
      {},
      group,
      node.label + (node.path ? " · " + node.path : ""),
    );
    group.addEventListener("click", () => inspect(node));
    group.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        inspect(node);
      }
      const delta = {
        ArrowLeft: [-10, 0],
        ArrowRight: [10, 0],
        ArrowUp: [0, -10],
        ArrowDown: [0, 10],
      }[event.key];
      if (delta) {
        event.preventDefault();
        node.x = Math.max(0, node.x + delta[0]);
        node.y = Math.max(0, node.y + delta[1]);
        updatePositions();
        persist();
      }
    });
    group.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      event.stopPropagation();
      inspect(node);
      group.focus();
      const point = coordinates(event);
      drag = { node, x: point.x - node.x, y: point.y - node.y };
      svg.setPointerCapture(event.pointerId);
    });
  });
  function coordinates(event) {
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    return point.matrixTransform(svg.getScreenCTM().inverse());
  }
  svg.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    const point = coordinates(event);
    drag = {
      x: event.clientX,
      y: event.clientY,
      startX: view.x,
      startY: view.y,
      matrix: svg.getScreenCTM().inverse(),
    };
    svg.setPointerCapture(event.pointerId);
  });
  svg.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const point = coordinates(event);
    if (drag.node) {
      drag.node.x = Math.min(10000, Math.max(0, point.x - drag.x));
      drag.node.y = Math.min(10000, Math.max(0, point.y - drag.y));
      updatePositions();
    } else {
      view.x = drag.startX + (drag.x - event.clientX) * drag.matrix.a;
      view.y = drag.startY + (drag.y - event.clientY) * drag.matrix.d;
      updateView();
    }
  });
  svg.addEventListener("pointerup", () => {
    if (drag?.node) persist();
    drag = null;
  });
  svg.addEventListener("pointercancel", () => {
    drag = null;
  });
  toolbar.append(
    search,
    button("Zoom in", () => zoom(0.8)),
    button("Zoom out", () => zoom(1.25)),
    button("Fit graph", () => {
      view = {
        x: 0,
        y: 0,
        width: Math.max(760, ...nodes.map((node) => node.x + 250)),
        height: Math.max(height, ...nodes.map((node) => node.y + 70)),
      };
      updateView();
    }),
    button("Auto arrange", () => {
      arrange();
      updatePositions();
      persist();
      view = { x: 0, y: 0, width: 760, height };
      updateView();
    }),
    button("Export assignments", () =>
      download("acira-assignments.json", {
        project: project.name,
        agents: Object.fromEntries(
          Object.entries(configuration.document.agents).map(
            ([name, profile]) => [
              name,
              {
                adapter: profile.adapter,
                policies: profile.policies,
                disabled_policies: profile.disabled_policies || [],
                decisions: profile.decisions || {},
              },
            ],
          ),
        ),
      }),
    ),
  );
  function filter() {
    const query = search.value.toLowerCase();
    svg.querySelectorAll("[data-node]").forEach((group) => {
      const node = nodes[Number(group.dataset.node)];
      group.style.opacity =
        !query ||
        (node.label + " " + (node.path || "")).toLowerCase().includes(query)
          ? "1"
          : ".3";
    });
    assignmentTable.replaceChildren(
      table(
        ["Agent", "Policy file", "Assignment", "Inspect"],
        groups.flatMap((group) =>
          group.policies
            .filter(
              (policy) =>
                !query ||
                (group.agent.name + " " + policy.name + " " + policy.path)
                  .toLowerCase()
                  .includes(query),
            )
            .map((policy) => [
              group.agent.name,
              policyFile(policy.path),
              group.agent.disabled_policies?.includes(policy.id)
                ? "Disabled"
                : "Enabled",
              button(
                "Inspect " + policy.name + " for " + group.agent.name,
                () => inspect(resources.get("policy:" + policy.path)),
              ),
            ]),
        ),
      ),
    );
  }
  search.oninput = filter;
  const library = el("details"),
    libraryBody = el("div");
  library.append(
    el("summary", "Copy an installed policy to another agent"),
    libraryBody,
  );
  const target = el("select"),
    policyChoice = el("select"),
    scope = el("p", undefined, "muted");
  target.setAttribute("aria-label", "Copy destination agent");
  policyChoice.setAttribute("aria-label", "Policy to copy");
  groups.forEach((group) =>
    target.append(option(group.agent.name, group.agent.name)),
  );
  [...resources.values()]
    .filter((node) => node.kind === "policy")
    .forEach((node) =>
      policyChoice.append(option(node.key, node.label + " · " + node.path)),
    );
  const copy = button(
    "Copy policy",
    async () => {
      const resource = resources.get(policyChoice.value),
        group = groups.find((group) => group.agent.name === target.value);
      if (!resource) throw new Error("Choose an installed policy first.");
      await api(endpoint + "/policies", {
        agent: group.agent.name,
        filename: resource.path.split(/[\\/]/).pop(),
        document: resource.policy.document,
        expected: null,
      });
      $("dialog").close();
      notice("Policy copy saved to " + group.agent.name + "'s folder.");
      await refresh();
      await render();
    },
    "primary",
  );
  function copyScope() {
    const resource = resources.get(policyChoice.value),
      group = groups.find((group) => group.agent.name === target.value);
    const kind = resource?.policy.kind;
    const owners = groups
      .filter(
        (item) =>
          item.agent.policy_roots[kind] === group.agent.policy_roots[kind],
      )
      .map((item) => item.agent.name);
    scope.textContent =
      "Writes into the selected agent's " +
      (kind || "policy") +
      " folder. Affected agents: " +
      owners.join(", ");
    copy.disabled =
      !resource ||
      group.policies.some((policy) => policy.id === resource.policy.id);
  }
  target.onchange = copyScope;
  policyChoice.onchange = copyScope;
  copyScope();
  libraryBody.append(target, policyChoice, scope, copy);
  body.append(
    el(
      "p",
      "Select a node to edit. Drag nodes or use arrow keys to arrange the view. Dashed connections are disabled assignments.",
    ),
    toolbar,
    svg,
    inspector,
    assignmentTable,
    library,
    button("Advanced project JSON", configurationDialog),
  );
  filter();
  updateView();
  if (nodes.length) inspect(nodes[0]);
}

async function folderDialog(agent) {
  const endpoint = base(),
    project = selected(),
    data = await api(endpoint + "/configuration"),
    policies = await api(
      endpoint + "/policies?agent=" + encodeURIComponent(agent),
    ),
    body = dialog("Policy folders · " + project.name + " / " + agent),
    fields = {};
  const profile = data.document.agents[agent],
    original = { ...profile.policies };
  body.append(
    el(
      "p",
      "Choose project-relative folders or existing registered external folders. Sharing a folder makes its file edits affect every assigned agent. Assignments and decision overrides for a replaced folder are reset. The original files remain in their folder.",
    ),
  );
  for (const kind of ["general", "specific"]) {
    const label = el(
        "label",
        kind === "general" ? "General policy folder" : "Specific policy folder",
      ),
      input = el("input");
    input.value = profile.policies[kind];
    label.append(input);
    fields[kind] = input;
    body.append(label);
    const roots = [
      ...new Set(
        Object.values(data.document.agents).map((item) => item.policies[kind]),
      ),
    ];
    const select = el("select");
    select.setAttribute("aria-label", "Use existing " + kind + " folder");
    select.append(option("", "Choose an existing folder"));
    roots.forEach((root) => select.append(option(root, root)));
    select.onchange = () => {
      if (select.value) input.value = select.value;
    };
    body.append(select);
  }
  body.append(
    button(
      "Save policy folders",
      async () => {
        const removed = new Set(
          policies.policies
            .filter(
              (policy) =>
                fields[policy.kind].value.trim() !== original[policy.kind],
            )
            .map((policy) => policy.id),
        );
        profile.policies = Object.fromEntries(
          Object.entries(fields).map(([kind, input]) => [
            kind,
            input.value.trim(),
          ]),
        );
        profile.disabled_policies = (profile.disabled_policies || []).filter(
          (id) => !removed.has(id),
        );
        for (const id of removed) delete (profile.decisions || {})[id];
        await api(
          endpoint + "/configuration",
          { document: data.document, expected: data.hash },
          "PUT",
        );
        $("dialog").close();
        notice("Policy folders saved for " + agent + ".");
        await refresh();
        await render();
      },
      "primary",
    ),
  );
}

async function activity(content) {
  const endpoint = base(),
    bar = el("div", undefined, "toolbar"),
    search = el("input"),
    run = el("select");
  search.placeholder = "Search recorded activity";
  search.setAttribute("aria-label", "Filter activity");
  run.setAttribute("aria-label", "Filter activity by run");
  run.append(option("", "All runs"));
  selected().runs.forEach((item) =>
    run.append(
      option(item.run_id, item.agent + " · " + item.run_id.slice(0, 16)),
    ),
  );
  const list = el("div");
  let cursor = null,
    rows = [];
  const params = () =>
    new URLSearchParams({
      order: "newest",
      limit: "100",
      q: search.value,
      ...(run.value ? { run: run.value } : {}),
    });
  const older = button("Load older events", () => load(true));
  async function load(append = false) {
    const query = params();
    if (append && cursor) query.set("before", cursor);
    const data = await api(endpoint + "/events?" + query);
    rows = append ? rows.concat(data.events) : data.events;
    cursor = data.next_cursor;
    list.replaceChildren(
      rows.length
        ? table(
            ["Time", "Event", "Agent / run", "Details"],
            rows.map((event) => [
              new Date(event.created_at * 1000).toLocaleString(),
              event.type,
              event.agent || event.run_id,
              button("Inspect", () =>
                dialog("Recorded outcome").append(pretty(event)),
              ),
            ]),
          )
        : empty(
            "No matching activity",
            "Run an Acira-enabled agent. Local history is recorded even with the dashboard closed.",
          ),
    );
    older.hidden = !data.has_more;
  }
  bar.append(
    el("h2", selected().name + " · Recorded activity"),
    search,
    run,
    button("Search", () => load()),
    button("Export matching history", () => {
      const link = el("a");
      link.href = endpoint + "/events/export?" + params();
      link.download = "acira-activity.jsonl";
      link.click();
    }),
  );
  search.addEventListener("keydown", (event) => {
    if (event.key === "Enter")
      load().catch((error) => notice(error.message, true));
  });
  run.onchange = () => load().catch((error) => notice(error.message, true));
  content.append(
    bar,
    el(
      "p",
      "Newest events first. Export includes all retained events matching these filters.",
      "muted",
    ),
    list,
    older,
  );
  await load();
}

function download(name, data) {
  const url = URL.createObjectURL(
      new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
    ),
    a = el("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function backtesting(content) {
  const endpoint = base(),
    agent = state.agent,
    data = await api(
      endpoint + "/revisions?agent=" + encodeURIComponent(agent),
    );
  content.append(
    el("h2", "Compare policies against recorded actions"),
    el(
      "p",
      "Missing evidence produces an indeterminate result. Replay does not predict later agent behavior.",
      "muted",
    ),
  );
  const bar = el("div", undefined, "toolbar"),
    baseline = el("select"),
    candidate = el("select"),
    run = el("select");
  for (const select of [baseline, candidate]) {
    select.append(option("current", "Current policies"));
    data.revisions.forEach((r) =>
      select.append(
        option(
          r.revision,
          new Date(r.created_at * 1000).toLocaleString() +
            " · " +
            r.revision.slice(0, 8),
        ),
      ),
    );
  }
  let revisionOffset = data.next_offset;
  const olderRevisions = button("Load older policy revisions", async () => {
    const page = await api(
      endpoint +
        "/revisions?" +
        new URLSearchParams({ agent, offset: String(revisionOffset) }),
    );
    for (const select of [baseline, candidate]) {
      const known = new Set([...select.options].map((item) => item.value));
      for (const revision of page.revisions) {
        if (!known.has(revision.revision))
          select.append(
            option(
              revision.revision,
              new Date(revision.created_at * 1000).toLocaleString() +
                " · " +
                revision.revision.slice(0, 8),
            ),
          );
      }
    }
    revisionOffset = page.next_offset;
    olderRevisions.hidden = revisionOffset === null;
    notice("Older revisions are available in both comparison selectors.");
  });
  olderRevisions.hidden = revisionOffset === null;
  if (data.revisions.length > 1) baseline.value = data.revisions[1].revision;
  baseline.setAttribute("aria-label", "Baseline revision");
  candidate.setAttribute("aria-label", "Candidate revision");
  run.setAttribute("aria-label", "Compare actions from run");
  run.append(option("", "All runs for this agent"));
  selected()
    .runs.filter((item) => item.agent === agent)
    .forEach((item) => run.append(option(item.run_id, item.run_id)));
  const results = el("div"),
    filter = el("select");
  filter.setAttribute("aria-label", "Filter comparison results");
  [
    ["all", "All results"],
    ["changed", "Changed decisions"],
    ["indeterminate", "Insufficient evidence"],
  ].forEach(([v, t]) => filter.append(option(v, t)));
  let rows = [],
    request = null,
    cursor = null,
    through = null;
  function draw() {
    const visible = rows.filter(
      (row) =>
        filter.value === "all" ||
        (filter.value === "changed"
          ? row.changed
          : row.candidate.decision === "indeterminate"),
    );
    results.replaceChildren(
      el(
        "p",
        rows.length +
          " recorded actions loaded · " +
          rows.filter((r) => r.changed).length +
          " changed",
      ),
      table(
        ["Action", "Run", "Baseline", "Candidate", "Explanation"],
        visible.map((row) => [
          row.action,
          row.run_id,
          row.baseline.decision,
          row.candidate.decision,
          detail(row.candidate.reason, row),
        ]),
      ),
    );
    more.hidden = cursor === null;
  }
  async function load(reset = false) {
    if (reset) {
      request = {
        agent,
        baseline: baseline.value,
        candidate: candidate.value,
        ...(run.value ? { run: run.value } : {}),
      };
      rows = [];
      cursor = null;
      through = null;
    }
    const result = await api(endpoint + "/backtest", {
      ...request,
      after: cursor || 0,
      ...(through === null ? {} : { through }),
    });
    rows.push(...result.results);
    cursor = result.next_cursor;
    through = result.through;
    draw();
  }
  const more = button("Compare next 500 actions", () => load()),
    exportButton = button("Export loaded comparison", () =>
      download("acira-comparison.json", {
        request,
        through,
        results: rows,
        next_cursor: cursor,
      }),
    );
  more.hidden = true;
  filter.onchange = draw;
  bar.append(
    baseline,
    el("span", "→"),
    candidate,
    run,
    button("Run comparison", () => load(true), "primary"),
  );
  content.append(bar, olderRevisions, filter, results, more, exportButton);
}

async function configurationDialog() {
  const data = await api(base() + "/configuration"),
    body = dialog("Agent configurations · " + selected().name),
    editor = el("textarea", undefined, "code");
  editor.value = JSON.stringify(data.document, null, 2);
  editor.setAttribute("aria-label", "Project configuration JSON");
  body.append(
    el(
      "p",
      "Define agent profiles, policy folders, disabled policy IDs, risk overrides, and secret configuration references. External folders must first be registered through acira init.",
    ),
    editor,
    button(
      "Validate and save",
      async () => {
        await api(
          base() + "/configuration",
          { document: JSON.parse(editor.value), expected: data.hash },
          "PUT",
        );
        $("dialog").close();
        notice(
          "Configuration saved. Adapter changes require restarting the affected agent.",
        );
        await refresh();
        await render();
      },
      "primary",
    ),
  );
}
async function settings(content) {
  const appearance = el("section", undefined, "settings-group");
  const preference = el("label", "Companions");
  const mode = el("select");
  mode.setAttribute("aria-label", "Companion appearance");
  for (const [value, label] of [["animated", "Animated"], ["still", "Still"], ["hidden", "Hidden"]])
    mode.append(option(value, label));
  mode.value = AciraReview.appearance();
  mode.onchange = () => AciraReview.appearance(mode.value);
  preference.append(mode);
  appearance.append(el("h2", "Appearance"), preference);
  content.append(appearance);
  const preferences = el("div", undefined, "actions");
  preferences.append(button("Add agent", addAgentDialog), button("Secret references", secretDialog), button("History retention", retentionDialog));
  const project = disclosure("Project settings", "project-settings", preferences,
    el("p", "Agent configurations, secret references, and local storage limits for " + selected().name + ".", "muted"),
    disclosure("Project folder", "settings-folder", el("pre", selected().root)));
  project.classList.add("settings-group");
  content.append(project);
  const pairingAction = el("div", undefined, "actions");
  pairingAction.append(button("Pair capture extension", async () => {
    const pairing = await api("/api/pairing", {}), body = dialog("Pair Acira Capture");
    body.append(el("p", "Open the extension’s connection settings and enter this local address and pairing code. The code expires in two minutes."),
      pretty(pairing), el("p", "The extension can list destinations and import captures. It cannot approve actions or read secrets."));
  }));
  const data = await api("/api/extensions");
  const extensions = disclosure("Capture extension" + (data.extensions.length ? ` (${data.extensions.length} paired)` : ""), "extensions", pairingAction);
  extensions.classList.add("settings-group");
  if (data.extensions.length) {
    extensions.open = true;
    extensions.append(table(["Name", "Paired", "Access"], data.extensions.map((extension) => [extension.name,
      new Date(extension.created_at * 1000).toLocaleString(), button("Revoke", async () => {
        await api("/api/extensions/" + extension.id, undefined, "DELETE"); await render();
      }, "danger")] )));
  }
  content.append(extensions);
  const grants = await api(base() + "/grants");
  const permissions = disclosure("Reusable approvals" + (grants.grants.length ? ` (${grants.grants.length} active)` : ""), "grants");
  permissions.classList.add("settings-group");
  if (!grants.grants.length) permissions.append(el("p", "No reusable approvals are active.", "muted"));
  else {
    permissions.open = true;
    permissions.append(table(["Run", "Scope", "Expires", "Access"], grants.grants.map((grant) => [grant.run,
      detail("Exact scope", grant.scope), new Date(grant.expires_at * 1000).toLocaleTimeString(),
      button("Revoke grant", async () => { await api(base() + "/grants/" + grant.id, undefined, "DELETE"); await render(); }, "danger")] )));
  }
  content.append(permissions);
  const advancedActions = el("div", undefined, "actions");
  advancedActions.append(button("Advanced configuration", configurationDialog));
  const advanced = disclosure("Advanced settings", "advanced-settings", advancedActions, detail("Runtime diagnostics", selected()));
  advanced.classList.add("settings-group");
  content.append(advanced);
}


async function boot() {
  try {
    const fragment = new URLSearchParams(location.hash.slice(1)),
      ticket = fragment.get("ticket");
    if (ticket) {
      history.replaceState(null, "", location.pathname);
      await api("/api/session", { ticket });
    }
    await refresh();
    await render();
  } catch (error) {
    $("connection").textContent = "Disconnected";
    $("content").replaceChildren(
      empty(
        "Open your local workspace",
        "Run acira open from a terminal with Acira installed.",
      ),
    );
    notice(error.message, true);
  }
}
boot();
setInterval(async () => {
  if (state.busy) return;
  try {
    if ($("dialog").open) {
      const data = await api("/api/projects");
      state.projects = data.projects;
      await refreshOpenReview();
      return;
    }
    await refresh();
    const focused = document.activeElement;
    if (state.view === "Approvals") {
      // Preserve focus only on the same request, never on a newly arrived one.
      const reviewId = focused?.dataset.reviewId;
      await render();
      if (reviewId) {
        const same = Array.from($("content").querySelectorAll("[data-review-id]"))
          .find((node) => node.dataset.reviewId === reviewId);
        (reachableControl(same) ? same : $("refresh")).focus();
      }
      return;
    }
    if (state.view === "Agents") {
      const { runId, projectId, runAction, agentName } = focused?.dataset || {};
      await render();
      if (runId) {
        const same = Array.from($("content").querySelectorAll("[data-run-id]"))
          .find((node) => node.dataset.runId === runId && node.dataset.projectId === projectId && node.dataset.runAction === runAction);
        // A changed control (Resume -> Pause) or ended run must not inherit focus.
        (reachableControl(same) ? same : $("refresh")).focus();
      } else if (agentName) {
        const same = Array.from($("content").querySelectorAll("[data-agent-name]"))
          .find((node) => node.dataset.projectId === projectId && node.dataset.agentName === agentName);
        (reachableControl(same) ? same : $("refresh")).focus();
      }
    }
  } catch (error) {
    $("connection").textContent =
      "Disconnected · local SDK enforcement continues";
  }
}, 5000);

async function assignmentDialog() {
  const data = await api(base() + "/configuration"),
    policies = await api(scoped()),
    body = dialog("Assignments · " + contextLine());
  const profile = data.document.agents[state.agent];
  profile.disabled_policies ||= [];
  profile.decisions ||= {};
  body.append(
    el(
      "p",
      "These choices apply only to this agent configuration. Shared policy files are unchanged.",
    ),
  );
  for (const policy of policies.policies) {
    const section = el("section", undefined, "card"),
      label = el("label", undefined, "checkbox"),
      enabled = el("input");
    enabled.type = "checkbox";
    enabled.checked = !profile.disabled_policies.includes(policy.id);
    label.append(enabled, document.createTextNode(policy.name));
    enabled.onchange = () => {
      profile.disabled_policies = profile.disabled_policies.filter(
        (id) => id !== policy.id,
      );
      if (!enabled.checked) profile.disabled_policies.push(policy.id);
    };
    section.append(label);
    if (policy.kind === "general")
      for (const risk of policy.document.risks || []) {
        const id = risk.id || risk.risk,
          row = el("div", undefined, "risk-row"),
          choice = el("select");
        choice.setAttribute("aria-label", policy.name + " · " + id);
        choice.append(option("inherit", "Use policy default"));
        ["allow", "ask", "block"].forEach((value) =>
          choice.append(option(value, value)),
        );
        choice.value = profile.decisions[policy.id]?.[id] || "inherit";
        choice.onchange = () => {
          profile.decisions[policy.id] ||= {};
          if (choice.value === "inherit")
            delete profile.decisions[policy.id][id];
          else profile.decisions[policy.id][id] = choice.value;
        };
        row.append(el("span", id), choice);
        section.append(row);
      }
    body.append(section);
  }
  body.append(
    button(
      "Save assignments",
      async () => {
        await api(
          base() + "/configuration",
          { document: data.document, expected: data.hash },
          "PUT",
        );
        $("dialog").close();
        notice(
          "Assignments saved for " +
            state.agent +
            ". Other agents keep their own choices.",
        );
        await refresh();
        await render();
      },
      "primary",
    ),
  );
}
async function addAgentDialog() {
  const data = await api(base() + "/configuration"),
    body = dialog("Add agent · " + selected().name),
    name = el("input"),
    adapter = el("select");
  name.placeholder = "checkout-agent";
  name.setAttribute("aria-label", "Agent configuration name");
  adapter.setAttribute("aria-label", "Browser integration");
  adapter.append(
    option("playwright", "Playwright · asynchronous Chromium"),
    option("browser_use", "browser-use · Chromium"),
  );
  body.append(
    el(
      "p",
      "Each agent gets separate policy folders. You can assign shared folders in advanced settings.",
    ),
    name,
    adapter,
    button(
      "Create agent",
      async () => {
        if (
          !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(name.value) ||
          data.document.agents[name.value]
        )
          throw new Error(
            "Choose a unique agent name using letters, numbers, dots, hyphens, or underscores.",
          );
        data.document.agents[name.value] = {
          adapter: adapter.value,
          policies: {
            general: "policies/" + name.value + "/general",
            specific: "policies/" + name.value + "/specific",
          },
          decisions: {},
          disabled_policies: [],
        };
        await api(
          base() + "/configuration",
          { document: data.document, expected: data.hash },
          "PUT",
        );
        state.agent = name.value;
        $("dialog").close();
        notice(
          "Agent created. Use protect(agent=" +
            JSON.stringify(name.value) +
            ") in that workflow, then add its policies here.",
        );
        await refresh();
        await render();
      },
      "primary",
    ),
  );
}

async function secretDialog() {
  const data = await api(
      base() + "/secret-references?agent=" + encodeURIComponent(state.agent),
    ),
    body = dialog("Secret references · " + contextLine());
  body.append(
    el(
      "p",
      "Manage names and permissions here. Values remain in the project’s private .env file and are resolved only for a supported just-in-time secret operation.",
    ),
  );
  if (
    data.providers.some(
      (provider) =>
        provider.kind !== "env_file" || provider.id !== "project-env",
    )
  ) {
    body.append(
      el(
        "p",
        "This agent uses an existing secret provider. Its references are shown below; keep managing that provider through its configuration file.",
      ),
      pretty(data),
    );
    return;
  }
  const entries = data.secrets.map(
    ({ id, label, ref, active, tags, classification, policy }) => ({
      id,
      label,
      ref,
      active,
      tags,
      classification,
      policy,
    }),
  );
  const enabled = el("input"),
    label = el("label", undefined, "checkbox");
  enabled.type = "checkbox";
  enabled.checked = data.enabled;
  label.append(
    enabled,
    document.createTextNode("Enable secret references for this agent"),
  );
  body.append(label);
  const rows = el("div");
  body.append(rows);
  function draw() {
    rows.replaceChildren();
    for (const entry of entries) {
      const card = el("div", undefined, "card");
      for (const [key, title] of [
        ["id", "Reference ID"],
        ["label", "Display label"],
        ["ref", "Environment variable name"],
      ]) {
        const label = el("label", title),
          input = el("input");
        input.value = entry[key] || "";
        input.oninput = () => (entry[key] = input.value);
        label.append(input);
        card.append(label);
      }
      const active = el("input"),
        activeLabel = el("label", undefined, "checkbox");
      active.type = "checkbox";
      active.checked = entry.active;
      active.onchange = () => (entry.active = active.checked);
      activeLabel.append(active, document.createTextNode("Active"));
      card.append(
        activeLabel,
        button(
          "Remove reference",
          () => {
            entries.splice(entries.indexOf(entry), 1);
            draw();
          },
          "danger",
        ),
      );
      rows.append(card);
    }
  }
  draw();
  body.append(
    button("Add reference", () => {
      entries.push({
        id: "",
        label: "",
        ref: "",
        active: false,
        policy: { decision: "ask" },
      });
      draw();
    }),
    button(
      "Save references",
      async () => {
        await api(
          base() + "/secret-references",
          {
            agent: state.agent,
            expected: data.configuration_hash,
            enabled: enabled.checked,
            entries,
          },
          "PUT",
        );
        $("dialog").close();
        notice("References saved. Secret values were not read or transferred.");
        await refresh();
        await render();
      },
      "primary",
    ),
  );
}

function jsonChanges(before, after, path = "", rows = []) {
  if (rows.length >= 500) return rows;
  if (JSON.stringify(before) === JSON.stringify(after)) return rows;
  if (
    before &&
    after &&
    typeof before === "object" &&
    typeof after === "object"
  ) {
    for (const key of new Set([...Object.keys(before), ...Object.keys(after)]))
      jsonChanges(before[key], after[key], path + " / " + key, rows);
  } else
    rows.push([
      path,
      JSON.stringify(before) ?? "Absent",
      JSON.stringify(after) ?? "Absent",
    ]);
  return rows;
}

async function retentionDialog() {
  const data = await api(base() + "/configuration"),
    body = dialog("History retention · " + selected().name);
  const retention = {
    audit_days: 30,
    image_days: 7,
    audit_bytes: 1073741824,
    image_bytes: 262144000,
    ...data.document.retention,
  };
  body.append(
    el(
      "p",
      "Expired records and images are removed locally. Lowering limits can remove older history at the next maintenance pass.",
    ),
  );
  for (const [key, title, unit] of [
    ["audit_days", "Audit history · days", 1],
    ["image_days", "Review images · days", 1],
    ["audit_bytes", "Audit records · MiB", 1048576],
    ["image_bytes", "Review images · MiB", 1048576],
  ]) {
    const label = el("label", title),
      input = el("input");
    input.type = "number";
    input.min = "1";
    input.step = "1";
    input.value = retention[key] / unit;
    input.oninput = () => (retention[key] = Number(input.value) * unit);
    label.append(input);
    body.append(label);
  }
  body.append(
    button(
      "Save retention",
      async () => {
        data.document.retention = retention;
        await api(
          base() + "/configuration",
          { document: data.document, expected: data.hash },
          "PUT",
        );
        $("dialog").close();
        notice(
          "Retention saved. Existing history is pruned during local maintenance.",
        );
      },
      "primary",
    ),
  );
}

async function remoteCatalogDialog() {
  const body = dialog("Import from catalog · " + contextLine()),
    url = el("input"),
    name = el("input");
  url.type = "url";
  url.placeholder = "https://…/catalog.json";
  url.setAttribute("aria-label", "HTTPS catalog URL");
  name.placeholder = "Policy name";
  name.setAttribute("aria-label", "Catalog policy name");
  const preview = el("div");
  body.append(
    el(
      "p",
      "Acira downloads a declarative policy only when you request it. Review it here before saving to your project.",
    ),
    url,
    name,
    button("Fetch preview", async () => {
      const result = await api("/api/catalog/preview", {
        url: url.value,
        name: name.value,
      });
      preview.replaceChildren(
        pretty(result.document),
        el(
          "p",
          "Affected agents: " +
            affected(
              result.document.schema.startsWith("acira.general_skill")
                ? "general"
                : "specific",
            ).join(", "),
        ),
        button(
          "Install this policy",
          async () => {
            await api(base() + "/policies", {
              agent: state.agent,
              ...result,
              expected: null,
            });
            $("dialog").close();
            notice("Catalog policy pinned locally. Updates remain explicit.");
            await refresh();
            await render();
          },
          "primary",
        ),
      );
    }),
    preview,
  );
}
async function updatesDialog() {
  const body = dialog("Policy updates · " + contextLine());
  body.append(el("p", "Checking explicitly configured catalogs…"));
  const data = await api(base() + "/policy-updates", { agent: state.agent });
  body.replaceChildren();
  if (!data.updates.length) {
    body.append(
      el(
        "p",
        "No updates are available from the installed package or pinned catalogs.",
      ),
    );
    return;
  }
  for (const policy of data.updates) {
    const card = el("section", undefined, "card");
    card.append(
      el("h3", policy.name),
      el("p", "Affected agents: " + policy.affected_agents.join(", ")),
      table(
        ["Changed path", "Current", "Proposed"],
        jsonChanges(policy.document, policy.proposed),
      ),
      button(
        "Accept this update",
        async () => {
          await api(base() + "/policies", {
            agent: state.agent,
            filename: policy.path.split(/[\\/]/).pop(),
            document: policy.proposed,
            expected: policy.hash,
          });
          card.replaceChildren(el("p", policy.name + " · Saved"));
          await refresh();
        },
        "primary",
      ),
    );
    body.append(card);
  }
}
