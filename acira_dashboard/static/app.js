const token = document.querySelector('meta[name="acira-dashboard-token"]').content;
      const state = {
        status: null, events: [], sessions: [], reviews: [], pendingReviews: [], configs: [], secretConfigs: [],
        generalSkills: [], specificSkills: [], mergedSkills: [], currentSession: null, currentSessionEvents: [], currentSessionReviews: [], currentSessionSecrets: [],
        currentReview: null, currentReviewArtifact: null, currentGraph: null, selectedGraphNodeId: "config", currentSpecificSkill: null, currentSpecificTab: "targets", currentSpecificMode: "empty", currentMergedSkill: null, currentMergedTab: "components", currentConfigTab: "graph", currentConfigMode: "empty", currentSecretConfig: null, currentSecretTab: "settings", currentSecretRecordId: null,
        sessionFilter: "live", sessionFeedFilter: "all", reviewFilter: "pending", skillErrors: {}, notifiedReviewIds: new Set(), reviewTrailIds: new Set(),
        graphUserScale: 1, graphUserPanX: 0, graphUserPanY: 0,
        currentConfigOriginalName: null
      };
      const byId = (id) => document.getElementById(id);
      const els = {
        liveDot: byId("live-dot"), liveLabel: byId("live-label"), title: byId("view-title"), toast: byId("toast"), notificationStack: byId("notification-stack"),
        dashboardEvents: byId("dashboard-events"), eventList: byId("event-list"), eventFilter: byId("event-filter"), eventSearch: byId("event-search"),
        dashboardError: byId("dashboard-error"), sessionError: byId("session-error"), reviewError: byId("review-error"), secretError: byId("secret-error"),
        sessionList: byId("session-list"), sessionDetail: byId("session-detail"), reviewList: byId("review-list"), reviewDetail: byId("review-detail"),
        generalList: byId("general-list"), generalDetail: byId("general-detail"), specificList: byId("specific-list"), specificName: byId("specific-name"),
        specificEditor: byId("specific-editor"), specificState: byId("specific-state"), specificGui: byId("specific-gui"), mergedList: byId("merged-list"),
        specificTargetsPane: byId("specific-targets-pane"), specificJsonPane: byId("specific-json-pane"), specificImportPane: byId("specific-import-pane"), specificImportEditor: byId("specific-import-editor"),
        mergedName: byId("merged-name"), mergedEditor: byId("merged-editor"), mergedState: byId("merged-state"), mergedComponentsPane: byId("merged-components-pane"),
        mergedJsonPane: byId("merged-json-pane"), mergedComponents: byId("merged-components"), mergedPicker: byId("merged-picker"), mergedSkillSearch: byId("merged-skill-search"), mergedCompiled: byId("merged-compiled"), configList: byId("config-list"),
        configName: byId("config-name"), configEditor: byId("config-editor"), configState: byId("config-state"), graph: byId("config-graph"),
        graphSkillSearch: byId("graph-skill-search"), graphSkillLibrary: byId("graph-skill-library"), graphInspector: byId("graph-inspector"), secretConfigList: byId("secret-config-list"), secretConfigName: byId("secret-config-name"),
        secretConfigEditor: byId("secret-config-editor"), secretConfigState: byId("secret-config-state"), secretRecordList: byId("secret-record-list"),
        secretConfigIndex: byId("secret-config-index"), secretConfigDetail: byId("secret-config-detail"), secretSettingsPane: byId("secret-settings-pane"), secretJsonPane: byId("secret-json-pane"),
        secretConfigEnabled: byId("secret-config-enabled"), secretDefaultDecision: byId("secret-default-decision"), secretRecordCount: byId("secret-record-count"),
        secretReadonlyBanner: byId("secret-readonly-banner"), secretEditorTitle: byId("secret-editor-title"), secretEditorHelp: byId("secret-editor-help"),
        backtestConfig: byId("backtest-config"), backtestSkillMode: byId("backtest-skill-mode")
      };
      const progress = (() => {
        let count = 0;
        let hideTimer = null;
        let watchdog = null;
        const el = () => document.getElementById("acira-progress");
        const hide = () => {
          const bar = el();
          if (bar) bar.classList.remove("active");
          document.querySelectorAll(".refresh-btn.is-busy").forEach((b) => { b.classList.remove("is-busy"); clearTimeout(b._busyTimer); });
        };
        return {
          start() {
            count += 1;
            const bar = el();
            if (bar) { clearTimeout(hideTimer); bar.classList.add("active"); }
            clearTimeout(watchdog);
            watchdog = setTimeout(() => { count = 0; hide(); }, 10000);
          },
          done() {
            count = Math.max(0, count - 1);
            if (count > 0) return;
            clearTimeout(watchdog);
            clearTimeout(hideTimer);
            hideTimer = setTimeout(hide, 220);
          }
        };
      })();
      const api = async (path, options = {}) => {
        const showBar = !options.silent;
        if (showBar) progress.start();
        try {
          const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
          if (options.method && options.method !== "GET") headers["X-Acira-Dashboard-Token"] = token;
          const response = await fetch(path, Object.assign({}, options, { headers }));
          const text = await response.text();
          let data = {};
          if (text) {
            try { data = JSON.parse(text); }
            catch { data = { error: text }; }
          }
          if (!response.ok) throw new Error(data.error || response.statusText);
          return data;
        } finally {
          if (showBar) progress.done();
        }
      };
      function showConfirmModal(title, body, { okLabel = "Confirm", okClass = "danger" } = {}) {
        return new Promise((resolve) => {
          const backdrop = byId("confirm-modal");
          byId("confirm-modal-title").textContent = title;
          byId("confirm-modal-body").textContent = body;
          const okBtn = byId("confirm-modal-ok");
          const cancelBtn = byId("confirm-modal-cancel");
          okBtn.textContent = okLabel;
          okBtn.className = `button ${okClass}`;
          backdrop.classList.remove("hidden");
          const cleanup = (result) => {
            backdrop.classList.add("hidden");
            okBtn.removeEventListener("click", onOk);
            cancelBtn.removeEventListener("click", onCancel);
            backdrop.removeEventListener("click", onBackdrop);
            resolve(result);
          };
          const onOk = () => cleanup(true);
          const onCancel = () => cleanup(false);
          const onBackdrop = (e) => { if (e.target === backdrop) cleanup(false); };
          okBtn.addEventListener("click", onOk);
          cancelBtn.addEventListener("click", onCancel);
          backdrop.addEventListener("click", onBackdrop);
        });
      }
      const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
      const badge = (text, cls = "") => `<span class="badge ${cls}">${escapeHtml(text)}</span>`;
      const statusClass = (text) => String(text || "").match(/block|deny|expired|invalid/i) ? "danger" : String(text || "").match(/pending|ask|paused|stale|observed/i) ? "warn" : "ok";
      const TOAST_ICON = `<span class="toast-ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6"><path d="m5 12 4 4 10-10"/></svg></span>`;
      const showToast = (message) => {
        els.toast.innerHTML = `${TOAST_ICON}<span class="toast-msg">${escapeHtml(message)}</span>`;
        els.toast.classList.remove("hidden", "show");
        void els.toast.offsetWidth;
        els.toast.classList.add("show");
        clearTimeout(showToast.timer);
        showToast.timer = setTimeout(() => els.toast.classList.add("hidden"), 3200);
      };
      const positiveNumber = (value) => {
        const parsed = Number(value);
        return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
      };
      function showReviewNotification(review) {
        const reviewId = review && review.review_id;
        if (!reviewId || state.notifiedReviewIds.has(reviewId)) return;
        state.notifiedReviewIds.add(reviewId);
        const meta = [review.run_id, review.created_at || review.updated_at].filter(Boolean).join(" | ");
        const source = reviewSourceLabel(review);
        const node = document.createElement("div");
        node.className = "notification hitl-notification";
        node.dataset.notificationReview = reviewId;
        node.innerHTML = `
          <div class="notification-head">
            <div class="notification-icon">!</div>
            <div class="notification-copy">
              <div class="notification-kicker">Human review needed</div>
              <div class="item-title">${escapeHtml(review.summary || "Review the requested browser action")}</div>
              <div class="item-meta">${escapeHtml(meta || source || "Waiting for your decision")}</div>
              ${source && meta ? `<div class="event-source">${escapeHtml(source)}</div>` : ""}
            </div>
          </div>
          <div class="notification-actions">
            <button class="button primary" data-open-notification-review="${escapeHtml(reviewId)}">Review now</button>
            <button class="button" data-dismiss-notification-review="${escapeHtml(reviewId)}">Dismiss</button>
          </div>
        `;
        els.notificationStack.appendChild(node);
      }
      function clearReviewNotification(reviewId) {
        if (!reviewId) return;
        const escaped = window.CSS && CSS.escape ? CSS.escape(reviewId) : String(reviewId).replace(/"/g, '\\"');
        const node = els.notificationStack.querySelector(`[data-notification-review="${escaped}"]`);
        if (node) node.remove();
      }
      function maybeNotifyReviewEvent(event) {
        if (!event || !event.review_id) return;
        const type = String(event.type || "");
        const status = String(event.status || "").toLowerCase();
        if (type === "hitl_review_pending" && status !== "allow" && status !== "block" && status !== "expired") {
          rememberReviewTrail([event]);
          showReviewNotification(event);
        }
        if (type === "hitl_review_decision" || type === "hitl_review_expired" || status === "allow" || status === "block" || status === "expired") {
          clearReviewNotification(event.review_id);
        }
      }
      const eventTitle = (event) => event.summary || event.reason || event.action || event.type || "event";
      const renderJson = (value) => `<pre>${escapeHtml(JSON.stringify(value, null, 2))}</pre>`;
      const selectedName = (list, fallback = "") => (list[0] && list[0].name) || fallback;
      const normalizeJsonName = (name, fallback) => {
        const raw = String(name || fallback || "").trim();
        const base = raw || fallback || "dashboard.json";
        return base.toLowerCase().endsWith(".json") ? base : `${base}.json`;
      };
      const uniqueJsonName = (stem, list) => {
        const existing = new Set((Array.isArray(list) ? list : []).map((item) => item.name));
        const base = normalizeJsonName(stem || "dashboard", "dashboard.json");
        if (!existing.has(base)) return base;
        const cleanStem = base.replace(/\.json$/i, "");
        for (let index = 2; index < 100; index += 1) {
          const candidate = `${cleanStem}-${index}.json`;
          if (!existing.has(candidate)) return candidate;
        }
        return `${cleanStem}-${Date.now()}.json`;
      };
      const emptyStatus = () => ({
        configs: { count: 0 },
        skills: { general_count: 0, specific_count: 0, merged_count: 0, diagnostics: {} },
        events: { count: 0, active_review_count: 0, run_count: 0, paused_session_count: 0 },
        sessions: { count: 0, live_count: 0, active_count: 0, paused_count: 0, stale_count: 0, ended_count: 0, observed_count: 0, total_count: 0 },
        secrets: { config_count: 0, legacy_count: 0 }
      });
      const normalizeStatus = (raw) => {
        const base = emptyStatus();
        const status = raw && typeof raw === "object" ? raw : {};
        return {
          configs: Object.assign(base.configs, status.configs || {}),
          skills: Object.assign(base.skills, status.skills || {}),
          events: Object.assign(base.events, status.events || {}),
          sessions: Object.assign(base.sessions, status.sessions || {}),
          secrets: Object.assign(base.secrets, status.secrets || {})
        };
      };
      const setInlineError = (target, message) => {
        if (!target) return;
        target.textContent = message || "";
        target.classList.toggle("hidden", !message);
      };
      const formatHeartbeatAge = (seconds) => {
        if (seconds === undefined || seconds === null || Number.isNaN(Number(seconds))) return "";
        const value = Math.max(0, Number(seconds));
        if (value < 2) return "just now";
        if (value < 60) return `${Math.round(value)}s ago`;
        if (value < 3600) return `${Math.round(value / 60)}m ago`;
        if (value < 86400) return `${Math.round(value / 3600)}h ago`;
        return `${Math.round(value / 86400)}d ago`;
      };
      let streamReconnectTimer = null;
      let pollingFallbackTimer = null;
      let reviewArtifactRefreshTimer = null;

      function setLiveState(label, live = false) {
        els.liveDot.classList.toggle("live", live);
        els.liveLabel.textContent = label;
      }

      function showFileModeWarning() {
        setLiveState("Offline", false);
        const message = "Run the dashboard server and open http://127.0.0.1:8765/";
        renderEvents(els.dashboardEvents, []);
        els.dashboardEvents.innerHTML = `<div class="empty">${escapeHtml(message)}</div>`;
        showToast(message);
      }

      function setView(name) {
        document.querySelectorAll(".view").forEach((el) => el.classList.add("hidden"));
        byId(`view-${name}`).classList.remove("hidden");
        document.querySelectorAll("[data-view-button]").forEach((btn) => btn.classList.toggle("active", btn.dataset.viewButton === name));
        const VIEW_TITLES = { dashboard: "Overview", hitl: "Reviews", events: "Activity" };
        els.title.textContent = VIEW_TITLES[name] || (name[0].toUpperCase() + name.slice(1));
        els.title.classList.remove("t-in");
        void els.title.offsetWidth;
        els.title.classList.add("t-in");
        window.scrollTo(0, 0);
        if (name === "sessions") loadSessions();
        if (name === "hitl") loadReviews();
        if (name === "skills") loadSkills();
        if (name === "configs") loadConfigs();
        if (name === "secrets") { showSecretIndex(); loadSecrets(); }
        if (name === "events") loadEvents();
      }

      function renderEvents(target, events) {
        const safeEvents = Array.isArray(events) ? events : [];
        if (!safeEvents.length) { target.innerHTML = '<div class="empty">No events yet.</div>'; return; }
        target.innerHTML = safeEvents.map((event) => {
          const source = eventSourceLabel(event);
          const meta = [event.created_at || event.updated_at, event.run_id, event.framework, event.action || event.type].filter(Boolean).join(" | ");
          const session = event.session_id || event.run_id || "";
          const review = event.review_id || "";
          const attrs = [session ? `data-open-session="${escapeHtml(session)}"` : "", review ? `data-open-review="${escapeHtml(review)}"` : ""].filter(Boolean).join(" ");
          return `<button class="event event-button" ${attrs}><div class="event-title"><span>${escapeHtml(eventTitle(event))}</span>${badge(event.status || event.type, statusClass(event.status || event.type))}</div><div class="event-meta">${escapeHtml(meta)}</div>${source ? `<div class="event-source">${escapeHtml(source)}</div>` : ""}</button>`;
        }).join("");
      }
      function renderDashboardActivity() {
        const urgent = dashboardAttentionItems();
        if (!urgent.length) {
          els.dashboardEvents.innerHTML = '<div class="empty">No urgent requests.</div>';
          return;
        }
        els.dashboardEvents.innerHTML = urgent.map((item) => {
          const attrs = [item.session_id ? `data-open-session="${escapeHtml(item.session_id)}"` : "", item.review_id ? `data-open-review="${escapeHtml(item.review_id)}"` : ""].filter(Boolean).join(" ");
          return `<button class="event event-button" ${attrs}><div class="event-title"><span>${escapeHtml(item.title)}</span>${badge(item.badge, statusClass(item.badge))}</div><div class="event-meta">${escapeHtml(item.meta)}</div>${item.source ? `<div class="event-source">${escapeHtml(item.source)}</div>` : ""}</button>`;
        }).join("");
      }
      function dashboardAttentionItems() {
        const liveIds = new Set(filteredSessionsFor("live").flatMap((session) => [session.session_id, session.run_id]).filter(Boolean));
        const items = [];
        for (const review of Array.isArray(state.pendingReviews) ? state.pendingReviews : []) {
          items.push({
            kind: "review",
            review_id: review.review_id,
            session_id: review.run_id,
            title: review.summary || "Human review needed",
            badge: "pending",
            meta: [review.run_id, review.created_at].filter(Boolean).join(" | "),
            source: reviewSourceLabel(review)
          });
        }
        for (const session of filteredSessionsFor("paused")) {
          items.push({
            kind: "paused",
            session_id: session.session_id,
            title: `Session paused: ${session.session_id}`,
            badge: "paused",
            meta: [session.framework, formatHeartbeatAge(session.heartbeat_age_seconds)].filter(Boolean).join(" | "),
            source: session.pause_reason || "Waiting for resume"
          });
        }
        for (const event of Array.isArray(state.events) ? state.events : []) {
          const status = String(event.status || "").toLowerCase();
          const type = String(event.type || "");
          const sessionId = event.session_id || event.run_id || "";
          if (!sessionId || !liveIds.has(sessionId)) continue;
          if (!(status.match(/block|deny|failed/) || type.match(/backend_failed|blocked/i))) continue;
          items.push({
            kind: "blocked",
            session_id: sessionId,
            review_id: event.review_id,
            title: eventTitle(event),
            badge: status || "blocked",
            meta: [event.created_at || event.updated_at, event.action || type].filter(Boolean).join(" | "),
            source: eventSourceLabel(event)
          });
        }
        return items.slice(-20).reverse();
      }
      function eventSourceLabel(event) {
        const metadata = event.metadata && typeof event.metadata === "object" ? event.metadata : {};
        const cedar = metadata.cedar && typeof metadata.cedar === "object" ? metadata.cedar : {};
        const classification = metadata.classification && typeof metadata.classification === "object" ? metadata.classification : event.classification;
        const details = classification && typeof classification.details === "object" ? classification.details : {};
        const detections = cedar.general_skills && Array.isArray(cedar.general_skills.detections) ? cedar.general_skills.detections : [];
        const detection = detections[0] || {};
        return detection.skill_name || detection.skill_id || cedar.source || metadata.decision || details.source || event.source || "";
      }
      function reviewSourceLabel(review) {
        const classification = review.classification && typeof review.classification === "object" ? review.classification : {};
        const details = classification.details && typeof classification.details === "object" ? classification.details : {};
        const general = details.general_skills && Array.isArray(details.general_skills.detections) ? details.general_skills.detections[0] : null;
        return (general && (general.skill_name || general.skill_id)) || details.source || classification.reason || review.reason || "";
      }

      async function loadStatus(opts = {}) {
        try {
          state.status = normalizeStatus(await api("/api/status", { silent: opts.silent }));
          setInlineError(els.dashboardError, "");
        } catch (err) {
          state.status = normalizeStatus(state.status);
          setInlineError(els.dashboardError, `Status API error: ${err.message || err}`);
          throw err;
        }
        renderStatusMetrics();
      }
      function renderStatusMetrics() {
        state.status = normalizeStatus(state.status);
        byId("metric-reviews").textContent = state.status.events.active_review_count || 0;
        byId("metric-sessions").textContent = state.status.sessions.live_count ?? state.status.sessions.count ?? 0;
        byId("metric-paused").textContent = state.status.sessions.paused_count ?? 0;
        byId("metric-general").textContent = state.status.skills.general_count || 0;
        byId("metric-specific").textContent = state.status.skills.specific_count || 0;
        byId("metric-merged").textContent = state.status.skills.merged_count || 0;
      }
      async function loadEvents(opts = {}) {
        try {
          const data = await api("/api/events?limit=100", { silent: opts.silent });
          state.events = Array.isArray(data.events) ? data.events : [];
          state.events.forEach(maybeNotifyReviewEvent);
          setInlineError(els.dashboardError, "");
          renderDashboardActivity();
          renderEventView();
        } catch (err) {
          setInlineError(els.dashboardError, `Events API error: ${err.message || err}`);
          throw err;
        }
      }
      function renderEventView() {
        const type = els.eventFilter.value;
        const text = els.eventSearch.value.trim().toLowerCase();
        const events = (Array.isArray(state.events) ? state.events : []).filter((event) => !type || event.type === type).filter((event) => !text || JSON.stringify(event).toLowerCase().includes(text)).slice().reverse();
        renderEvents(els.eventList, events);
      }

      async function loadSessions(opts = {}) {
        try {
          const data = await api("/api/sessions", { silent: opts.silent });
          state.sessions = Array.isArray(data.sessions) ? data.sessions : [];
          setInlineError(els.sessionError, "");
          renderSessions();
          renderDashboardActivity();
        } catch (err) {
          setInlineError(els.sessionError, `Sessions API error: ${err.message || err}`);
          renderSessions();
          throw err;
        }
      }
      function renderSessions() {
        document.querySelectorAll("[data-session-filter]").forEach((button) => button.classList.toggle("active", button.dataset.sessionFilter === state.sessionFilter));
        const sessions = filteredSessions();
        els.sessionList.innerHTML = sessions.map((session) => {
          const status = displaySessionStatus(session);
          const heartbeat = formatHeartbeatAge(session.heartbeat_age_seconds);
          const agentName = session.agent_name || session.session_id;
          const meta = [session.agent_name ? session.session_id : null, session.framework, status, heartbeat].filter(Boolean).join(" | ");
          const selected = state.currentSession && state.currentSession.session_id === session.session_id ? " active" : "";
          return `<button class="list-item${selected}" data-session="${escapeHtml(session.session_id)}"><div class="event-title"><span class="item-title">${escapeHtml(agentName)}</span>${badge(status, statusClass(status))}</div><div class="item-meta">${escapeHtml(meta)}</div></button>`;
        }).join("") || `<div class="empty">${escapeHtml(emptySessionMessage())}</div>`;
      }
      function filteredSessions() {
        return filteredSessionsFor(state.sessionFilter);
      }
      function filteredSessionsFor(filter) {
        const sessions = Array.isArray(state.sessions) ? state.sessions : [];
        if (filter === "all") return sessions;
        if (filter === "live") return sessions.filter((session) => session.status === "active" || session.status === "paused");
        return sessions.filter((session) => session.status === filter);
      }
      function emptySessionMessage() {
        if (state.sessionFilter === "live") return "No live sessions running.";
        if (state.sessionFilter === "stale") return "No stale sessions.";
        if (state.sessionFilter === "ended") return "No ended sessions.";
        if (state.sessionFilter === "paused") return "No paused sessions.";
        return "No sessions.";
      }
      function displaySessionStatus(session) {
        if ((session && session.status) === "stale") return "Historical / stale";
        if ((session && session.status) === "observed") return "Historical";
        return (session && session.status) || "unknown";
      }
      async function openSession(id) {
        const data = await api(`/api/sessions/${encodeURIComponent(id)}`);
        state.currentSession = data.session || null;
        state.currentSessionEvents = Array.isArray(data.events) ? data.events : [];
        state.currentSessionReviews = Array.isArray(data.reviews) ? data.reviews : [];
        state.currentSessionSecrets = Array.isArray(data.secrets_used) ? data.secrets_used : [];
        renderSessions();
        renderSessionDetail();
      }
      function renderSessionDetail() {
        const session = state.currentSession || {};
        const events = Array.isArray(state.currentSessionEvents) ? state.currentSessionEvents : [];
        const reviews = Array.isArray(state.currentSessionReviews) ? state.currentSessionReviews : [];
        const secrets = Array.isArray(state.currentSessionSecrets) ? state.currentSessionSecrets : [];
        const status = session.status || "unknown";
        const labelStatus = displaySessionStatus(session);
        const isPaused = Boolean(session.paused) || status === "paused";
        const canControl = status === "active" || status === "paused" || isPaused;
        byId("pause-session").disabled = !canControl || isPaused;
        byId("resume-session").disabled = !isPaused;
        const filteredEvents = filteredSessionEvents(events, state.sessionFeedFilter);
        els.sessionDetail.innerHTML = `
          <div class="row">${badge(labelStatus, statusClass(status))}${session.paused ? badge("paused", "warn") : ""}${session.live ? badge("live", "ok") : ""}</div>
          <h3>${escapeHtml(session.agent_name || session.session_id || "selected session")}</h3>
          <div class="kv-grid">
            <div class="kv"><div class="label">Agent</div><div class="value">${escapeHtml(session.agent_name || "unnamed")}</div></div>
            <div class="kv"><div class="label">Session</div><div class="value">${escapeHtml(session.session_id || "unknown")}</div></div>
            <div class="kv"><div class="label">Framework</div><div class="value">${escapeHtml(session.framework || "unknown")}</div></div>
            <div class="kv"><div class="label">Heartbeat</div><div class="value">${escapeHtml(formatHeartbeatAge(session.heartbeat_age_seconds) || "not available")}</div></div>
            <div class="kv"><div class="label">Started</div><div class="value">${escapeHtml(session.started_at || "unknown")}</div></div>
            <div class="kv"><div class="label">Updated</div><div class="value">${escapeHtml(session.updated_at || "unknown")}</div></div>
          </div>
          <div class="small">Secrets: ${escapeHtml(secrets.map((item) => item.label || item.id).join(", ") || "none")}</div>
          <div class="small">Reviews: ${reviews.length}</div>
          <div class="feed-filters tabs">
            ${["all", "blocked", "hitl", "decisions", "allowed", "secrets"].map((filter) => `<button class="tab ${state.sessionFeedFilter === filter ? "active" : ""}" data-session-feed-filter="${filter}">${escapeHtml(feedFilterLabel(filter))}</button>`).join("")}
          </div>
          <h3 style="margin-top:10px">Action feed</h3>
          ${filteredEvents.length ? "" : '<div class="empty">No matching session events.</div>'}
          <div>${filteredEvents.slice().reverse().map(renderSessionEventRow).join("")}</div>
        `;
      }
      function feedFilterLabel(filter) {
        return { all: "All", blocked: "Blocked", hitl: "HITL", decisions: "Decisions", allowed: "Allowed", secrets: "Secrets" }[filter] || "All";
      }
      function filteredSessionEvents(events, filter) {
        if (filter === "all") return events;
        return events.filter((event) => {
          const type = String(event.type || "").toLowerCase();
          const status = String(event.status || "").toLowerCase();
          const text = JSON.stringify(event).toLowerCase();
          if (filter === "blocked") return status.match(/block|deny|failed/) || type.match(/blocked|backend_failed/);
          if (filter === "hitl") return type.includes("review") || status === "pending" || status === "ask";
          if (filter === "decisions") return type.includes("decision") || status === "allow" || status === "block";
          if (filter === "allowed") return status === "allow" || status === "allowed";
          if (filter === "secrets") return text.includes("secret_id") || text.includes("[redacted:secret]") || text.includes("secret used");
          return true;
        });
      }
      function renderSessionEventRow(event) {
        const meta = [event.created_at || event.updated_at, event.action || event.type, event.review_id].filter(Boolean).join(" | ");
        const source = eventSourceLabel(event) || "Acira runtime";
        const status = event.status || event.type;
        const session = event.session_id || event.run_id || "";
        const review = event.review_id || "";
        const actions = [review ? `<button class="button" data-open-review="${escapeHtml(review)}">Open review</button>` : "", session ? `<button class="button" data-open-session="${escapeHtml(session)}">Open session</button>` : ""].filter(Boolean).join("");
        return `<div class="event"><div class="event-title"><span>${escapeHtml(eventTitle(event))}</span>${badge(status, statusClass(status))}</div><div class="event-meta">${escapeHtml(meta)}</div><div class="event-source">${escapeHtml(source)}</div>${actions ? `<div class="row event-actions">${actions}</div>` : ""}<details class="advanced-panel"><summary>Redacted details</summary>${renderJson(event)}</details></div>`;
      }
      async function pauseOrResume(paused) {
        if (!state.currentSession) throw new Error("Select a session first.");
        const path = `/api/sessions/${encodeURIComponent(state.currentSession.session_id)}/${paused ? "pause" : "resume"}`;
        await api(path, { method: "POST", body: JSON.stringify({ reason: paused ? "Paused from dashboard" : "Resumed from dashboard" }) });
        showToast(paused ? "Session paused." : "Session resumed.");
        await Promise.all([loadSessions(), openSession(state.currentSession.session_id), loadStatus()]);
      }
      async function runBacktestForSession() {
        if (!state.currentSession) throw new Error("Select a session first.");
        renderBacktestConfigOptions();
        byId("backtest-panel").classList.remove("hidden");
        const mode = els.backtestSkillMode.value || "config";
        const request = { session_id: state.currentSession.session_id };
        if (mode === "all") {
          request.skills = {
            general: (state.generalSkills || []).map((skill) => skill.name),
            specific: (state.specificSkills || []).map((skill) => skill.name),
            merged: (state.mergedSkills || []).map((skill) => skill.name)
          };
        } else {
          request.config = els.backtestConfig.value || selectedName(state.configs, "");
        }
        const result = await api("/api/backtests", { method: "POST", body: JSON.stringify(request) });
        byId("backtest-results").innerHTML = result.results.length ? result.results.map((row) => `<div class="event"><div class="event-title"><span>${escapeHtml(row.summary || row.action || "action")}</span>${badge(row.final, statusClass(row.final))}</div><div class="event-meta">general=${escapeHtml(row.general.decision)} | specific=${escapeHtml(row.specific.decision)}</div></div>`).join("") : '<div class="empty">No backtestable events.</div>';
      }
      function renderBacktestConfigOptions() {
        if (!els.backtestConfig) return;
        const selected = els.backtestConfig.value || selectedName(state.configs, "");
        const options = (state.configs || []).map((config) => `<option value="${escapeHtml(config.name)}" ${config.name === selected ? "selected" : ""}>${escapeHtml(config.display_name || config.name)}</option>`).join("");
        els.backtestConfig.innerHTML = options || '<option value="">No config available</option>';
      }

      async function loadReviews(opts = {}) {
        try {
          const filter = state.reviewFilter || "pending";
          const [current, pending] = await Promise.allSettled([
            api(`/api/reviews?status=${encodeURIComponent(filter === "pending" ? "all" : filter)}`, { silent: opts.silent }),
            api("/api/reviews?status=pending", { silent: opts.silent })
          ]);
          if (current.status === "rejected") throw current.reason;
          const currentReviews = Array.isArray(current.value.reviews) ? current.value.reviews : [];
          const activePendingReviews = pending.status === "fulfilled" && pending.value && Array.isArray(pending.value.reviews) ? pending.value.reviews : [];
          rememberReviewTrail(activePendingReviews);
          if (state.currentReview) rememberReviewTrail([state.currentReview]);
          state.pendingReviews = activePendingReviews;
          state.reviews = filter === "pending" ? visibleReviewTrail(currentReviews, activePendingReviews) : currentReviews;
          state.pendingReviews.forEach(showReviewNotification);
          setInlineError(els.reviewError, "");
          renderReviews();
          renderDashboardActivity();
        } catch (err) {
          setInlineError(els.reviewError, `Reviews API error: ${err.message || err}`);
          renderReviews();
          throw err;
        }
      }
      function rememberReviewTrail(reviews) {
        for (const review of Array.isArray(reviews) ? reviews : []) {
          const reviewId = review && review.review_id;
          if (reviewId) state.reviewTrailIds.add(reviewId);
        }
      }
      function visibleReviewTrail(allReviews, pendingReviews) {
        const pendingIds = new Set((Array.isArray(pendingReviews) ? pendingReviews : []).map((review) => review && review.review_id).filter(Boolean));
        return (Array.isArray(allReviews) ? allReviews : []).filter((review) => {
          const reviewId = review && review.review_id;
          return reviewId && (pendingIds.has(reviewId) || state.reviewTrailIds.has(reviewId));
        });
      }
      function renderReviews() {
        document.querySelectorAll("[data-review-filter]").forEach((button) => button.classList.toggle("active", button.dataset.reviewFilter === state.reviewFilter));
        const label = state.reviewFilter === "pending" ? "session reviews" : state.reviewFilter === "history" ? "review history" : "reviews";
        els.reviewList.innerHTML = (Array.isArray(state.reviews) ? state.reviews : []).map((review) => {
          const selected = state.currentReview && state.currentReview.review_id === review.review_id ? " active" : "";
          const status = review.status || "pending";
          const complete = String(status).toLowerCase() === "pending" ? "" : " review-complete";
          const decided = review.decided_at || review.expired_at || review.updated_at || review.created_at || "";
          return `<button class="list-item${selected}${complete}" data-review="${escapeHtml(review.review_id)}"><div class="event-title"><span class="item-title">${escapeHtml(review.summary || review.review_id)}</span>${badge(status, statusClass(status))}</div><div class="item-meta">${escapeHtml([review.run_id, decided].filter(Boolean).join(" | "))}</div></button>`;
        }).join("") || `<div class="empty">No ${escapeHtml(label)}.</div>`;
      }
      async function openReview(id) {
        stopReviewArtifactRefresh();
        const data = await api(`/api/reviews/${encodeURIComponent(id)}`);
        state.currentReview = data.review;
        rememberReviewTrail([data.review]);
        state.currentReviewArtifact = null;
        renderReviews();
        let artifactHtml = '<div class="artifact"><div class="empty">No screenshot artifact.</div></div>';
        let artifactData = null;
        if (data.review.artifact_id) {
          try {
            const artifact = await api(`/api/artifacts/${encodeURIComponent(data.review.artifact_id)}`);
            artifactData = unwrapArtifactPayload(artifact);
            artifactHtml = renderReviewArtifact(artifactData);
          } catch (err) {
            const message = `Screenshot artifact could not be loaded: ${err.message || err}`;
            artifactData = { capture_status: "failed", capture_error: message };
            artifactHtml = `<div class="artifact artifact-state"><div class="empty">${escapeHtml(message)}</div></div>`;
          }
        }
        state.currentReviewArtifact = artifactData;
        setReviewActionState(state.currentReview, state.currentReviewArtifact);
        const status = data.review.status || "pending";
        const confirmation = status === "allow" || status === "block" ? `<div class="decision-confirmation">Review ${escapeHtml(status === "allow" ? "allowed" : "blocked")}. The runtime has received this decision.</div>` : "";
        const waiting = reviewDecisionLocked(data.review, state.currentReviewArtifact)
          ? '<div class="decision-waiting">Preparing screenshot before decision...</div>'
          : "";
        els.reviewDetail.innerHTML = `${confirmation}${waiting}${artifactHtml}${renderReviewHeader(data.review)}${renderReviewClassification(data.review.classification || {})}${renderReviewDecisionMeta(data.review)}`;
        scheduleSelectedReviewArtifactRefresh(state.currentReview, state.currentReviewArtifact);
      }
      function unwrapArtifactPayload(payload) {
        return payload && typeof payload.artifact === "object" && payload.artifact ? payload.artifact : (payload || {});
      }
      function reviewArtifactPending(artifact) {
        return Boolean(artifact && !artifact.image_data_url && String(artifact.capture_status || "").toLowerCase() === "pending");
      }
      function reviewDecisionLocked(review, artifact) {
        return Boolean(review && review.status === "pending" && reviewArtifactPending(artifact));
      }
      function stopReviewArtifactRefresh() {
        if (reviewArtifactRefreshTimer) clearTimeout(reviewArtifactRefreshTimer);
        reviewArtifactRefreshTimer = null;
      }
      function scheduleSelectedReviewArtifactRefresh(review, artifact) {
        stopReviewArtifactRefresh();
        if (!reviewDecisionLocked(review, artifact) || !review.artifact_id) return;
        reviewArtifactRefreshTimer = setTimeout(async () => {
          reviewArtifactRefreshTimer = null;
          if (!state.currentReview || state.currentReview.review_id !== review.review_id) return;
          try {
            const payload = await api(`/api/artifacts/${encodeURIComponent(review.artifact_id)}`);
            const nextArtifact = unwrapArtifactPayload(payload);
            state.currentReviewArtifact = nextArtifact;
            if (reviewArtifactPending(nextArtifact)) {
              setReviewActionState(state.currentReview, nextArtifact);
              scheduleSelectedReviewArtifactRefresh(state.currentReview, nextArtifact);
              return;
            }
            await openReview(review.review_id);
          } catch (err) {
            scheduleSelectedReviewArtifactRefresh(review, artifact);
          }
        }, 600);
      }
      function renderReviewArtifact(artifact) {
        const status = artifact.capture_status || "unavailable";
        if (!artifact.image_data_url) {
          const box = artifact.target_box;
          const boxText = box ? `Target box: x ${Math.round(Number(box.x || 0))}, y ${Math.round(Number(box.y || 0))}, ${Math.round(Number(box.width || 0))}x${Math.round(Number(box.height || 0))}` : "No specific target box available.";
          const attemptsText = artifact.capture_attempts ? `Capture attempts: ${artifact.capture_attempts}` : "";
          const sessionsText = Array.isArray(artifact.capture_session_candidates) && artifact.capture_session_candidates.length
            ? `Sessions tried: ${artifact.capture_session_candidates.join(", ")}`
            : (artifact.capture_session_id ? `Session used: ${artifact.capture_session_id}` : "");
          const errorsText = Array.isArray(artifact.capture_errors) && artifact.capture_errors.length ? artifact.capture_errors.join(" | ") : "";
          const message = status === "pending"
            ? "Preparing screenshot before decision."
            : status === "failed"
              ? `Screenshot capture failed${artifact.capture_error ? `: ${artifact.capture_error}` : "."}`
              : "Screenshot unavailable.";
          const statusBadge = badge(status, statusClass(status));
          const details = [boxText, attemptsText, sessionsText, errorsText].filter(Boolean);
          return `<div class="artifact artifact-state"><div class="empty"><div class="artifact-status-line">${statusBadge}<span>${escapeHtml(message)}</span></div>${details.map((item) => `<div class="small">${escapeHtml(item)}</div>`).join("")}</div></div>`;
        }
        const box = artifact.target_box;
        const viewport = artifact.viewport || {};
        const imageWidth = positiveNumber(artifact.image_width);
        const imageHeight = positiveNumber(artifact.image_height);
        const imageAttrs = `${imageWidth ? ` data-image-width="${imageWidth}"` : ""}${imageHeight ? ` data-image-height="${imageHeight}"` : ""}`;
        const boxAttrs = box ? `data-target-box="${escapeHtml(JSON.stringify(box))}" data-viewport="${escapeHtml(JSON.stringify(viewport))}"${imageAttrs}` : "";
        const helper = box ? "" : '<div class="small">Page-level review; no specific target.</div>';
        setTimeout(() => { observeArtifactStages(); scaleArtifactBoxes(); }, 0);
        return `<div class="artifact"><div class="artifact-stage"${imageAttrs}><img src="${escapeHtml(artifact.image_data_url)}" alt="review screenshot" onload="scaleArtifactBoxes()" />${box ? `<div class="target-box" ${boxAttrs}></div>` : ""}</div>${helper}</div>`;
      }
      const artifactStageObserver = window.ResizeObserver ? new ResizeObserver(() => scaleArtifactBoxes()) : null;
      function observeArtifactStages() {
        if (!artifactStageObserver) return;
        document.querySelectorAll(".artifact-stage").forEach((stage) => {
          if (stage.dataset.resizeObserved === "true") return;
          stage.dataset.resizeObserved = "true";
          artifactStageObserver.observe(stage);
        });
      }
      function scaleArtifactBoxes() {
        document.querySelectorAll(".target-box[data-target-box]").forEach((node) => {
          const stage = node.closest(".artifact-stage");
          const img = stage ? stage.querySelector("img") : null;
          if (!stage || !img) return;
          let box = {};
          let viewport = {};
          try { box = JSON.parse(node.dataset.targetBox || "{}"); viewport = JSON.parse(node.dataset.viewport || "{}"); } catch { return; }
          if (!img.clientWidth || !img.clientHeight) return;
          const imageWidth = positiveNumber(node.dataset.imageWidth) || positiveNumber(stage.dataset.imageWidth) || positiveNumber(img.naturalWidth) || positiveNumber(img.clientWidth) || 1;
          const imageHeight = positiveNumber(node.dataset.imageHeight) || positiveNumber(stage.dataset.imageHeight) || positiveNumber(img.naturalHeight) || positiveNumber(img.clientHeight) || 1;
          const viewportWidth = positiveNumber(viewport.width || viewport.clientWidth);
          const viewportHeight = positiveNumber(viewport.height || viewport.clientHeight);
          const fallbackPixelRatio = positiveNumber(viewport.screenshot_scale_x)
            || positiveNumber(viewport.device_pixel_ratio)
            || positiveNumber(window.devicePixelRatio)
            || 1;
          const fallbackPixelRatioY = positiveNumber(viewport.screenshot_scale_y)
            || positiveNumber(viewport.device_pixel_ratio)
            || positiveNumber(window.devicePixelRatio)
            || fallbackPixelRatio;
          const source = String(box.source || "").toLowerCase();
          const coordinateSpace = String(box.coordinate_space || (source === "document_rect" ? "document" : "viewport")).toLowerCase();
          const baseWidth = coordinateSpace === "image" ? imageWidth : (viewportWidth || imageWidth / fallbackPixelRatio);
          const baseHeight = coordinateSpace === "image" ? imageHeight : (viewportHeight || imageHeight / fallbackPixelRatioY);
          const pageX = positiveNumber(viewport.page_x ?? viewport.pageX ?? viewport.offset_x ?? viewport.offsetX) || 0;
          const pageY = positiveNumber(viewport.page_y ?? viewport.pageY ?? viewport.offset_y ?? viewport.offsetY) || 0;
          let x = Number(box.x || 0);
          let y = Number(box.y || 0);
          const width = Number(box.width || 8);
          const height = Number(box.height || 8);
          if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(width) || !Number.isFinite(height)) return;
          if (coordinateSpace === "document") {
            x -= pageX;
            y -= pageY;
          }
          const scaleX = img.clientWidth / baseWidth;
          const scaleY = img.clientHeight / baseHeight;
          const left = Math.max(0, Math.min(img.clientWidth, x * scaleX));
          const top = Math.max(0, Math.min(img.clientHeight, y * scaleY));
          const right = Math.max(left + 8, Math.min(img.clientWidth, (x + width) * scaleX));
          const bottom = Math.max(top + 8, Math.min(img.clientHeight, (y + height) * scaleY));
          node.style.left = `${left}px`;
          node.style.top = `${top}px`;
          node.style.width = `${Math.max(8, right - left)}px`;
          node.style.height = `${Math.max(8, bottom - top)}px`;
        });
      }
      function humanizeAction(value) {
        const key = String(value || "").toLowerCase().replace(/^playwright\./, "");
        const map = {
          page_preflight: "Page preflight",
          form_input: "Form input",
          click: "Click",
          submit: "Submit",
          navigation: "Navigation",
          select: "Select option",
          file_upload: "File upload",
          check: "Toggle",
          keyboard: "Keyboard",
          meaningful_action: "Action",
        };
        if (map[key]) return map[key];
        if (!key) return "Action";
        return key.replace(/[_.]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
      }
      function humanizeRisk(value) {
        const text = String(value || "").trim();
        if (!text) return "";
        return text.split(/[,\s]+/).filter(Boolean).map((token) => {
          const parts = token.split(".");
          const head = parts.shift() || "";
          const tail = parts.join(" ").replace(/_/g, " ").trim();
          const cap = head ? head.charAt(0).toUpperCase() + head.slice(1) : "";
          return tail ? `${cap} · ${tail}` : cap;
        }).filter(Boolean).join(", ");
      }
      function statusText(value) {
        const map = { allow: "Allowed", block: "Blocked", pending: "Pending", expired: "Expired", error: "Error" };
        const key = String(value || "").toLowerCase();
        return map[key] || (value ? String(value) : "");
      }
      function formatReviewTime(value) {
        if (!value) return "";
        try {
          const d = new Date(value);
          if (Number.isNaN(d.getTime())) return String(value);
          return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
        } catch {
          return String(value);
        }
      }
      function metaRow(label, value) {
        if (value === undefined || value === null) return "";
        const text = String(value).trim();
        if (!text || /^(not available|unknown|not provided|n\/a|none)$/i.test(text)) return "";
        return `<div class="meta-row"><span class="meta-label">${escapeHtml(label)}</span><span class="meta-value">${escapeHtml(text)}</span></div>`;
      }
      function metaCard(rows) {
        const body = rows.filter(Boolean).join("");
        return body ? `<div class="meta-card">${body}</div>` : "";
      }
      function cleanReviewTitle(summary, reason) {
        let title = String(summary || "").trim();
        const trimmedReason = String(reason || "").trim();
        if (trimmedReason && title.includes(trimmedReason)) {
          const idx = title.indexOf(trimmedReason);
          title = idx > 0 ? title.slice(0, idx).replace(/[\s:–—-]+$/, "").trim() : trimmedReason;
        }
        return title || String(summary || "");
      }
      function renderReviewHeader(review) {
        const status = review.status || "pending";
        const cls = review.classification || {};
        const reason = review.reason || "";
        const title = cleanReviewTitle(review.summary || review.review_id, reason);
        const showReason = reason && reason !== title;
        const chip = `<span class="meta-chip">${escapeHtml(humanizeAction(cls.action || cls.method))}</span>`;
        return `<div class="review-head"><div class="review-head-tags">${badge(status, statusClass(status))}${chip}</div><h3 class="review-title">${escapeHtml(title)}</h3>${showReason ? `<p class="review-reason">${escapeHtml(reason)}</p>` : ""}</div>`;
      }
      function renderReviewDecisionMeta(review) {
        if (!review || review.status === "pending") return "";
        const card = metaCard([
          metaRow("Decision", statusText(review.status)),
          metaRow("Time", formatReviewTime(review.decided_at || review.expired_at || review.updated_at)),
          metaRow("Reason", review.decision_reason),
        ]);
        return card ? `<div class="meta-section-title">Decision</div>${card}` : "";
      }
      function renderReviewClassification(classification) {
        const target = classification.target || {};
        const targetText = target.text || target.label || target.selector || target.name || "";
        const risk = humanizeRisk(classification.risk || classification.detected_risks || "");
        const card = metaCard([
          metaRow("Action", humanizeAction(classification.action || classification.method)),
          metaRow("Target", targetText),
          metaRow("Risk", risk),
        ]);
        return card ? `<div class="meta-section-title">Details</div>${card}` : "";
      }
      async function decideReview(status) {
        if (!state.currentReview) throw new Error("Select a review first.");
        if (state.currentReview.status !== "pending") throw new Error("This review is no longer pending.");
        if (reviewDecisionLocked(state.currentReview, state.currentReviewArtifact)) {
          setReviewActionState(state.currentReview, state.currentReviewArtifact);
          showToast("Screenshot is still being prepared.");
          scheduleSelectedReviewArtifactRefresh(state.currentReview, state.currentReviewArtifact);
          return;
        }
        const reviewId = state.currentReview.review_id;
        setReviewActionState(Object.assign({}, state.currentReview, { status }));
        stopReviewArtifactRefresh();
        let accepted = false;
        try {
          await api(`/api/reviews/${encodeURIComponent(reviewId)}/decision`, { method: "POST", body: JSON.stringify({ status, reason: `Dashboard ${status}` }) });
          clearReviewNotification(reviewId);
          showToast(status === "allow" ? "Review allowed." : "Review blocked.");
          accepted = true;
        } catch (err) {
          showToast(`Decision failed: ${err.message || err}`);
        }
        await Promise.allSettled([loadReviews(), loadStatus()]);
        if (!accepted) {
          await openReview(reviewId).catch(() => {});
          return;
        }
        const next = (state.pendingReviews || []).find((review) => review.review_id !== reviewId);
        if (next && state.reviewFilter === "pending") {
          await openReview(next.review_id);
        } else {
          await openReview(reviewId).catch(() => {
            state.currentReview = null;
            state.currentReviewArtifact = null;
            setReviewActionState(null, null);
            els.reviewDetail.innerHTML = `<div class="decision-confirmation">Review ${escapeHtml(status === "allow" ? "allowed" : "blocked")}.</div><div class="empty">No pending reviews.</div>`;
          });
        }
      }
      function setReviewActionState(review, artifact = state.currentReviewArtifact) {
        const pending = review && review.status === "pending";
        const locked = pending && reviewDecisionLocked(review, artifact);
        const title = locked ? "Screenshot is still being prepared." : "";
        byId("allow-review").disabled = !pending || locked;
        byId("block-review").disabled = !pending || locked;
        byId("allow-review").title = title;
        byId("block-review").title = title;
      }
      function setReviewFilter(filter) {
        state.reviewFilter = filter || "pending";
        loadReviews().catch((err) => showToast(err.message));
      }

      async function loadSkills() {
        const [general, specific, merged, status] = await Promise.allSettled([api("/api/skills/general"), api("/api/skills/specific"), api("/api/skills/merged"), api("/api/skills/status")]);
        state.skillErrors = {};
        if (general.status === "fulfilled") state.generalSkills = general.value.skills || []; else { state.generalSkills = []; state.skillErrors.general = general.reason.message || String(general.reason); }
        if (specific.status === "fulfilled") state.specificSkills = specific.value.skills || []; else { state.specificSkills = []; state.skillErrors.specific = specific.reason.message || String(specific.reason); }
        if (merged.status === "fulfilled") state.mergedSkills = merged.value.skills || []; else { state.mergedSkills = []; state.skillErrors.merged = merged.reason.message || String(merged.reason); }
        if (status.status === "fulfilled") state.skillDiagnostics = status.value.skills || {};
        const failed = [general, specific, merged].find((result) => result.status === "rejected");
        if (failed) showToast(`Skills API error: ${failed.reason.message || failed.reason}`);
        renderSkillLists();
        if (state.currentMergedSkill) renderMergedSkill();
        if (state.currentGraph) renderGraph();
      }
      function renderSkillLists() {
        els.generalList.innerHTML = state.skillErrors.general ? `<div class="empty">API error: ${escapeHtml(state.skillErrors.general)}</div>` : state.generalSkills.map((skill) => `<button class="list-item" data-general="${escapeHtml(skill.name)}"><div class="item-title">${escapeHtml(skill.display_name || skill.name)}</div><div class="item-meta">${escapeHtml(skill.id || "")} | ${skill.risk_count || 0} risks</div></button>`).join("") || '<div class="empty">No general skills.</div>';
        els.specificList.innerHTML = state.skillErrors.specific ? `<div class="empty">API error: ${escapeHtml(state.skillErrors.specific)}</div>` : state.specificSkills.map((skill) => `<button class="list-item" data-specific="${escapeHtml(skill.name)}"><div class="item-title">${escapeHtml(skill.display_name || skill.name)}</div><div class="item-meta">${escapeHtml(skill.id || "")} | ${skill.target_count || 0} targets</div></button>`).join("") || '<div class="empty">No specific skills.</div>';
        els.mergedList.innerHTML = state.skillErrors.merged ? `<div class="empty">API error: ${escapeHtml(state.skillErrors.merged)}</div>` : state.mergedSkills.map((skill) => `<button class="list-item" data-merged="${escapeHtml(skill.name)}"><div class="item-title">${escapeHtml(skill.display_name || skill.name)}</div><div class="item-meta">${skill.component_count || 0} components | ${skill.valid ? "valid" : "invalid"}</div></button>`).join("") || '<div class="empty">No merged skills.</div>';
      }
      async function openSpecific(name) {
        const data = await api(`/api/skills/specific/${encodeURIComponent(name)}`);
        state.currentSpecificMode = "edit";
        state.currentSpecificSkill = data;
        els.specificName.value = data.name;
        state.currentSpecificTab = "targets";
        renderSpecificSkill();
      }
      function newSpecific() {
        state.currentSpecificMode = "import";
        state.currentSpecificSkill = null;
        els.specificName.value = uniqueJsonName("specific-skill", state.specificSkills);
        els.specificImportEditor.value = "";
        els.specificEditor.value = "";
        els.specificGui.innerHTML = "";
        renderSpecificSkill();
      }
      function renderSpecificSkill() {
        const importMode = state.currentSpecificMode === "import";
        byId("specific-targets-tab").classList.toggle("active", state.currentSpecificTab === "targets");
        byId("specific-json-tab").classList.toggle("active", state.currentSpecificTab === "json");
        els.specificImportPane.classList.toggle("hidden", !importMode);
        els.specificTargetsPane.classList.toggle("hidden", importMode || state.currentSpecificTab !== "targets");
        els.specificJsonPane.classList.toggle("hidden", importMode || state.currentSpecificTab !== "json");
        byId("specific-targets-tab").disabled = importMode;
        byId("specific-json-tab").disabled = importMode;
        byId("delete-specific").disabled = importMode || !state.currentSpecificSkill;
        byId("save-specific").textContent = importMode ? "Import skill" : "Save";
        byId("cancel-specific").classList.toggle("hidden", state.currentSpecificMode === "empty");
        if (importMode) {
          byId("save-specific").disabled = false;
          els.specificState.innerHTML = badge("Import mode", "warn");
          return;
        }
        const skill = state.currentSpecificSkill;
        if (!skill) {
          state.currentSpecificMode = "empty";
          els.specificName.value = "";
          els.specificEditor.value = "";
          els.specificState.innerHTML = '<div class="empty">Select a specific skill.</div>';
          els.specificGui.innerHTML = "";
          byId("delete-specific").disabled = true;
          byId("save-specific").disabled = true;
          byId("cancel-specific").classList.add("hidden");
          return;
        }
        byId("save-specific").disabled = false;
        const data = skill.data || {};
        els.specificEditor.value = JSON.stringify(data, null, 2);
        els.specificState.innerHTML = skill.valid ? badge("Valid", "ok") : badge(skill.error || "Invalid", "danger");
        const targets = Array.isArray(data.targets) ? data.targets : [];
        els.specificGui.innerHTML = targets.map(renderSpecificTarget).join("") || '<div class="empty">No protected targets in this skill.</div>';
      }
      function renderSpecificTarget(target, index) {
        const summary = targetSummary(target);
        return `
          <div class="target-card" data-target-index="${index}">
            <div class="toolbar">
              <div>
                <div class="item-title">${escapeHtml(summary.label)}</div>
                <div class="item-meta">${escapeHtml(summary.kind)} | ${escapeHtml(summary.section || "No section")}</div>
              </div>
              ${badge(target.decision || "ask", statusClass(target.decision))}
            </div>
            <div class="target-details">
              <div class="target-detail"><strong>Page</strong><br>${escapeHtml(summary.page)}</div>
              <div class="target-detail"><strong>Selector</strong><br>${escapeHtml(summary.selector)}</div>
              <div class="target-detail"><strong>Target ID</strong><br>${escapeHtml(target.id || "")}</div>
            </div>
            <div class="target-controls">
              <label class="control-field"><span>Decision</span><select data-target-decision="${index}"><option value="ask" ${target.decision === "ask" ? "selected" : ""}>Ask</option><option value="block" ${target.decision === "block" ? "selected" : ""}>Block</option></select></label>
              <label class="control-field"><span>Reason</span><input data-target-reason="${index}" value="${escapeHtml(target.reason || "")}" placeholder="Reason shown to reviewer" /></label>
              <button class="button danger" data-delete-target="${index}">Delete target</button>
            </div>
            <details class="advanced-panel">
              <summary>Advanced match settings</summary>
              <label class="control-field" style="max-width:180px;margin-top:8px"><span>Match threshold</span><input data-target-threshold="${index}" type="number" min="1" max="100" value="${escapeHtml(target.threshold ?? 70)}" /></label>
            </details>
          </div>
        `;
      }
      function targetSummary(target) {
        const fp = target.fingerprint || target.match || {};
        const labels = fp.labels || {};
        const capture = target.capture || {};
        const action = fp.action_signature || {};
        const label = labels.accessible || labels.associated || fp.parent_label || fp.nearby_text || action.text || fp.text || target.id || "Unnamed target";
        const section = fp.section_heading || (fp.structural_context || {}).section_heading || "";
        const selector = fp.selector || fp.id || fp.name || "No selector";
        const page = capture.title || capture.url || "Captured page";
        return { label, section, selector, page, kind: inferTargetKind(fp) };
      }
      function inferTargetKind(fp) {
        const tag = String(fp.tag || "").toLowerCase();
        const type = String(fp.type || (fp.stable_attributes || {}).type || "").toLowerCase();
        const role = String(fp.role || "").toLowerCase();
        const kind = String(fp.control_kind || (fp.action_signature || {}).kind || (fp.structure_signature || {}).target_kind || "").toLowerCase();
        if (tag === "input" && type === "password") return "Password input";
        if (tag === "input" && type === "email") return "Email input";
        if (tag === "input" && type === "checkbox") return "Checkbox";
        if (tag === "input" && type === "radio") return "Radio button";
        if (tag === "input" && type === "file") return "File upload";
        if (tag === "input" || kind === "text_entry") return "Text input";
        if (tag === "button" || role === "button" || kind.includes("button")) return "Button";
        if (tag === "select" || kind.includes("select")) return "Dropdown";
        if (tag === "a" || role === "link") return "Link";
        return "Unknown element";
      }
      function updateSpecificEditorFromState() {
        if (state.currentSpecificSkill) els.specificEditor.value = JSON.stringify(state.currentSpecificSkill.data || {}, null, 2);
      }
      function updateSpecificTarget(index, patch, options = {}) {
        if (!state.currentSpecificSkill) return;
        const data = state.currentSpecificSkill.data || {};
        const targets = Array.isArray(data.targets) ? data.targets : [];
        if (!targets[index]) return;
        targets[index] = Object.assign({}, targets[index], patch);
        state.currentSpecificSkill.data = Object.assign({}, data, { targets });
        updateSpecificEditorFromState();
        if (options.render !== false) renderSpecificSkill();
      }
      function deleteSpecificTarget(index) {
        if (!state.currentSpecificSkill) return;
        const data = state.currentSpecificSkill.data || {};
        const targets = (Array.isArray(data.targets) ? data.targets : []).filter((_, itemIndex) => itemIndex !== index);
        state.currentSpecificSkill.data = Object.assign({}, data, { targets });
        renderSpecificSkill();
      }
      async function saveSpecific() {
        if (state.currentSpecificMode === "import") return importSpecific();
        if (!state.currentSpecificSkill) throw new Error("Select a specific skill first, or use New / Import.");
        const name = normalizeJsonName(els.specificName.value, "specific-skill.json");
        els.specificName.value = name;
        const data = state.currentSpecificTab === "json" ? JSON.parse(els.specificEditor.value) : (state.currentSpecificSkill && state.currentSpecificSkill.data) || JSON.parse(els.specificEditor.value);
        await api(`/api/skills/specific/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify(data) });
        showToast("Specific skill saved.");
        await loadSkills();
        await openSpecific(name);
      }
      async function validateSpecificImport() {
        const normalized = sanitizeSpecificSkillImportData(parseSpecificImportJson());
        syncSpecificImportEditor(normalized);
        const result = await api("/api/skills/specific/validate", { method: "POST", body: JSON.stringify({ data: normalized.data }) });
        const removed = normalized.removed + Number(result.removed_raw_value_fields || 0);
        const note = removed ? `<span class="item-meta">Removed ${removed} raw captured value field${removed === 1 ? "" : "s"}.</span>` : "";
        els.specificState.innerHTML = result.valid ? `${badge("Valid import", "ok")} ${note}` : badge(result.error || "Invalid import", "danger");
        if (!result.valid) throw new Error(result.error || "Specific skill JSON is invalid.");
        showToast(removed ? "Specific skill JSON is valid after normalization." : "Specific skill JSON is valid.");
        return normalized.data;
      }
      async function importSpecific() {
        const name = normalizeJsonName(els.specificName.value, "specific-skill.json");
        els.specificName.value = name;
        const normalized = sanitizeSpecificSkillImportData(parseSpecificImportJson());
        syncSpecificImportEditor(normalized);
        const result = await api("/api/skills/specific/import", { method: "POST", body: JSON.stringify({ name, data: normalized.data }) });
        const removed = normalized.removed + Number(result.removed_raw_value_fields || 0);
        showToast(removed ? `Specific skill imported after removing ${removed} raw value field${removed === 1 ? "" : "s"}.` : "Specific skill imported.");
        state.currentSpecificMode = "edit";
        await loadSkills();
        await openSpecific(name);
      }
      function parseSpecificImportJson() {
        try {
          const data = JSON.parse(els.specificImportEditor.value || "{}");
          if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("Import must be a JSON object.");
          return data;
        } catch (err) {
          els.specificState.innerHTML = badge(`Invalid JSON: ${err.message || err}`, "danger");
          throw err;
        }
      }
      function sanitizeSpecificSkillImportData(data) {
        const cleaned = JSON.parse(JSON.stringify(data));
        const targets = Array.isArray(cleaned.targets) ? cleaned.targets : [];
        let removed = 0;
        targets.forEach((target) => {
          if (!target || typeof target !== "object" || Array.isArray(target)) return;
          ["fingerprint", "match"].forEach((key) => {
            const fingerprint = target[key];
            if (fingerprint && typeof fingerprint === "object" && !Array.isArray(fingerprint)) {
              removed += stripSpecificFingerprintRawValues(fingerprint);
            }
          });
        });
        return { data: cleaned, removed };
      }
      function stripSpecificFingerprintRawValues(value) {
        const rawKeys = new Set(["value", "value_sample", "input_value", "text_value", "raw_value"]);
        let removed = 0;
        if (Array.isArray(value)) {
          value.forEach((item) => { removed += stripSpecificFingerprintRawValues(item); });
          return removed;
        }
        if (!value || typeof value !== "object") return 0;
        Object.keys(value).forEach((key) => {
          if (rawKeys.has(String(key).toLowerCase())) {
            delete value[key];
            removed += 1;
          } else {
            removed += stripSpecificFingerprintRawValues(value[key]);
          }
        });
        return removed;
      }
      function syncSpecificImportEditor(normalized) {
        if (normalized.removed > 0) {
          els.specificImportEditor.value = JSON.stringify(normalized.data, null, 2);
        }
      }
      function cancelSpecific() {
        state.currentSpecificMode = "empty";
        state.currentSpecificSkill = null;
        renderSpecificSkill();
      }
      async function deleteSpecific() {
        const name = els.specificName.value.trim();
        if (!name) throw new Error("Select a specific skill first.");
        await api(`/api/skills/specific/${encodeURIComponent(name)}`, { method: "DELETE" });
        state.currentSpecificSkill = null;
        state.currentSpecificMode = "empty";
        renderSpecificSkill();
        showToast("Specific skill deleted.");
        await loadSkills();
      }
      async function openMerged(name) {
        const data = await api(`/api/skills/merged/${encodeURIComponent(name)}`);
        state.currentMergedSkill = data;
        els.mergedName.value = data.name;
        renderMergedSkill();
      }
      function newMerged() {
        const data = {
          schema: "acira.merged_skill.v1",
          id: "new-merged-skill",
          name: "New merged skill",
          components: []
        };
        state.currentMergedSkill = { name: "new-merged-skill.json", data, valid: true };
        els.mergedName.value = "new-merged-skill.json";
        switchMergedTab("components");
        renderMergedSkill();
      }
      function renderMergedSkill() {
        const skill = state.currentMergedSkill;
        if (!skill) {
          els.mergedName.value = "";
          els.mergedEditor.value = "";
          els.mergedState.innerHTML = '<div class="empty">Select or create a merged skill.</div>';
          els.mergedComponents.innerHTML = "";
          els.mergedPicker.innerHTML = "";
          els.mergedCompiled.innerHTML = "";
          return;
        }
        skill.data = normalizeMergedSkillData(skill.data || {});
        els.mergedEditor.value = JSON.stringify(skill.data, null, 2);
        els.mergedState.innerHTML = skill.valid ? badge("Valid", "ok") : badge(skill.error || "Invalid", "danger");
        renderMergedComponents();
        renderMergedPicker();
        renderMergedCompiled();
      }
      function normalizeMergedSkillData(data) {
        const normalized = Object.assign({ schema: "acira.merged_skill.v1", components: [] }, data || {});
        normalized.components = dedupeMergedComponents(Array.isArray(normalized.components) ? normalized.components : []);
        return normalized;
      }
      function dedupeMergedComponents(components) {
        const seen = new Set();
        const deduped = [];
        for (const component of components || []) {
          const type = String(component.type || "").trim().toLowerCase();
          const name = String(component.name || component.path || "").trim();
          if (!type || !name) continue;
          const key = `${type}:${name}`;
          if (seen.has(key)) continue;
          seen.add(key);
          deduped.push({ type, name });
        }
        return deduped;
      }
      function componentSources(type) {
        if (type === "general") return state.generalSkills;
        if (type === "specific") return state.specificSkills;
        return state.mergedSkills;
      }
      function componentSummary(type, name) {
        const item = componentSources(type).find((skill) => skill.name === name);
        const title = item ? item.display_name || item.name : name;
        const meta = item ? item.id || item.name : name;
        return { title, meta };
      }
      function renderMergedComponents() {
        const data = normalizeMergedSkillData((state.currentMergedSkill || {}).data || {});
        const components = data.components || [];
        els.mergedComponents.innerHTML = components.map((component, index) => {
          const summary = componentSummary(component.type, component.name);
          return `
            <div class="component-card">
              <div>
                <div class="component-kind">${escapeHtml(component.type)}</div>
                <div class="item-title">${escapeHtml(summary.title)}</div>
                <div class="item-meta">${escapeHtml(component.name)} | ${escapeHtml(summary.meta)}</div>
              </div>
              <button class="button danger" data-remove-merged-component="${index}">Remove</button>
            </div>
          `;
        }).join("") || '<div class="empty">No skills in this bundle yet — add some from the list on the right.</div>';
      }
      function renderMergedPicker() {
        const currentName = els.mergedName.value.trim();
        const selected = new Set(((state.currentMergedSkill || {}).data || {}).components?.map((item) => `${item.type}:${item.name}`) || []);
        const query = (els.mergedSkillSearch && els.mergedSkillSearch.value || "").trim().toLowerCase();
        const sections = [
          ["general", "General skills"],
          ["specific", "Specific skills"],
          ["merged", "Merged skills"]
        ];
        els.mergedPicker.innerHTML = sections.map(([type, title]) => {
          const items = componentSources(type).filter((skill) => !(type === "merged" && skill.name === currentName)).filter((skill) => !query || `${skill.display_name || ""} ${skill.name || ""} ${skill.id || ""}`.toLowerCase().includes(query));
          const rows = items.map((skill) => {
            const key = `${type}:${skill.name}`;
            const added = selected.has(key);
            return `
              <div class="component-card">
                <div>
                  <div class="component-kind">${escapeHtml(type)}</div>
                  <div class="item-title">${escapeHtml(skill.display_name || skill.name)}</div>
                  <div class="item-meta">${escapeHtml(skill.name)}${skill.valid === false ? " | invalid" : ""}</div>
                </div>
                <button class="button" data-add-merged-component-type="${escapeHtml(type)}" data-add-merged-component-name="${escapeHtml(skill.name)}" ${added || skill.valid === false ? "disabled" : ""}>${added ? "Added" : "Add"}</button>
              </div>
            `;
          }).join("") || '<div class="empty">None available.</div>';
          return `<details class="skill-drawer" open><summary><span>${escapeHtml(title)}</span><span>${items.length}</span></summary><div class="skill-drawer-body">${rows}</div></details>`;
        }).join("");
      }
      function renderMergedCompiled() {
        const compiled = ((state.currentMergedSkill || {}).data || {}).compiled;
        if (!compiled) {
          els.mergedCompiled.innerHTML = '<h3>Compiled result</h3><div class="empty">Saved merged skills show compiled output here.</div>';
          return;
        }
        const general = Array.isArray(compiled.general_skills) ? compiled.general_skills.length : 0;
        const specific = Array.isArray(compiled.specific_skills) ? compiled.specific_skills.length : 0;
        const targets = Array.isArray(compiled.specific_targets) ? compiled.specific_targets.length : 0;
        els.mergedCompiled.innerHTML = `<h3>Compiled result</h3><div class="kv-grid"><div class="kv"><div class="label">General</div><div class="value">${general}</div></div><div class="kv"><div class="label">Specific</div><div class="value">${specific}</div></div><div class="kv"><div class="label">Targets</div><div class="value">${targets}</div></div></div>`;
      }
      function addMergedComponent(type, name) {
        if (!state.currentMergedSkill) newMerged();
        const data = normalizeMergedSkillData(state.currentMergedSkill.data || {});
        data.components = dedupeMergedComponents([...(data.components || []), { type, name }]);
        state.currentMergedSkill.data = data;
        renderMergedSkill();
      }
      function removeMergedComponent(index) {
        if (!state.currentMergedSkill) return;
        const data = normalizeMergedSkillData(state.currentMergedSkill.data || {});
        data.components = (data.components || []).filter((_, itemIndex) => itemIndex !== index);
        state.currentMergedSkill.data = data;
        renderMergedSkill();
      }
      function syncMergedIdentityFromName(data) {
        const fileName = els.mergedName.value.trim();
        const stem = fileName.replace(/\.json$/i, "");
        const next = Object.assign({}, data);
        if (!String(next.id || "").trim()) next.id = stem;
        if (!String(next.name || "").trim()) next.name = stem;
        return next;
      }
      async function saveMerged() {
        const name = els.mergedName.value.trim();
        if (!name) throw new Error("Merged skill needs a file name.");
        const source = state.currentMergedTab === "json" ? JSON.parse(els.mergedEditor.value || "{}") : (state.currentMergedSkill && state.currentMergedSkill.data) || {};
        const data = syncMergedIdentityFromName(normalizeMergedSkillData(source));
        if (!Array.isArray(data.components) || data.components.length === 0) {
          els.mergedState.innerHTML = badge("Add at least one component before saving.", "danger");
          throw new Error("Merged skill needs at least one component.");
        }
        await api(`/api/skills/merged/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify(data) });
        showToast("Merged skill saved.");
        await loadSkills();
        await openMerged(name);
      }
      async function deleteMerged() {
        const name = els.mergedName.value.trim();
        if (!name) throw new Error("Select a merged skill first.");
        await api(`/api/skills/merged/${encodeURIComponent(name)}`, { method: "DELETE" });
        state.currentMergedSkill = null;
        renderMergedSkill();
        showToast("Merged skill deleted.");
        await loadSkills();
      }

      async function loadConfigs() {
        const data = await api("/api/configs");
        state.configs = data.configs || [];
        els.configList.innerHTML = state.configs.map((config) => `<button class="list-item" data-config="${escapeHtml(config.name)}"><div class="item-title">${escapeHtml(config.display_name || config.name)}</div><div class="item-meta">${escapeHtml(config.name)} | ${config.valid ? "valid" : "invalid"}</div></button>`).join("") || '<div class="empty">No configs.</div>';
        renderBacktestConfigOptions();
        updateConfigActionState();
        await refreshSecretConfigSelect();
      }
      async function openConfig(name) {
        const data = await api(`/api/configs/${encodeURIComponent(name)}`);
        state.currentConfigMode = "edit";
        state.currentConfigOriginalName = data.name;
        const pop = byId("config-pop");
        if (pop) pop.classList.add("hidden");
        els.configName.value = data.name;
        els.configEditor.value = JSON.stringify(data.data, null, 2);
        els.configState.innerHTML = data.valid ? badge("Valid", "ok") : badge(data.error || "Invalid", "danger");
        await refreshSecretConfigSelect(data.data.secrets_config_path || "");
        const graph = await api(`/api/configs/${encodeURIComponent(name)}/graph`);
        state.currentGraph = normalizeConfigGraph(graph.graph, data.name, data.data && data.data.name);
        state.selectedGraphNodeId = "config";
        state.graphUserScale = 1; state.graphUserPanX = 0; state.graphUserPanY = 0;
        switchConfigTab("graph");
        applyGraphAutoLayout(state.currentGraph, { onlyUnpinned: true });
        renderGraph();
        updateConfigActionState();
      }
      async function newConfig() {
        const name = uniqueJsonName("untitled-config", state.configs);
        const created = await api("/api/configs", { method: "POST", body: JSON.stringify({ name }) });
        showToast("Blank config created — drag skills from the library onto the canvas to build it.");
        await loadConfigs();
        await openConfig(created.name || name);
      }
      async function duplicateConfig() {
        const currentName = els.configName.value.trim();
        if (!currentName) throw new Error("Open a config before duplicating it.");
        const stem = currentName.replace(/\.json$/i, "");
        const name = uniqueJsonName(`${stem}-copy`, state.configs);
        const data = safeConfigForCreate(configDataFromEditor());
        data.name = `${data.name || stem} Copy`;
        const created = await api("/api/configs", { method: "POST", body: JSON.stringify({ name, data }) });
        showToast("Config duplicated.");
        await loadConfigs();
        await openConfig(created.name || name);
      }
      async function saveConfig() {
        if (state.currentConfigTab === "graph") return saveGraph();
        const name = normalizeJsonName(els.configName.value, "dashboard-config.json");
        els.configName.value = name;
        const data = configDataFromEditor();
        await api(`/api/configs/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify(data) });
        if (state.currentConfigOriginalName && state.currentConfigOriginalName !== name) {
          await api(`/api/configs/${encodeURIComponent(state.currentConfigOriginalName)}`, { method: "DELETE" }).catch(() => {});
        }
        state.currentConfigOriginalName = name;
        showToast("Config saved.");
        await loadConfigs();
        await openConfig(name);
      }
      function configDataFromEditor() {
        const data = JSON.parse(els.configEditor.value || "{}");
        if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("Config JSON must be an object.");
        return data;
      }
      function safeConfigForCreate(data) {
        const clone = JSON.parse(JSON.stringify(data || {}));
        if (clone.dashboard && typeof clone.dashboard === "object" && clone.dashboard.api_key === "[REDACTED]") {
          delete clone.dashboard.api_key;
          clone.dashboard.enabled = false;
        }
        return clone;
      }
      function updateConfigActionState() {
        const hasConfig = !!els.configName.value.trim();
        byId("duplicate-config").disabled = !hasConfig;
        byId("save-config").disabled = !hasConfig;
        byId("delete-config").disabled = !hasConfig;
      }
      async function deleteConfig() {
        const name = state.currentConfigOriginalName || els.configName.value.trim();
        if (!name) throw new Error("Open a config first.");
        const confirmed = await showConfirmModal(
          "Delete config",
          `"${name}" will be permanently deleted. This cannot be undone.`,
          { okLabel: "Delete config", okClass: "danger" }
        );
        if (!confirmed) return;
        await api(`/api/configs/${encodeURIComponent(name)}`, { method: "DELETE" });
        state.currentGraph = null;
        state.currentConfigOriginalName = null;
        els.configName.value = "";
        els.configEditor.value = "";
        els.configState.innerHTML = "";
        state.currentConfigMode = "empty";
        showToast("Config deleted.");
        await loadConfigs();
      }
      async function refreshSecretConfigSelect(selected = null) {
        const data = await api("/api/secrets/configs");
        state.secretConfigs = data.configs || [];
        if (selected !== null) state.currentSecretLink = selected || "";
        renderGraphSkillLibrary();
        renderGraphInspector();
      }
      async function saveConfigSecretLink() {
        if (!els.configName.value.trim()) throw new Error("Open a config first.");
        const select = byId("graph-secret-link");
        const data = await api(`/api/configs/${encodeURIComponent(els.configName.value.trim())}/secrets-config-link`, { method: "PUT", body: JSON.stringify({ secrets_config_path: select ? select.value : "" }) });
        els.configEditor.value = JSON.stringify(data.data, null, 2);
        state.currentSecretLink = data.secrets_config_path || "";
        els.configState.innerHTML = badge("Valid", "ok");
        showToast("Secrets config linked.");
        renderGraphInspector();
      }
      function normalizeConfigGraph(graph, configName, displayName = "") {
        const raw = graph && typeof graph === "object" ? graph : {};
        const existingNodes = Array.isArray(raw.nodes) ? raw.nodes : [];
        const existingByKey = new Map(existingNodes.filter((node) => node && node.type === "skill").map((node) => [`${node.skill_type}:${node.name || node.path}`, node]));
        const existingSecret = existingNodes.find((node) => node && node.type === "secret_config") || {};
        const namesByType = { general: [], specific: [], merged: [] };
        ["general", "specific", "merged"].forEach((type) => {
          const key = `${type}_skills`;
          (Array.isArray(raw[key]) ? raw[key] : []).forEach((name) => { if (name) namesByType[type].push(String(name)); });
        });
        existingNodes.forEach((node) => {
          if (!node || node.type !== "skill" || !namesByType[node.skill_type]) return;
          const name = node.name || (node.path ? String(node.path).split(/[\\/]/).pop() : "");
          if (name) namesByType[node.skill_type].push(name);
        });
        const rawSecretName = raw.secrets_config_path || raw.secret_config_path || raw.secret_config || state.currentSecretLink || "";
        let secretName = existingSecret.name || (existingSecret.path ? String(existingSecret.path).split(/[\\/]/).pop() : "") || (rawSecretName ? String(rawSecretName).split(/[\\/]/).pop() : "");
        const nodes = [{ id: "config", type: "config", label: displayName || raw.config || configName || "Config", x: 48, y: 220, pinned: true }];
        const edges = [];
        ["general", "specific", "merged"].forEach((type) => {
          namesByType[type] = dedupeNames(namesByType[type]);
          namesByType[type].forEach((name) => {
            const old = existingByKey.get(`${type}:${name}`) || {};
            const nodeId = old.id || `skill-${type}-${safeDomId(name)}`;
            const node = { id: nodeId, type: "skill", skill_type: type, name, path: old.path || name, x: old.pinned ? Number(old.x) : undefined, y: old.pinned ? Number(old.y) : undefined, pinned: !!old.pinned };
            nodes.push(node);
            edges.push({ from: node.id, to: "config" });
          });
        });
        if (secretName) {
          const node = {
            id: existingSecret.id || `secret-config-${safeDomId(secretName)}`,
            type: "secret_config",
            name: secretName,
            path: existingSecret.path || secretName,
            x: existingSecret.pinned ? Number(existingSecret.x) : undefined,
            y: existingSecret.pinned ? Number(existingSecret.y) : undefined,
            pinned: !!existingSecret.pinned
          };
          nodes.push(node);
          edges.push({ from: node.id, to: "config" });
        }
        const normalized = { schema: "acira.config_graph.v1", config: configName || raw.config || "", nodes, edges, general_skills: namesByType.general, specific_skills: namesByType.specific, merged_skills: namesByType.merged, secrets_config_path: secretName || "" };
        applyGraphAutoLayout(normalized, { onlyUnpinned: true });
        return normalized;
      }
      function dedupeNames(items) {
        const seen = new Set();
        return (items || []).map((item) => String(item || "").trim()).filter((item) => {
          if (!item || seen.has(item)) return false;
          seen.add(item);
          return true;
        });
      }
      function safeDomId(value) {
        return String(value || "").replace(/[^a-zA-Z0-9_-]/g, "-");
      }
      // Lay the graph out to fit entirely inside the visible canvas (no sideways scroll):
      // the config sits on the left, every linked skill stacks in a single column on the
      // right, grouped by type. The column + config are vertically centered.
      function applyGraphAutoLayout(graph, options = {}) {
        if (!graph || !Array.isArray(graph.nodes)) return graph;
        const onlyUnpinned = options.onlyUnpinned !== false;
        const W = Math.max(440, (els.graph && els.graph.clientWidth) || 760);
        const H = Math.max(360, (els.graph && els.graph.clientHeight) || 520);
        const padX = 30, padY = 28, rowGap = 94, groupGap = 22;
        const order = ["general", "specific", "merged", "secrets"];
        const grouped = { general: [], specific: [], merged: [], secrets: [] };
        graph.nodes.forEach((node) => {
          if (node && node.type === "skill" && grouped[node.skill_type]) grouped[node.skill_type].push(node);
          if (node && node.type === "secret_config") grouped.secrets.push(node);
        });
        const total = order.reduce((n, t) => n + grouped[t].length, 0);
        const groupsPresent = order.filter((t) => grouped[t].length).length;
        const colHeight = Math.max(GRAPH_NODE_H, total * rowGap - (total ? rowGap - GRAPH_NODE_H : 0) + Math.max(0, groupsPresent - 1) * groupGap);
        const colTop = colHeight + 2 * padY <= H ? Math.round((H - colHeight) / 2) : padY;
        const configX = padX;
        const skillX = configX + GRAPH_NODE_W + 104;
        let y = colTop;
        let placed = 0;
        order.forEach((type) => {
          const nodes = grouped[type];
          if (!nodes.length) return;
          if (placed > 0) y += groupGap;
          nodes.forEach((node) => {
            if (!(onlyUnpinned && node.pinned)) { node.x = skillX; node.y = y; node.pinned = false; }
            y += rowGap;
            placed += 1;
          });
        });
        const config = graph.nodes.find((node) => node.id === "config");
        if (config) {
          config.x = configX;
          config.y = total > 0
            ? Math.max(padY, Math.round(colTop + colHeight / 2 - GRAPH_NODE_H / 2))
            : Math.round((H - GRAPH_NODE_H) / 2);
          config.pinned = true;
        }
        return graph;
      }
      function renderGraph() {
        renderGraphSkillLibrary();
        renderGraphCanvas();
        renderGraphInspector();
      }
      function graphSkillNodes() {
        return state.currentGraph && Array.isArray(state.currentGraph.nodes) ? state.currentGraph.nodes.filter((node) => node.type === "skill") : [];
      }
      function graphSecretNodes() {
        return state.currentGraph && Array.isArray(state.currentGraph.nodes) ? state.currentGraph.nodes.filter((node) => node.type === "secret_config") : [];
      }
      function graphResourceNodes() {
        return state.currentGraph && Array.isArray(state.currentGraph.nodes) ? state.currentGraph.nodes.filter((node) => node.type === "skill" || node.type === "secret_config") : [];
      }
      function isGraphSkillAdded(type, name) {
        return graphSkillNodes().some((node) => node.skill_type === type && node.name === name);
      }
      function isGraphSecretAdded(name) {
        return graphSecretNodes().some((node) => node.name === name);
      }
      function skillSourceFor(type) {
        return type === "general" ? state.generalSkills : type === "specific" ? state.specificSkills : state.mergedSkills;
      }
      function secretConfigSourceFor(name) {
        return (state.secretConfigs || []).find((config) => config.name === name) || {};
      }
      const DRAWER_DESC = {
        general: "Apply on every page",
        specific: "Captured per-app rules",
        merged: "Bundled skill sets",
        secrets: "Local secret values"
      };
      const DRAWER_ICON = {
        general: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.6 2.7 2.6 15.3 0 18M12 3c-2.6 2.7-2.6 15.3 0 18"/></svg>`,
        specific: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2.5"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></svg>`,
        merged: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m12 3 9 5-9 5-9-5 9-5z"/><path d="m3 13 9 5 9-5"/></svg>`,
        secrets: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="8" cy="15" r="4"/><path d="m21 3-9 9M15 9l3 3"/></svg>`
      };
      function graphDrawerSummary(type, label, count) {
        return `<summary>
          <svg class="drawer-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="m6 9 6 6 6-6"/></svg>
          <span class="drawer-ico drawer-ico-${type}">${DRAWER_ICON[type] || ""}</span>
          <span class="drawer-text"><span class="drawer-label">${escapeHtml(label)}</span><span class="drawer-desc">${escapeHtml(DRAWER_DESC[type] || "")}</span></span>
          <span class="drawer-count">${count}</span>
        </summary>`;
      }
      function renderGraphSkillLibrary() {
        if (!els.graphSkillLibrary) return;
        const query = (els.graphSkillSearch && els.graphSkillSearch.value || "").trim().toLowerCase();
        const sections = [["general", "General skills"], ["specific", "Specific skills"], ["merged", "Merged skills"]];
        const skillHtml = sections.map(([type, label]) => {
          const items = (skillSourceFor(type) || []).filter((skill) => !query || `${skill.display_name || ""} ${skill.name || ""} ${skill.id || ""}`.toLowerCase().includes(query));
          const rows = items.map((skill) => {
            const added = isGraphSkillAdded(type, skill.name);
            return `<div class="library-skill" draggable="false" aria-disabled="${added || skill.valid === false}" data-library-skill-type="${escapeHtml(type)}" data-library-skill-name="${escapeHtml(skill.name)}">
              <div>
                <div class="item-title">${escapeHtml(skill.display_name || skill.name)}</div>
                <div class="item-meta">${escapeHtml(skill.name)}${type === "general" ? ` | ${skill.risk_count || 0} risks` : type === "specific" ? ` | ${skill.target_count || 0} targets` : ` | ${skill.component_count || 0} components`}</div>
              </div>
              <div class="row">${badge(added ? "Added" : skill.valid === false ? "Invalid" : "Ready", added ? "" : skill.valid === false ? "danger" : "ok")}<button class="button" data-add-graph-skill-type="${escapeHtml(type)}" data-add-graph-skill-name="${escapeHtml(skill.name)}" ${added || skill.valid === false ? "disabled" : ""}>Add</button></div>
            </div>`;
          }).join("") || '<div class="empty">No matching skills.</div>';
          return `<details class="skill-drawer" open>${graphDrawerSummary(type, label, items.length)}<div class="skill-drawer-body">${rows}</div></details>`;
        }).join("");
        const linkedSecret = graphSecretNodes()[0];
        const secretItems = (state.secretConfigs || []).filter((config) => !query || `${config.name || ""} ${config.display_name || ""}`.toLowerCase().includes(query));
        const secretRows = secretItems.map((config) => {
          const added = isGraphSecretAdded(config.name);
          const disabled = config.valid === false;
          const action = added ? "Added" : linkedSecret ? "Replace" : "Add";
          return `<div class="library-skill secret-config-resource" draggable="false" aria-disabled="${disabled}" data-library-secret-config-name="${escapeHtml(config.name)}">
            <div>
              <div class="item-title">${escapeHtml(config.name)}</div>
              <div class="item-meta">${config.secret_count || 0} secrets | ${config.enabled ? "enabled" : "disabled"}${config.read_only ? " | template" : ""}</div>
            </div>
            <div class="row">${badge(added ? "Linked" : disabled ? "Invalid" : linkedSecret ? "Replace" : "Ready", added || linkedSecret ? "warn" : disabled ? "danger" : "ok")}<button class="button" data-add-graph-secret-config-name="${escapeHtml(config.name)}" ${disabled || added ? "disabled" : ""}>${action}</button></div>
          </div>`;
        }).join("") || '<div class="empty">No secrets configs. Create one in Secrets first.</div>';
        const secretHtml = `<details class="skill-drawer" open>${graphDrawerSummary("secrets", "Secrets configs", secretItems.length)}<div class="skill-drawer-body">${secretRows}</div></details>`;
        els.graphSkillLibrary.innerHTML = `${skillHtml}${secretHtml}`;
      }
      const GRAPH_NODE_W = 236;
      const GRAPH_NODE_H = 66;
      // Route a smooth bezier from a resource node toward the config node, attaching to
      // whichever edges face each other (AciraCloud routeEdge). Works for ANY relative
      // position — drag a node anywhere and the curve + arrow re-route cleanly.
      function routeConfigWire(node, config) {
        const sx = Number(node.x), sy = Number(node.y);
        const tx = Number(config.x), ty = Number(config.y);
        const sc = { x: sx + GRAPH_NODE_W / 2, y: sy + GRAPH_NODE_H / 2 };
        const dc = { x: tx + GRAPH_NODE_W / 2, y: ty + GRAPH_NODE_H / 2 };
        const dx = dc.x - sc.x, dy = dc.y - sc.y;
        const horizontal = Math.abs(dx) >= Math.abs(dy);
        let s, d, c1, c2;
        if (horizontal) {
          s = { x: dx >= 0 ? sx + GRAPH_NODE_W : sx, y: sc.y };
          d = { x: dx >= 0 ? tx : tx + GRAPH_NODE_W, y: dc.y };
          const off = Math.max(42, Math.abs(d.x - s.x) * 0.45);
          c1 = { x: s.x + (dx >= 0 ? off : -off), y: s.y };
          c2 = { x: d.x - (dx >= 0 ? off : -off), y: d.y };
        } else {
          s = { x: sc.x, y: dy >= 0 ? sy + GRAPH_NODE_H : sy };
          d = { x: dc.x, y: dy >= 0 ? ty : ty + GRAPH_NODE_H };
          const off = Math.max(42, Math.abs(d.y - s.y) * 0.45);
          c1 = { x: s.x, y: s.y + (dy >= 0 ? off : -off) };
          c2 = { x: d.x, y: d.y - (dy >= 0 ? off : -off) };
        }
        return {
          path: `M ${s.x.toFixed(1)} ${s.y.toFixed(1)} C ${c1.x.toFixed(1)} ${c1.y.toFixed(1)}, ${c2.x.toFixed(1)} ${c2.y.toFixed(1)}, ${d.x.toFixed(1)} ${d.y.toFixed(1)}`,
          s, d
        };
      }
      function buildGraphWiresInner(resourceNodes, config) {
        const wireDefs = `<defs>`
          + `<linearGradient id="acira-edge" x1="0" y1="0" x2="1" y2="0"><stop offset="0%" stop-color="#6ee7ff"/><stop offset="55%" stop-color="#3cc5ff"/><stop offset="100%" stop-color="#2f80ff"/></linearGradient>`
          + `<linearGradient id="acira-edge-teal" x1="0" y1="0" x2="1" y2="0"><stop offset="0%" stop-color="#6ee7c2"/><stop offset="100%" stop-color="#10b981"/></linearGradient>`
          + `</defs>`;
        return wireDefs + (resourceNodes || []).map((node) => {
          const isSecret = node.type === "secret_config";
          const r = routeConfigWire(node, config);
          const grad = isSecret ? "acira-edge-teal" : "acira-edge";
          const color = isSecret ? "#10b981" : "#2f80ff";
          return `<path class="graph-wire-halo${isSecret ? " graph-wire-secret" : ""}" d="${r.path}" />`
            + `<path class="graph-wire${isSecret ? " graph-wire-secret" : ""}" d="${r.path}" stroke="url(#${grad})" />`
            + `<path class="graph-wire-flow${isSecret ? " graph-wire-secret" : ""}" d="${r.path}" stroke="url(#${grad})" />`
            + `<circle class="graph-port graph-port-out" cx="${r.s.x.toFixed(1)}" cy="${r.s.y.toFixed(1)}" r="4.5" style="--port:${color}" />`
            + `<circle class="graph-port graph-port-in" cx="${r.d.x.toFixed(1)}" cy="${r.d.y.toFixed(1)}" r="6" style="--port:${color}" />`
            + `<circle class="graph-port-core" cx="${r.d.x.toFixed(1)}" cy="${r.d.y.toFixed(1)}" r="2.4" style="--port:${color}" />`;
        }).join("");
      }
      // Live-redraw only the wires (used during node drag so the dragged DOM node isn't rebuilt).
      function redrawGraphWires() {
        if (!state.currentGraph) return;
        const svg = els.graph.querySelector(".graph-lines");
        if (!svg) return;
        const nodes = state.currentGraph.nodes || [];
        const config = nodes.find((node) => node.id === "config") || { x: 420, y: 245 };
        svg.innerHTML = buildGraphWiresInner(graphResourceNodes(), config);
      }
      function graphNodeById(id) {
        return state.currentGraph && (state.currentGraph.nodes || []).find((node) => node.id === id);
      }
      function renderGraphCanvas() {
        if (!state.currentGraph) {
          els.graph.innerHTML = '<div class="graph-empty">Open a config to compose its enabled skills.</div>';
          return;
        }
        state.currentGraph = normalizeConfigGraph(state.currentGraph, els.configName.value.trim(), state.currentGraph.nodes?.[0]?.label);
        const nodes = state.currentGraph.nodes || [];
        const config = nodes.find((node) => node.id === "config") || { x: 420, y: 245 };
        const skillNodes = nodes.filter((node) => node.type === "skill");
        const resourceNodes = graphResourceNodes();
        const contentX = Math.round(Math.max(GRAPH_NODE_W + 60, ...nodes.map((node) => Number(node.x || 0) + GRAPH_NODE_W)) + 28);
        const contentY = Math.round(Math.max(GRAPH_NODE_H + 60, ...nodes.map((node) => Number(node.y || 0) + GRAPH_NODE_H)) + 28);
        const lines = buildGraphWiresInner(resourceNodes, config);
        const ICONS = {
          config: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z"/></svg>`,
          secret: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="8" cy="15" r="4"/><path d="m21 3-9 9M15 9l3 3"/></svg>`,
          general: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.6 2.7 2.6 15.3 0 18M12 3c-2.6 2.7-2.6 15.3 0 18"/></svg>`,
          specific: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2.5"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></svg>`,
          merged: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m12 3 9 5-9 5-9-5 9-5z"/><path d="m3 13 9 5 9-5"/></svg>`,
          skill: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2 5 13h6l-1 9 8-12h-6z"/></svg>`
        };
        const nodeHtml = nodes.map((node) => {
          const selected = state.selectedGraphNodeId === node.id ? " selected" : "";
          const skill = node.type === "skill" ? skillSourceFor(node.skill_type).find((item) => item.name === node.name) : null;
          const secret = node.type === "secret_config" ? secretConfigSourceFor(node.name) : null;
          const title = node.type === "config" ? node.label || "Config" : node.type === "secret_config" ? node.name : (skill && skill.display_name) || node.name;
          const meta = node.type === "config" ? "Config root" : node.type === "secret_config" ? `${(secret && secret.secret_count) || 0} secrets | ${secret && secret.enabled ? "enabled" : "disabled"}` : `${node.skill_type} | ${node.name}`;
          const nodeClass = node.type === "config" ? "config" : node.type === "secret_config" ? "secret" : "";
          const icon = node.type === "config" ? ICONS.config : node.type === "secret_config" ? ICONS.secret : (ICONS[node.skill_type] || ICONS.skill);
          return `<button class="node ${nodeClass}${selected}" draggable="false" data-graph-node="${escapeHtml(node.id)}" style="left:${Number(node.x)}px;top:${Number(node.y)}px">
            ${node.type !== "config" ? `<span class="button danger node-remove" data-remove-graph-node="${escapeHtml(node.id)}">×</span>` : ""}
            <span class="node-ico">${icon}</span>
            <div class="node-copy"><div class="item-title">${escapeHtml(title)}</div><div class="item-meta">${escapeHtml(meta)}</div></div>
          </button>`;
        }).join("");
        const empty = resourceNodes.length ? "" : '<div class="graph-empty">No resources connected yet. Drag skills or a secrets config from the library.</div>';
        // Scale-to-fit + center the graph inside the CLEAR region of the canvas — i.e. the
        // area not covered by the floating top bar or the floating side panels — so the whole
        // graph is always visible, never scrolls, and never hides under a popup.
        const cw = els.graph.clientWidth || 760;
        const ch = els.graph.clientHeight || 520;
        let clTop = 14, clRight = 14;
        const clLeft = 14, clBottom = 14;
        const stageEl = els.graph.closest(".config-stage");
        if (stageEl) {
          const gr = els.graph.getBoundingClientRect();
          const bar = stageEl.querySelector(".stage-bar");
          if (bar) { const br = bar.getBoundingClientRect(); if (br.height) clTop = Math.max(clTop, br.bottom - gr.top + 14); }
          let minPanelLeft = gr.right;
          stageEl.querySelectorAll(".stage-panel").forEach((p) => { const pr = p.getBoundingClientRect(); if (pr.width > 0) minPanelLeft = Math.min(minPanelLeft, pr.left); });
          if (minPanelLeft < gr.right) clRight = Math.max(clRight, gr.right - minPanelLeft + 14);
        }
        const usableW = Math.max(220, cw - clLeft - clRight);
        const usableH = Math.max(180, ch - clTop - clBottom);
        const scale = Math.min(1, usableW / contentX, usableH / contentY);
        const offX = Math.round(clLeft + Math.max(0, (usableW - contentX * scale) / 2));
        const offY = Math.round(clTop + Math.max(0, (usableH - contentY * scale) / 2));
        state.graphScale = scale;
        state.graphOffX = offX;
        state.graphOffY = offY;
        state.graphMaxX = usableW / scale;
        state.graphMaxY = usableH / scale;
        els.graph.innerHTML = `<div class="graph-world" style="width:${cw}px;height:${ch}px">`
          + `<div class="graph-scaled" style="width:${contentX}px;height:${contentY}px;transform:translate(${offX}px,${offY}px) scale(${scale.toFixed(4)})">`
          + `<svg class="graph-lines">${lines}</svg>${nodeHtml}${empty}`
          + `</div></div>`;
        applyGraphUserTransform();
      }
      function applyGraphUserTransform() {
        const scaled = els.graph && els.graph.querySelector('.graph-scaled');
        if (!scaled) return;
        const baseScale = state.graphScale || 1;
        const s = baseScale * (state.graphUserScale || 1);
        const ox = (state.graphOffX || 0) + (state.graphUserPanX || 0);
        const oy = (state.graphOffY || 0) + (state.graphUserPanY || 0);
        scaled.style.transform = `translate(${ox}px,${oy}px) scale(${s.toFixed(4)})`;
      }
      function renderGraphInspector() {
        if (!els.graphInspector) return;
        if (!state.currentGraph) {
          els.graphInspector.innerHTML = '<div class="empty">Open a config to inspect it.</div>';
          return;
        }
        const nodes = state.currentGraph.nodes || [];
        const selected = nodes.find((node) => node.id === state.selectedGraphNodeId) || nodes.find((node) => node.id === "config");
        if (!selected || selected.type === "config") {
          const skills = graphSkillNodes();
          const secretNode = graphSecretNodes()[0];
          const mergeHint = skills.length > 1 ? '<div class="inline-error" style="border-color:#fed7aa;background:#fffbeb;color:#92400e">Multiple skills are connected. If these usually travel together, create a merged skill from the Skills tab.</div>' : "";
          const secretSummary = secretNode
            ? `<div class="event"><div class="event-title"><span>${escapeHtml(secretNode.name)}</span>${badge("Linked", "ok")}</div><div class="event-meta">Managed as a graph node. Select it to inspect or remove it.</div></div>`
            : '<div class="empty">No secrets config linked. Add one from the Secrets configs library section.</div>';
          els.graphInspector.innerHTML = `
            <div class="item-title">${escapeHtml(els.configName.value || "Config")}</div>
            <div class="item-meta">${skills.length} connected skills${secretNode ? " | 1 secrets config" : ""}</div>
            <div class="kv-grid">
              <div class="kv"><div class="label">General</div><div class="value">${(state.currentGraph.general_skills || []).length}</div></div>
              <div class="kv"><div class="label">Specific</div><div class="value">${(state.currentGraph.specific_skills || []).length}</div></div>
              <div class="kv"><div class="label">Merged</div><div class="value">${(state.currentGraph.merged_skills || []).length}</div></div>
              <div class="kv"><div class="label">Secrets</div><div class="value">${secretNode ? "Linked" : "None"}</div></div>
            </div>
            ${secretSummary}
            ${mergeHint}
          `;
          return;
        }
        if (selected.type === "secret_config") {
          const config = secretConfigSourceFor(selected.name);
          els.graphInspector.innerHTML = `
            <div class="component-kind">Secrets config</div>
            <div class="item-title">${escapeHtml(selected.name)}</div>
            <div class="item-meta">${config.read_only ? "Template" : "Local config"} | ${config.enabled ? "enabled" : "disabled"}</div>
            <div class="kv-grid">
              <div class="kv"><div class="label">Secrets</div><div class="value">${config.secret_count || 0}</div></div>
              <div class="kv"><div class="label">Status</div><div class="value">${config.valid === false ? "Invalid" : "Ready"}</div></div>
            </div>
            <button class="button danger" data-remove-graph-node="${escapeHtml(selected.id)}">Remove from config</button>
          `;
          return;
        }
        const skill = skillSourceFor(selected.skill_type).find((item) => item.name === selected.name) || {};
        els.graphInspector.innerHTML = `
          <div class="component-kind">${escapeHtml(selected.skill_type)}</div>
          <div class="item-title">${escapeHtml(skill.display_name || selected.name)}</div>
          <div class="item-meta">${escapeHtml(selected.name)}</div>
          <div class="kv-grid">
            <div class="kv"><div class="label">Status</div><div class="value">${skill.valid === false ? "Invalid" : "Ready"}</div></div>
            <div class="kv"><div class="label">Source</div><div class="value">${escapeHtml(selected.skill_type)}</div></div>
          </div>
          <button class="button danger" data-remove-graph-node="${escapeHtml(selected.id)}">Remove from config</button>
        `;
      }
      function addGraphSkill(type, name, position = {}) {
        if (!state.currentGraph) return;
        if (isGraphSkillAdded(type, name)) return;
        const index = graphSkillNodes().length;
        const node = { id: `skill-${type}-${safeDomId(name)}`, type: "skill", skill_type: type, name, path: name, x: Number(position.x ?? 0), y: Number(position.y ?? 0), pinned: !!position.manual };
        state.currentGraph.nodes.push(node);
        state.currentGraph.edges.push({ from: node.id, to: "config" });
        applyGraphAutoLayout(state.currentGraph, { onlyUnpinned: true });
        state.selectedGraphNodeId = node.id;
        syncGraphSkillLists();
        renderGraph();
        persistGraphSoon({ immediate: true, toast: "Skill linked to config." });
      }
      function addGraphSecretConfig(name, position = {}) {
        if (!state.currentGraph || !name) return;
        const oldSecretIds = new Set(graphSecretNodes().map((node) => node.id));
        state.currentGraph.nodes = (state.currentGraph.nodes || []).filter((node) => node.type !== "secret_config");
        state.currentGraph.edges = (state.currentGraph.edges || []).filter((edge) => !oldSecretIds.has(edge.from) && !oldSecretIds.has(edge.to));
        const node = { id: `secret-config-${safeDomId(name)}`, type: "secret_config", name, path: name, x: Number(position.x ?? 0), y: Number(position.y ?? 0), pinned: !!position.manual };
        state.currentGraph.nodes.push(node);
        state.currentGraph.edges.push({ from: node.id, to: "config" });
        state.currentSecretLink = name;
        syncGraphSkillLists();
        applyGraphAutoLayout(state.currentGraph, { onlyUnpinned: true });
        state.selectedGraphNodeId = node.id;
        renderGraph();
        persistGraphSoon({ immediate: true, toast: "Secrets config linked." });
      }
      function removeGraphNode(id) {
        if (!state.currentGraph || id === "config") return;
        state.currentGraph.nodes = (state.currentGraph.nodes || []).filter((node) => node.id !== id);
        state.currentGraph.edges = (state.currentGraph.edges || []).filter((edge) => edge.from !== id && edge.to !== id);
        state.selectedGraphNodeId = "config";
        syncGraphSkillLists();
        renderGraph();
        persistGraphSoon({ immediate: true, toast: "Removed from config." });
      }
      function moveGraphNode(id, position) {
        const node = state.currentGraph && (state.currentGraph.nodes || []).find((item) => item.id === id && (item.type === "skill" || item.type === "secret_config"));
        if (!node) return;
        node.x = Number(position.x);
        node.y = Number(position.y);
        node.pinned = true;
        renderGraph();
      }
      function graphDropPosition(event) {
        const rect = els.graph.getBoundingClientRect();
        const baseScale = state.graphScale || 1;
        const userScale = state.graphUserScale || 1;
        const s = baseScale * userScale;
        const x = (event.clientX - rect.left + els.graph.scrollLeft - (state.graphOffX || 0) - (state.graphUserPanX || 0)) / s;
        const y = (event.clientY - rect.top + els.graph.scrollTop - (state.graphOffY || 0) - (state.graphUserPanY || 0)) / s;
        return { x: Math.max(12, Math.round(x - GRAPH_NODE_W / 2)), y: Math.max(12, Math.round(y - GRAPH_NODE_H / 2)), manual: true };
      }
      function autoArrangeGraph() {
        if (!state.currentGraph) return;
        state.graphUserScale = 1; state.graphUserPanX = 0; state.graphUserPanY = 0;
        graphResourceNodes().forEach((node) => { node.pinned = false; });
        applyGraphAutoLayout(state.currentGraph, { onlyUnpinned: false });
        renderGraph();
        persistGraphSoon();
      }
      // Auto-save the config graph to the backend (debounced for moves, immediate for structural changes).
      let graphPersistTimer = null;
      function persistGraphSoon(opts = {}) {
        if (!state.currentGraph) return;
        clearTimeout(graphPersistTimer);
        const run = () => {
          if (!state.currentGraph) return;
          const name = els.configName.value.trim();
          if (!name) return;
          syncGraphSkillLists();
          state.currentGraph = normalizeConfigGraph(state.currentGraph, name, state.currentGraph.nodes?.[0]?.label);
          api(`/api/configs/${encodeURIComponent(name)}/graph`, { method: "PUT", body: JSON.stringify({ graph: state.currentGraph }), silent: true })
            .then(() => { if (opts.toast) showToast(opts.toast); })
            .catch((err) => showToast(err.message || String(err)));
        };
        if (opts.immediate) run();
        else graphPersistTimer = setTimeout(run, 550);
      }
      function syncGraphSkillLists() {
        if (!state.currentGraph) return;
        const byType = { general: [], specific: [], merged: [] };
        graphSkillNodes().forEach((node) => { if (byType[node.skill_type]) byType[node.skill_type].push(node.name); });
        state.currentGraph.general_skills = dedupeNames(byType.general);
        state.currentGraph.specific_skills = dedupeNames(byType.specific);
        state.currentGraph.merged_skills = dedupeNames(byType.merged);
        const secretNode = graphSecretNodes()[0];
        state.currentGraph.secrets_config_path = secretNode ? secretNode.name : "";
        state.currentSecretLink = state.currentGraph.secrets_config_path;
      }
      function syncConfigEditorFromGraph() {
        if (!state.currentGraph) return;
        let data = {};
        try { data = JSON.parse(els.configEditor.value || "{}"); } catch { data = {}; }
        syncGraphSkillLists();
        data.general_skills = (state.currentGraph.general_skills || []).map((name) => `../skills/general/${name}`);
        data.specific_skills = (state.currentGraph.specific_skills || []).map((name) => `../skills/specific/${name}`);
        if (state.currentGraph.secrets_config_path) data.secrets_config_path = state.currentGraph.secrets_config_path;
        else delete data.secrets_config_path;
        delete data.secrets_config_file;
        els.configEditor.value = JSON.stringify(data, null, 2);
      }
      function syncGraphFromConfigEditor() {
        if (!els.configName.value.trim()) return;
        const data = configDataFromEditor();
        const names = (items) => (Array.isArray(items) ? items : []).map((item) => String(item).split(/[\\/]/).pop()).filter(Boolean);
        state.currentGraph = normalizeConfigGraph({
          schema: "acira.config_graph.v1",
          config: els.configName.value.trim(),
          general_skills: names(data.general_skills || data.general_skill_paths),
          specific_skills: names(data.specific_skills || data.specific_skill_paths),
          merged_skills: (state.currentGraph && state.currentGraph.merged_skills) || [],
          secrets_config_path: data.secrets_config_path || data.secrets_config_file || ""
        }, els.configName.value.trim(), data.name || els.configName.value.trim());
        state.selectedGraphNodeId = "config";
        renderGraph();
      }
      async function saveGraph() {
        if (!state.currentGraph) throw new Error("Open a config first.");
        syncGraphSkillLists();
        applyGraphAutoLayout(state.currentGraph, { onlyUnpinned: true });
        state.currentGraph = normalizeConfigGraph(state.currentGraph, els.configName.value.trim(), state.currentGraph.nodes?.[0]?.label);
        await api(`/api/configs/${encodeURIComponent(els.configName.value.trim())}/graph`, { method: "PUT", body: JSON.stringify({ graph: state.currentGraph }) });
        showToast("Config graph saved.");
        await openConfig(els.configName.value.trim());
      }

      async function loadSecrets() {
        try {
          const data = await api("/api/secrets/configs");
          state.secretConfigs = Array.isArray(data.configs) ? data.configs : [];
          setInlineError(els.secretError, "");
          renderSecretConfigList();
          renderGraphSkillLibrary();
          if (state.currentGraph) renderGraphCanvas();
          renderGraphInspector();
        } catch (err) {
          setInlineError(els.secretError, `Secrets API error: ${err.message || err}`);
          renderSecretConfigList();
          throw err;
        }
      }
      function renderSecretConfigList() {
        els.secretConfigList.innerHTML = state.secretConfigs.map((config) => {
          const stateText = config.read_only ? "template" : config.valid ? "valid" : "invalid";
          return `<button class="list-item" data-secret-config="${escapeHtml(config.name)}"><div class="event-title"><span class="item-title">${escapeHtml(config.name)}</span>${badge(stateText, config.read_only ? "warn" : config.valid ? "ok" : "danger")}</div><div class="item-meta">${config.secret_count || 0} secrets | ${config.enabled ? "enabled" : "disabled"}</div></button>`;
        }).join("") || '<div class="empty">No secrets configs. Create a local secrets config to start.</div>';
      }
      function newSecretConfig() {
        const name = normalizeSecretConfigName(els.secretConfigName.value || "dashboard");
        state.currentSecretConfig = { name, read_only: false, data: { enabled: true, default_decision: "ask", secrets: [] } };
        state.currentSecretRecordId = null;
        els.secretConfigName.value = name;
        showSecretDetail();
        renderSecretConfig();
      }
      async function openSecretConfig(name) {
        const data = await api(`/api/secrets/configs/${encodeURIComponent(name)}`);
        state.currentSecretConfig = data;
        state.currentSecretRecordId = null;
        els.secretConfigName.value = data.name;
        showSecretDetail();
        renderSecretConfig();
      }
      function showSecretIndex() {
        els.secretConfigIndex.classList.remove("hidden");
        els.secretConfigDetail.classList.add("hidden");
        state.currentSecretConfig = null;
        state.currentSecretRecordId = null;
        clearSecretRecordForm();
      }
      function showSecretDetail() {
        els.secretConfigIndex.classList.add("hidden");
        els.secretConfigDetail.classList.remove("hidden");
        state.currentSecretTab = "settings";
        byId("secret-settings-tab").classList.add("active");
        byId("secret-json-tab").classList.remove("active");
        els.secretSettingsPane.classList.remove("hidden");
        els.secretJsonPane.classList.add("hidden");
      }
      function renderSecretConfig() {
        const config = state.currentSecretConfig;
        if (!config) {
          els.secretConfigEditor.value = "";
          els.secretConfigState.innerHTML = '<div class="empty">Select or create a secrets config.</div>';
          els.secretRecordList.innerHTML = "";
          if (els.secretRecordCount) els.secretRecordCount.textContent = "0";
          clearSecretRecordForm();
          return;
        }
        els.secretConfigEditor.value = JSON.stringify(config.data || {}, null, 2);
        els.secretConfigState.innerHTML = `${config.valid === false ? badge(config.error || "Invalid", "danger") : badge(config.read_only ? "Template" : "Local config", config.read_only ? "warn" : "ok")} ${badge(config.name)}`;
        els.secretConfigEnabled.checked = config.data ? config.data.enabled !== false : true;
        els.secretDefaultDecision.value = (config.data && config.data.default_decision) || "ask";
        els.secretConfigEnabled.disabled = !!config.read_only;
        els.secretDefaultDecision.disabled = !!config.read_only;
        els.secretReadonlyBanner.classList.toggle("hidden", !config.read_only);
        byId("save-secret-config").disabled = !!config.read_only;
        byId("delete-secret-config").disabled = !!config.read_only;
        const records = secretRecords(config.data || {});
        els.secretRecordCount.textContent = String(records.length);
        els.secretRecordList.innerHTML = records.map(renderSecretRecordCard).join("") || '<div class="empty">No existing secrets yet. Use Add secret to create one.</div>';
        const helper = byId("secret-record-helper");
        if (helper) helper.textContent = config.read_only ? "Template records are examples. Create a local copy to add real secrets." : "Values stay hidden. Select a record to edit metadata or replace its value.";
        if (state.currentSecretRecordId && !records.some((secret) => secret.id === state.currentSecretRecordId)) state.currentSecretRecordId = null;
        if (state.currentSecretRecordId) populateSecretRecordForm(state.currentSecretRecordId);
        else renderNewSecretForm();
      }
      function renderSecretRecordCard(secret) {
        const selected = state.currentSecretRecordId === secret.id ? " active" : "";
        const decision = (secret.policy || {}).decision || "ask";
        const readOnlyTemplate = !!(state.currentSecretConfig && state.currentSecretConfig.read_only);
        const valueBadge = readOnlyTemplate ? badge("Example record", "warn") : badge(secretValueStatus(secret), secretHasStoredValue(secret) ? "ok" : "warn");
        return `<button class="secret-record-card${selected}" data-secret-record="${escapeHtml(secret.id)}">
          <div class="event-title"><span class="item-title">${escapeHtml(secret.label || secret.id)}</span>${badge(secret.active ? "Active" : "Inactive", secret.active ? "ok" : "warn")}</div>
          <div class="item-meta">${escapeHtml(secret.id)} | ${escapeHtml(secret.classification || "unclassified")} | ${escapeHtml(decision)}</div>
          <div class="row">${valueBadge}${!readOnlyTemplate && (secret.provider || secret.provider_ref) ? badge("Provider") : ""}</div>
        </button>`;
      }
      function secretHasStoredValue(secret) {
        return secret.value === "[REDACTED]" || !!(secret.provider || secret.provider_id || secret.ref || secret.provider_ref);
      }
      function secretValueStatus(secret) {
        if (secret.value === "[REDACTED]") return "Stored value hidden";
        if (secret.provider || secret.provider_id || secret.ref || secret.provider_ref) return "Provider-backed";
        return "No local value";
      }
      async function saveSecretConfig() {
        if (state.currentSecretConfig && state.currentSecretConfig.read_only) throw new Error("Template secrets configs are read-only. Create a local config first.");
        const name = state.currentSecretConfig && els.secretConfigName.value === state.currentSecretConfig.name ? state.currentSecretConfig.name : normalizeSecretConfigName(els.secretConfigName.value);
        const data = state.currentSecretTab === "json" ? JSON.parse(els.secretConfigEditor.value || "{}") : secretConfigFromSettings();
        const saved = await api(`/api/secrets/configs/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify(data) });
        state.currentSecretConfig = saved;
        els.secretConfigName.value = saved.name;
        showToast("Secrets config saved.");
        renderSecretConfig();
        await loadSecrets();
      }
      async function copySecretConfigToLocal() {
        if (!state.currentSecretConfig || !state.currentSecretConfig.read_only) throw new Error("Open a read-only template first.");
        const name = uniqueLocalSecretConfigName(localSecretCopyName(state.currentSecretConfig.name));
        const sourceData = state.currentSecretConfig.data || {};
        const data = {
          enabled: sourceData.enabled === true,
          default_decision: sourceData.default_decision || "ask",
          secrets: []
        };
        const saved = await api(`/api/secrets/configs/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify(data) });
        state.currentSecretConfig = saved;
        state.currentSecretRecordId = null;
        els.secretConfigName.value = saved.name;
        showToast("Editable local secrets config created.");
        showSecretDetail();
        renderSecretConfig();
        await loadSecrets();
      }
      function localSecretCopyName(name) {
        const text = String(name || "dashboard").trim();
        if (text === "acira.secrets.example.json") return "acira.secrets.local.json";
        return normalizeSecretConfigName(text.replace(/\.example\.json$/i, "").replace(/\.secrets\.example$/i, ""));
      }
      function uniqueLocalSecretConfigName(preferred) {
        const existing = new Set((state.secretConfigs || []).map((config) => config.name));
        if (!existing.has(preferred)) return preferred;
        const stem = preferred.replace(/\.secrets\.local\.json$/i, "");
        for (let index = 2; index < 100; index += 1) {
          const candidate = `${stem}-${index}.secrets.local.json`;
          if (!existing.has(candidate)) return candidate;
        }
        return `dashboard-${Date.now()}.secrets.local.json`;
      }
      function secretConfigFromSettings() {
        const data = Object.assign({}, (state.currentSecretConfig && state.currentSecretConfig.data) || {});
        data.enabled = !!els.secretConfigEnabled.checked;
        data.default_decision = els.secretDefaultDecision.value || "ask";
        data.secrets = secretRecords(data);
        return data;
      }
      function updateSecretConfigSettingsFromControls() {
        if (!state.currentSecretConfig || state.currentSecretConfig.read_only) return;
        state.currentSecretConfig.data = secretConfigFromSettings();
        els.secretConfigEditor.value = JSON.stringify(state.currentSecretConfig.data || {}, null, 2);
      }
      function renderNewSecretForm() {
        state.currentSecretRecordId = null;
        els.secretEditorTitle.textContent = "Add secret";
        els.secretEditorHelp.textContent = state.currentSecretConfig && state.currentSecretConfig.read_only ? "Templates are read-only. Create a local config before adding secrets." : "Create a new local secret record.";
        clearSecretRecordForm();
        setSecretRecordControlsDisabled(!!(state.currentSecretConfig && state.currentSecretConfig.read_only));
        byId("delete-secret").disabled = true;
      }
      function populateSecretRecordForm(id) {
        const secret = secretRecords((state.currentSecretConfig || {}).data || {}).find((item) => item.id === id);
        if (!secret) { renderNewSecretForm(); return; }
        state.currentSecretRecordId = secret.id;
        els.secretEditorTitle.textContent = "Edit secret";
        els.secretEditorHelp.textContent = secretHasStoredValue(secret) ? "Plaintext is stored locally and hidden. Leave value blank to keep it." : "No local value is stored. Add a plaintext value or provider reference.";
        byId("secret-id").value = secret.id;
        byId("secret-label").value = secret.label || "";
        byId("secret-value").value = "";
        byId("secret-classification").value = secret.classification || "";
        byId("secret-decision").value = (secret.policy || {}).decision || "ask";
        setSecretRecordControlsDisabled(!!(state.currentSecretConfig && state.currentSecretConfig.read_only));
        byId("secret-id").disabled = true;
        byId("delete-secret").disabled = !!(state.currentSecretConfig && state.currentSecretConfig.read_only);
      }
      function setSecretRecordControlsDisabled(disabled) {
        ["secret-id", "secret-label", "secret-value", "secret-classification", "secret-decision", "save-secret", "clear-secret-form", "new-secret-record"].forEach((id) => {
          const el = byId(id);
          if (el) el.disabled = disabled;
        });
      }
      async function deleteSecretConfig() {
        if (!state.currentSecretConfig) throw new Error("Select a secrets config first.");
        if (state.currentSecretConfig.read_only) throw new Error("Template secrets configs are read-only.");
        await api(`/api/secrets/configs/${encodeURIComponent(state.currentSecretConfig.name)}`, { method: "DELETE" });
        state.currentSecretConfig = null;
        showToast("Secrets config deleted.");
        showSecretIndex();
        await loadSecrets();
      }
      function openSecretRecord(id) {
        if (!state.currentSecretConfig) return;
        state.currentSecretRecordId = id;
        renderSecretConfig();
      }
      function clearSecretRecordForm() {
        ["secret-id", "secret-label", "secret-value", "secret-classification"].forEach((id) => { const el = byId(id); if (el) el.value = ""; });
        if (byId("secret-decision")) byId("secret-decision").value = "ask";
        if (byId("secret-id")) byId("secret-id").disabled = false;
      }
      async function saveSecret() {
        if (!state.currentSecretConfig) throw new Error("Select a secrets config first.");
        if (state.currentSecretConfig.read_only) throw new Error("Template secrets configs are read-only. Create a local config first.");
        const id = byId("secret-id").value.trim();
        if (!id) throw new Error("Secret ID is required.");
        const saved = await api(`/api/secrets/configs/${encodeURIComponent(state.currentSecretConfig.name)}/secrets/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify({ label: byId("secret-label").value, value: byId("secret-value").value, active: true, classification: byId("secret-classification").value, policy: { decision: byId("secret-decision").value } }) });
        state.currentSecretConfig = saved;
        state.currentSecretRecordId = id;
        byId("secret-value").value = "";
        showToast("Secret record saved.");
        renderSecretConfig();
        await loadSecrets();
      }
      async function deleteSecret() {
        if (!state.currentSecretConfig) throw new Error("Select a secrets config first.");
        if (state.currentSecretConfig.read_only) throw new Error("Template secrets configs are read-only.");
        const id = state.currentSecretRecordId || byId("secret-id").value.trim();
        if (!id) throw new Error("Select a secret record first.");
        const saved = await api(`/api/secrets/configs/${encodeURIComponent(state.currentSecretConfig.name)}/secrets/${encodeURIComponent(id)}`, { method: "DELETE" });
        state.currentSecretConfig = saved;
        state.currentSecretRecordId = null;
        showToast("Secret record deleted.");
        renderSecretConfig();
        await loadSecrets();
      }
      function secretRecords(data) {
        return Array.isArray(data.secrets) ? data.secrets : Array.isArray(data.items) ? data.items : Array.isArray(data.entries) ? data.entries : [];
      }
      function normalizeSecretConfigName(name) {
        const stem = String(name || "").trim().replace(/\.json$/i, "").replace(/\.secrets\.local$/i, "");
        if (!stem) return "dashboard.secrets.local.json";
        return stem.endsWith(".secrets.local") ? `${stem}.json` : `${stem}.secrets.local.json`;
      }

      function setSessionFilter(filter, options = {}) {
        state.sessionFilter = filter || "live";
        renderSessions();
        if (options.load !== false) loadSessions().catch((err) => showToast(err.message));
      }
      function handleEventClick(event) {
        const button = event.target.closest("[data-open-session], [data-open-review]");
        if (!button) return;
        const reviewId = button.dataset.openReview;
        const sessionId = button.dataset.openSession;
        if (reviewId) {
          setView("hitl");
          openReview(reviewId).catch((err) => showToast(err.message));
          return;
        }
        if (sessionId) {
          setSessionFilter("all", { load: false });
          setView("sessions");
          openSession(sessionId).catch((err) => showToast(err.message));
        }
      }

      const SKILL_INTRO = {
        general: {
          icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.6 2.7 2.6 15.3 0 18M12 3c-2.6 2.7-2.6 15.3 0 18"/></svg>`,
          title: "General skills",
          desc: "Built-in protections that apply on every page — checkout, credentials, destructive actions and downloads. These ship with Acira. Select one to see the rules it enforces."
        },
        specific: {
          icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2.5"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></svg>`,
          title: "Specific skills",
          desc: "Rules captured for one application with the Acira Capture browser extension. Create or import one here, then attach it to a config."
        },
        merged: {
          icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m12 3 9 5-9 5-9-5 9-5z"/><path d="m3 13 9 5 9-5"/></svg>`,
          title: "Merged skills",
          desc: "Bundle several skills into one reusable set. Press New, add skills from the list on the right, then Save — now you can attach the whole bundle to a config in one step."
        }
      };
      function switchSkillTab(name) {
        ["general", "specific", "merged"].forEach((tab) => {
          byId(`tab-${tab}`).classList.toggle("active", tab === name);
          byId(`skills-${tab}`).classList.toggle("hidden", tab !== name);
        });
        const intro = SKILL_INTRO[name];
        const introEl = byId("skills-intro");
        if (intro && introEl) introEl.innerHTML = `<span class="section-intro-ico">${intro.icon}</span><div class="section-intro-copy"><h3>${escapeHtml(intro.title)}</h3><p>${escapeHtml(intro.desc)}</p></div>`;
      }
      function switchSpecificTab(name) {
        if (name === "targets" && state.currentSpecificTab === "json" && state.currentSpecificSkill) {
          try {
            state.currentSpecificSkill.data = JSON.parse(els.specificEditor.value || "{}");
          } catch (err) {
            showToast(`Invalid JSON: ${err.message || err}`);
            return;
          }
        }
        state.currentSpecificTab = name;
        byId("specific-targets-tab").classList.toggle("active", name === "targets");
        byId("specific-json-tab").classList.toggle("active", name === "json");
        els.specificTargetsPane.classList.toggle("hidden", name !== "targets");
        els.specificJsonPane.classList.toggle("hidden", name !== "json");
      }
      function switchMergedTab(name) {
        if (name === "components" && state.currentMergedTab === "json" && state.currentMergedSkill) {
          try {
            state.currentMergedSkill.data = normalizeMergedSkillData(JSON.parse(els.mergedEditor.value || "{}"));
          } catch (err) {
            showToast(`Invalid JSON: ${err.message || err}`);
            return;
          }
          renderMergedSkill();
        }
        if (name === "json" && state.currentMergedSkill) {
          state.currentMergedSkill.data = normalizeMergedSkillData(state.currentMergedSkill.data || {});
          els.mergedEditor.value = JSON.stringify(state.currentMergedSkill.data, null, 2);
        }
        state.currentMergedTab = name;
        byId("merged-components-tab").classList.toggle("active", name === "components");
        byId("merged-json-tab").classList.toggle("active", name === "json");
        els.mergedComponentsPane.classList.toggle("hidden", name !== "components");
        els.mergedJsonPane.classList.toggle("hidden", name !== "json");
      }
      function switchConfigTab(name) {
        if (name === "json" && state.currentConfigTab === "graph") syncConfigEditorFromGraph();
        if (name === "graph" && state.currentConfigTab === "json") {
          try {
            syncGraphFromConfigEditor();
          } catch (err) {
            showToast(`Invalid JSON: ${err.message || err}`);
            return;
          }
        }
        state.currentConfigTab = name;
        byId("config-json-tab").classList.toggle("active", name === "json");
        byId("config-graph-tab").classList.toggle("active", name === "graph");
        byId("config-json-pane").classList.toggle("hidden", name !== "json");
        byId("config-graph-pane").classList.toggle("hidden", name !== "graph");
        byId("view-configs").classList.toggle("json-mode", name === "json");
      }
      function switchSecretTab(name) {
        if (name === "settings" && state.currentSecretTab === "json" && state.currentSecretConfig) {
          try {
            state.currentSecretConfig.data = JSON.parse(els.secretConfigEditor.value || "{}");
          } catch (err) {
            showToast(`Invalid JSON: ${err.message || err}`);
            return;
          }
          renderSecretConfig();
        }
        if (name === "json" && state.currentSecretConfig) {
          state.currentSecretConfig.data = secretConfigFromSettings();
          els.secretConfigEditor.value = JSON.stringify(state.currentSecretConfig.data || {}, null, 2);
        }
        state.currentSecretTab = name;
        byId("secret-settings-tab").classList.toggle("active", name === "settings");
        byId("secret-json-tab").classList.toggle("active", name === "json");
        els.secretSettingsPane.classList.toggle("hidden", name !== "settings");
        els.secretJsonPane.classList.toggle("hidden", name !== "json");
      }
      function connectStream() {
        if (location.protocol === "file:") {
          showFileModeWarning();
          return;
        }
        clearTimeout(streamReconnectTimer);
        const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/events/stream`);
        ws.addEventListener("open", () => { setLiveState("Live", true); stopPollingFallback(); });
        ws.addEventListener("close", () => { setLiveState("Reconnecting", false); startPollingFallback(); streamReconnectTimer = setTimeout(connectStream, 1500); });
        ws.addEventListener("error", () => { setLiveState("API error", false); startPollingFallback(); });
        ws.addEventListener("message", (message) => {
          let payload = {};
          try { payload = JSON.parse(message.data); }
          catch { setLiveState("API error", false); return; }
          if (payload.type === "snapshot") {
            state.events = Array.isArray(payload.events) ? payload.events : [];
            state.events.forEach(maybeNotifyReviewEvent);
          }
          if (payload.type === "event" && payload.event) {
            state.events.push(payload.event);
            if (state.events.length > 600) state.events = state.events.slice(-600);
            maybeNotifyReviewEvent(payload.event);
            if (state.currentSession && (payload.event.run_id === state.currentSession.session_id || payload.event.session_id === state.currentSession.session_id)) {
              state.currentSessionEvents.push(payload.event);
              renderSessionDetail();
            }
            if (String(payload.event.type || "").startsWith("hitl_review_")) {
              loadReviews({ silent: true }).catch(() => {});
              if (state.currentReview && payload.event.review_id === state.currentReview.review_id) {
                openReview(state.currentReview.review_id).catch(() => {});
              }
            }
          }
          renderDashboardActivity();
          renderEventView();
          loadStatus({ silent: true }).catch(() => {});
          loadSessions({ silent: true }).catch(() => {});
        });
      }
      function startPollingFallback() {
        if (pollingFallbackTimer || location.protocol === "file:") return;
        const poll = () => Promise.allSettled([loadEvents({ silent: true }), loadStatus({ silent: true })]).then((results) => {
          if (results.every((result) => result.status === "rejected")) setLiveState("API error", false);
        });
        poll();
        pollingFallbackTimer = setInterval(poll, 3000);
      }
      function stopPollingFallback() {
        if (pollingFallbackTimer) clearInterval(pollingFallbackTimer);
        pollingFallbackTimer = null;
      }
      async function loadInitialData() {
        try {
          const data = await api("/api/bootstrap");
          state.status = normalizeStatus(data.status);
          state.events = Array.isArray(data.events) ? data.events : [];
          state.sessions = Array.isArray(data.sessions) ? data.sessions : [];
          state.reviews = Array.isArray(data.reviews) ? data.reviews : [];
          state.pendingReviews = state.reviews;
          state.configs = Array.isArray(data.configs) ? data.configs : [];
          state.secretConfigs = Array.isArray(data.secret_configs) ? data.secret_configs : [];
          const skills = data.skills && typeof data.skills === "object" ? data.skills : {};
          state.generalSkills = Array.isArray(skills.general) ? skills.general : [];
          state.specificSkills = Array.isArray(skills.specific) ? skills.specific : [];
          state.mergedSkills = Array.isArray(skills.merged) ? skills.merged : [];
          state.skillDiagnostics = skills.diagnostics || {};
          state.events.forEach(maybeNotifyReviewEvent);
          rememberReviewTrail(state.reviews);
          state.pendingReviews.forEach(showReviewNotification);
          renderStatusMetrics();
          renderDashboardActivity();
          renderEventView();
          renderSessions();
          renderReviews();
          renderSkillLists();
          renderSecretConfigList();
          renderBacktestConfigOptions();
          renderGraphInspector();
          setInlineError(els.dashboardError, "");
        } catch (err) {
          const results = await Promise.allSettled([loadStatus(), loadEvents(), loadSessions(), loadReviews(), loadSkills(), loadConfigs(), loadSecrets()]);
          const failed = results.find((result) => result.status === "rejected");
          showToast(`API error: ${(failed && failed.reason && failed.reason.message) || err.message || err}`);
          if (els.liveLabel.textContent === "Connecting") setLiveState("API error", false);
        }
      }

      const REFRESH_ICON = `<svg class="refresh-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>`;
      document.querySelectorAll('[id^="refresh-"]').forEach((btn) => {
        btn.classList.add("refresh-btn");
        if (!btn.querySelector(".refresh-ico")) btn.insertAdjacentHTML("afterbegin", REFRESH_ICON);
        btn.addEventListener("click", () => {
          btn.classList.add("is-busy");
          clearTimeout(btn._busyTimer);
          btn._busyTimer = setTimeout(() => btn.classList.remove("is-busy"), 1600);
        });
      });
      document.querySelectorAll("[data-view-button]").forEach((btn) => btn.addEventListener("click", () => setView(btn.dataset.viewButton)));
      document.querySelectorAll("[data-goto]").forEach((el) => el.addEventListener("click", () => setView(el.dataset.goto)));
      document.querySelectorAll("[data-metric-view]").forEach((card) => card.addEventListener("click", () => {
        if (card.dataset.sessionFilterTarget) setSessionFilter(card.dataset.sessionFilterTarget, { load: false });
        if (card.dataset.skillTabTarget) switchSkillTab(card.dataset.skillTabTarget);
        if (card.dataset.reviewFilterTarget) state.reviewFilter = card.dataset.reviewFilterTarget;
        setView(card.dataset.metricView);
      }));
      document.querySelectorAll("[data-session-filter]").forEach((btn) => btn.addEventListener("click", () => setSessionFilter(btn.dataset.sessionFilter)));
      byId("refresh-dashboard").addEventListener("click", () => Promise.allSettled([loadStatus(), loadEvents()]).then((results) => {
        const failed = results.find((result) => result.status === "rejected");
        if (failed) showToast(failed.reason.message || String(failed.reason));
      }));
      byId("refresh-sessions").addEventListener("click", () => loadSessions().catch((err) => showToast(err.message)));
      byId("refresh-reviews").addEventListener("click", () => loadReviews().catch((err) => showToast(err.message)));
      document.querySelectorAll("[data-review-filter]").forEach((btn) => btn.addEventListener("click", () => setReviewFilter(btn.dataset.reviewFilter)));
      byId("refresh-skills").addEventListener("click", () => loadSkills().catch((err) => showToast(err.message)));
      byId("refresh-configs").addEventListener("click", () => loadConfigs().catch((err) => showToast(err.message)));
      byId("refresh-secrets").addEventListener("click", () => loadSecrets().catch((err) => showToast(err.message)));
      byId("refresh-events").addEventListener("click", () => loadEvents().catch((err) => showToast(err.message)));
      byId("pause-session").addEventListener("click", () => pauseOrResume(true).catch((err) => showToast(err.message)));
      byId("resume-session").addEventListener("click", () => pauseOrResume(false).catch((err) => showToast(err.message)));
      byId("run-backtest").addEventListener("click", () => runBacktestForSession().catch((err) => showToast(err.message)));
      byId("close-backtest").addEventListener("click", () => byId("backtest-panel").classList.add("hidden"));
      byId("allow-review").addEventListener("click", () => decideReview("allow").catch((err) => showToast(err.message)));
      byId("block-review").addEventListener("click", () => decideReview("block").catch((err) => showToast(err.message)));
      byId("new-specific").addEventListener("click", newSpecific);
      byId("save-specific").addEventListener("click", () => saveSpecific().catch((err) => showToast(err.message)));
      byId("delete-specific").addEventListener("click", () => deleteSpecific().catch((err) => showToast(err.message)));
      byId("cancel-specific").addEventListener("click", cancelSpecific);
      byId("validate-specific").addEventListener("click", () => validateSpecificImport().catch((err) => showToast(err.message)));
      byId("import-specific").addEventListener("click", () => importSpecific().catch((err) => showToast(err.message)));
      byId("new-merged").addEventListener("click", () => { try { newMerged(); } catch (err) { showToast(err.message); } });
      byId("save-merged").addEventListener("click", () => saveMerged().catch((err) => showToast(err.message)));
      byId("delete-merged").addEventListener("click", () => deleteMerged().catch((err) => showToast(err.message)));
      byId("new-config").addEventListener("click", () => newConfig().catch((err) => showToast(err.message)));
      byId("duplicate-config").addEventListener("click", () => duplicateConfig().catch((err) => showToast(err.message)));
      byId("save-config").addEventListener("click", () => saveConfig().catch((err) => showToast(err.message)));
      byId("delete-config").addEventListener("click", () => deleteConfig().catch((err) => showToast(err.message)));
      byId("save-config-graph").addEventListener("click", () => saveGraph().catch((err) => showToast(err.message)));
      // Full-bleed stage: config switcher popover + floating panel collapse
      (function () {
        const switcher = byId("config-switcher");
        const pop = byId("config-pop");
        if (switcher && pop) {
          switcher.addEventListener("click", (event) => { event.stopPropagation(); pop.classList.toggle("hidden"); });
          document.addEventListener("click", (event) => {
            if (pop.classList.contains("hidden")) return;
            if (pop.contains(event.target) || switcher.contains(event.target)) return;
            pop.classList.add("hidden");
          });
        }
        document.querySelectorAll(".stage-collapse[data-collapse]").forEach((btn) => {
          btn.addEventListener("click", () => { const panel = btn.closest(".stage-panel"); if (panel) panel.classList.toggle("collapsed"); });
        });
      })();
      byId("new-secret-config").addEventListener("click", () => { try { newSecretConfig(); } catch (err) { showToast(err.message); } });
      byId("save-secret-config").addEventListener("click", () => saveSecretConfig().catch((err) => showToast(err.message)));
      byId("delete-secret-config").addEventListener("click", () => deleteSecretConfig().catch((err) => showToast(err.message)));
      byId("copy-secret-config").addEventListener("click", () => copySecretConfigToLocal().catch((err) => showToast(err.message)));
      byId("back-secrets").addEventListener("click", () => showSecretIndex());
      byId("save-secret").addEventListener("click", () => saveSecret().catch((err) => showToast(err.message)));
      byId("delete-secret").addEventListener("click", () => deleteSecret().catch((err) => showToast(err.message)));
      byId("new-secret-record").addEventListener("click", () => { renderNewSecretForm(); renderSecretConfig(); });
      byId("clear-secret-form").addEventListener("click", renderNewSecretForm);
      els.secretConfigEnabled.addEventListener("change", updateSecretConfigSettingsFromControls);
      els.secretDefaultDecision.addEventListener("change", updateSecretConfigSettingsFromControls);
      byId("tab-general").addEventListener("click", () => switchSkillTab("general"));
      byId("tab-specific").addEventListener("click", () => switchSkillTab("specific"));
      byId("tab-merged").addEventListener("click", () => switchSkillTab("merged"));
      switchSkillTab("general");
      byId("specific-targets-tab").addEventListener("click", () => switchSpecificTab("targets"));
      byId("specific-json-tab").addEventListener("click", () => switchSpecificTab("json"));
      byId("merged-components-tab").addEventListener("click", () => switchMergedTab("components"));
      byId("merged-json-tab").addEventListener("click", () => switchMergedTab("json"));
      byId("config-json-tab").addEventListener("click", () => switchConfigTab("json"));
      byId("config-graph-tab").addEventListener("click", () => switchConfigTab("graph"));
      byId("secret-settings-tab").addEventListener("click", () => switchSecretTab("settings"));
      byId("secret-json-tab").addEventListener("click", () => switchSecretTab("json"));
      els.configName.addEventListener("input", updateConfigActionState);
      byId("auto-layout-graph").addEventListener("click", autoArrangeGraph);
      els.graphSkillSearch.addEventListener("input", renderGraphSkillLibrary);
      els.graphSkillLibrary.addEventListener("click", (event) => {
        const button = event.target.closest("[data-add-graph-skill-type]");
        if (button) addGraphSkill(button.dataset.addGraphSkillType, button.dataset.addGraphSkillName);
        const secret = event.target.closest("[data-add-graph-secret-config-name]");
        if (secret) addGraphSecretConfig(secret.dataset.addGraphSecretConfigName);
      });
      // ── Pointer-based graph interactions (AciraCloud-style drag & drop) ──
      let railDrag = null;   // dragging a library item toward the canvas
      let nodeDrag = null;   // repositioning a node on the canvas
      let graphDragPill = null;
      function ensureDragPill() {
        if (!graphDragPill) {
          graphDragPill = document.createElement("div");
          graphDragPill.className = "graph-drag-pill hidden";
          document.body.appendChild(graphDragPill);
        }
        return graphDragPill;
      }
      function showDragPill(event, label, over) {
        const pill = ensureDragPill();
        pill.classList.remove("hidden");
        pill.classList.toggle("over", over);
        pill.innerHTML = over
          ? `<span class="dot"></span>Drop to link <b>${escapeHtml(label)}</b>`
          : `<span class="dot"></span>Drag onto the canvas`;
        pill.style.left = event.clientX + 14 + "px";
        pill.style.top = event.clientY + 16 + "px";
      }
      function hideDragPill() { if (graphDragPill) graphDragPill.classList.add("hidden"); }
      function pointerInGraph(event) {
        const r = els.graph.getBoundingClientRect();
        return event.clientX >= r.left && event.clientX <= r.right && event.clientY >= r.top && event.clientY <= r.bottom;
      }
      // Start dragging a resource out of the library
      els.graphSkillLibrary.addEventListener("pointerdown", (event) => {
        if (event.button !== 0) return;
        if (event.target.closest("[data-add-graph-skill-type], [data-add-graph-secret-config-name]")) return;
        const skill = event.target.closest("[data-library-skill-type]");
        const secret = event.target.closest("[data-library-secret-config-name]");
        let item = null;
        if (skill && skill.getAttribute("aria-disabled") !== "true") {
          item = { kind: "skill", type: skill.dataset.librarySkillType, name: skill.dataset.librarySkillName, label: (skill.querySelector(".item-title") || {}).textContent || skill.dataset.librarySkillName };
        } else if (secret && secret.getAttribute("aria-disabled") !== "true") {
          item = { kind: "secret", name: secret.dataset.librarySecretConfigName, label: secret.dataset.librarySecretConfigName };
        }
        if (!item) return;
        railDrag = Object.assign(item, { startX: event.clientX, startY: event.clientY, active: false });
      });
      // Start dragging / removing / selecting a node on the canvas
      els.graph.addEventListener("pointerdown", (event) => {
        if (event.button !== 0) return;
        const removeBtn = event.target.closest("[data-remove-graph-node]");
        if (removeBtn) { event.preventDefault(); event.stopPropagation(); removeGraphNode(removeBtn.dataset.removeGraphNode); return; }
        const nodeEl = event.target.closest("[data-graph-node]");
        if (!nodeEl) return;
        const id = nodeEl.dataset.graphNode;
        if (id === "config") { state.selectedGraphNodeId = "config"; renderGraph(); return; }
        const node = graphNodeById(id);
        if (!node) return;
        const rect = els.graph.getBoundingClientRect();
        const s = (state.graphScale || 1) * (state.graphUserScale || 1);
        const px = (event.clientX - rect.left + els.graph.scrollLeft - (state.graphOffX || 0) - (state.graphUserPanX || 0)) / s;
        const py = (event.clientY - rect.top + els.graph.scrollTop - (state.graphOffY || 0) - (state.graphUserPanY || 0)) / s;
        nodeDrag = { id, el: nodeEl, offsetX: px - Number(node.x), offsetY: py - Number(node.y), startX: event.clientX, startY: event.clientY, moved: false };
        nodeEl.classList.add("dragging");
        try { nodeEl.setPointerCapture(event.pointerId); } catch (e) {}
        event.preventDefault();
      });
      document.addEventListener("pointermove", (event) => {
        if (railDrag) {
          if (!railDrag.active && Math.hypot(event.clientX - railDrag.startX, event.clientY - railDrag.startY) < 5) return;
          railDrag.active = true;
          const over = pointerInGraph(event);
          showDragPill(event, railDrag.label, over);
          els.graph.classList.toggle("drag-over", over);
        } else if (nodeDrag) {
          const rect = els.graph.getBoundingClientRect();
          const s = (state.graphScale || 1) * (state.graphUserScale || 1);
          const px = (event.clientX - rect.left + els.graph.scrollLeft - (state.graphOffX || 0) - (state.graphUserPanX || 0)) / s;
          const py = (event.clientY - rect.top + els.graph.scrollTop - (state.graphOffY || 0) - (state.graphUserPanY || 0)) / s;
          if (!nodeDrag.moved && Math.hypot(event.clientX - nodeDrag.startX, event.clientY - nodeDrag.startY) > 3) nodeDrag.moved = true;
          const node = graphNodeById(nodeDrag.id);
          if (node) {
            // Clamp within the clear region (content coords) so nodes never hide under panels.
            const limX = Math.max(8, (state.graphMaxX || els.graph.clientWidth / s) - GRAPH_NODE_W - 8);
            const limY = Math.max(8, (state.graphMaxY || els.graph.clientHeight / s) - GRAPH_NODE_H - 8);
            node.x = Math.min(limX, Math.max(8, px - nodeDrag.offsetX));
            node.y = Math.min(limY, Math.max(8, py - nodeDrag.offsetY));
            node.pinned = true;
            nodeDrag.el.style.left = node.x + "px";
            nodeDrag.el.style.top = node.y + "px";
            redrawGraphWires();
          }
        }
      });
      function endGraphPointer(event) {
        if (railDrag) {
          if (railDrag.active && pointerInGraph(event)) {
            const position = graphDropPosition(event);
            if (railDrag.kind === "skill") addGraphSkill(railDrag.type, railDrag.name, position);
            else addGraphSecretConfig(railDrag.name, position);
          }
          hideDragPill();
          els.graph.classList.remove("drag-over");
          railDrag = null;
        }
        if (nodeDrag) {
          nodeDrag.el.classList.remove("dragging");
          if (nodeDrag.moved) {
            // The live drag already updated the node + wires in the DOM; don't full-render
            // (that would recompute scale-to-fit and make the node visually jump). Just persist.
            const node = graphNodeById(nodeDrag.id);
            if (node) node.pinned = true;
            persistGraphSoon();
          } else {
            state.selectedGraphNodeId = nodeDrag.id;
            renderGraph();
          }
          nodeDrag = null;
        }
      }
      document.addEventListener("pointerup", endGraphPointer);
      document.addEventListener("pointercancel", endGraphPointer);
      els.graph.addEventListener("dragstart", (event) => event.preventDefault());
      els.graphSkillLibrary.addEventListener("dragstart", (event) => event.preventDefault());
      // Re-fit the graph to the canvas whenever it resizes (window resize, breakpoint change),
      // so the whole graph stays visible without sideways scrolling. Never mid-drag.
      if (window.ResizeObserver && els.graph) {
        let graphResizeTimer = null;
        let lastGraphW = 0;
        const graphResizeObserver = new ResizeObserver((entries) => {
          if (!state.currentGraph || nodeDrag || railDrag) return;
          const width = Math.round(entries[0] && entries[0].contentRect ? entries[0].contentRect.width : els.graph.clientWidth);
          if (Math.abs(width - lastGraphW) < 24) return;
          lastGraphW = width;
          clearTimeout(graphResizeTimer);
          graphResizeTimer = setTimeout(() => {
            if (!state.currentGraph || nodeDrag || railDrag) return;
            applyGraphAutoLayout(state.currentGraph, { onlyUnpinned: true });
            renderGraphCanvas();
          }, 180);
        });
        graphResizeObserver.observe(els.graph);

        // Scroll-to-zoom
        els.graph.addEventListener('wheel', (e) => {
          if (!state.currentGraph) return;
          e.preventDefault();
          const factor = e.deltaY < 0 ? 1.08 : 0.93;
          state.graphUserScale = Math.max(0.15, Math.min(5, (state.graphUserScale || 1) * factor));
          applyGraphUserTransform();
        }, { passive: false });

        // Drag-to-pan (background only, not nodes)
        let _gpanning = false, _gpx = 0, _gpy = 0, _gox = 0, _goy = 0;
        els.graph.addEventListener('pointerdown', (e) => {
          if (e.button !== 0) return;
          if (e.target.closest('.node, .graph-empty, .graph-lane-label, [data-graph-node]')) return;
          _gpanning = true;
          _gpx = e.clientX; _gpy = e.clientY;
          _gox = state.graphUserPanX || 0; _goy = state.graphUserPanY || 0;
          els.graph.setPointerCapture(e.pointerId);
        });
        els.graph.addEventListener('pointermove', (e) => {
          if (!_gpanning) return;
          state.graphUserPanX = _gox + (e.clientX - _gpx);
          state.graphUserPanY = _goy + (e.clientY - _gpy);
          applyGraphUserTransform();
        });
        const _stopPan = () => { _gpanning = false; };
        els.graph.addEventListener('pointerup', _stopPan);
        els.graph.addEventListener('pointercancel', _stopPan);
      }
      els.graphInspector.addEventListener("click", (event) => {
        const remove = event.target.closest("[data-remove-graph-node]");
        if (remove) { removeGraphNode(remove.dataset.removeGraphNode); return; }
        const saveLink = event.target.closest("[data-save-secret-link]");
        if (saveLink) saveConfigSecretLink().catch((err) => showToast(err.message));
      });
      els.eventFilter.addEventListener("change", renderEventView);
      els.eventSearch.addEventListener("input", renderEventView);
      els.dashboardEvents.addEventListener("click", handleEventClick);
      els.eventList.addEventListener("click", handleEventClick);
      els.notificationStack.addEventListener("click", (event) => {
        const open = event.target.closest("[data-open-notification-review]");
        const dismiss = event.target.closest("[data-dismiss-notification-review]");
        if (open) {
          const reviewId = open.dataset.openNotificationReview;
          clearReviewNotification(reviewId);
          state.reviewFilter = "pending";
          setView("hitl");
          openReview(reviewId).catch((err) => showToast(err.message));
        }
        if (dismiss) clearReviewNotification(dismiss.dataset.dismissNotificationReview);
      });
      els.sessionList.addEventListener("click", (event) => { const button = event.target.closest("[data-session]"); if (button) openSession(button.dataset.session).catch((err) => showToast(err.message)); });
      els.sessionDetail.addEventListener("click", (event) => {
        const filter = event.target.closest("[data-session-feed-filter]");
        if (filter) {
          state.sessionFeedFilter = filter.dataset.sessionFeedFilter || "all";
          renderSessionDetail();
          return;
        }
        handleEventClick(event);
      });
      els.reviewList.addEventListener("click", (event) => { const button = event.target.closest("[data-review]"); if (button) openReview(button.dataset.review).catch((err) => showToast(err.message)); });
      els.generalList.addEventListener("click", (event) => {
        const button = event.target.closest("[data-general]"); if (!button) return;
        const skill = state.generalSkills.find((item) => item.name === button.dataset.general); if (!skill) return;
        els.generalDetail.innerHTML = `<div class="item-title">${escapeHtml(skill.display_name)}</div><div class="item-meta">${escapeHtml(skill.id)}</div><p>${escapeHtml((skill.risks || []).join(", "))}</p>${skill.valid ? badge("Read only") : badge(skill.error || "Invalid", "danger")}`;
      });
      els.specificList.addEventListener("click", (event) => { const button = event.target.closest("[data-specific]"); if (button) openSpecific(button.dataset.specific).catch((err) => showToast(err.message)); });
      els.specificGui.addEventListener("change", (event) => {
        const decision = event.target.closest("[data-target-decision]");
        const threshold = event.target.closest("[data-target-threshold]");
        if (decision) updateSpecificTarget(Number(decision.dataset.targetDecision), { decision: decision.value });
        if (threshold) updateSpecificTarget(Number(threshold.dataset.targetThreshold), { threshold: Number(threshold.value || 70) });
      });
      els.specificGui.addEventListener("input", (event) => {
        const reason = event.target.closest("[data-target-reason]");
        if (reason) updateSpecificTarget(Number(reason.dataset.targetReason), { reason: reason.value }, { render: false });
      });
      els.specificGui.addEventListener("click", (event) => {
        const button = event.target.closest("[data-delete-target]");
        if (button) deleteSpecificTarget(Number(button.dataset.deleteTarget));
      });
      els.mergedList.addEventListener("click", (event) => { const button = event.target.closest("[data-merged]"); if (button) openMerged(button.dataset.merged).catch((err) => showToast(err.message)); });
      els.mergedPicker.addEventListener("click", (event) => {
        const button = event.target.closest("[data-add-merged-component-type]");
        if (button) addMergedComponent(button.dataset.addMergedComponentType, button.dataset.addMergedComponentName);
      });
      els.mergedSkillSearch.addEventListener("input", renderMergedPicker);
      els.mergedComponents.addEventListener("click", (event) => {
        const button = event.target.closest("[data-remove-merged-component]");
        if (button) removeMergedComponent(Number(button.dataset.removeMergedComponent));
      });
      els.configList.addEventListener("click", (event) => { const button = event.target.closest("[data-config]"); if (button) openConfig(button.dataset.config).catch((err) => showToast(err.message)); });
      els.secretConfigList.addEventListener("click", (event) => { const button = event.target.closest("[data-secret-config]"); if (button) openSecretConfig(button.dataset.secretConfig).catch((err) => showToast(err.message)); });
      els.secretRecordList.addEventListener("click", (event) => { const button = event.target.closest("[data-secret-record]"); if (button) openSecretRecord(button.dataset.secretRecord); });
      window.addEventListener("resize", scaleArtifactBoxes);

      if (location.protocol === "file:") {
        showFileModeWarning();
      } else {
        setReviewActionState(null);
        connectStream();
        loadInitialData();
      }

