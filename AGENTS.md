# Acira Agent Handoff

These instructions apply to the entire repository. Read `README.md` before changing behavior.

## Mission

Acira is a security and governance layer for AI browser agents. It sits between an agent framework and the browser's Chrome DevTools Protocol (CDP), classifies proposed browser actions, and decides whether to allow, block, or pause for human-in-the-loop (HITL) review. The product combines broad declarative general skills with precise application-specific captured skills.

Security and trust are the product. Preserve fail-closed behavior, redaction, auditability, and human control. Do not solve integration problems by bypassing the guard or weakening a default.

## Repository Map

- `acira/`: public SDK. Application code should normally enter through `acira.protect(config=...)`.
- `acira/adapters/`: framework adapters and the shared adapter contract. Playwright and browser-use behavior belongs here, not in the framework-neutral core.
- `acira_cdp_proxy/`: CDP classification, enforcement, policy, target mapping, secrets, HITL, and audit pipeline.
- `acira_dashboard/`: local dashboard, review backend, runtime/session storage, config and skill management, and backtesting.
- `configs/`: runnable, production-safe examples for browser-use, Playwright, dashboard, and terminal review.
- `skills/general/`: reusable declarative protections for checkout, credentials, destructive actions, and downloads/exports.
- `skills/specific/`: customer/application-specific captured skills. Keep these declarative; do not place executable code in exported skills.
- `tools/acira-capture-extension/`: unpacked Chrome/Edge extension for capturing specific-skill target fingerprints.
- `demo/`: local pages used to exercise checkout enforcement and extension capture.
- `tests/`: unit and integration coverage, including audit, redaction, persistence, HITL, shutdown, and recovery.
- `docs/audit/`: audit-schema migration guidance.

The installed Strawberry browser integration that was prototyped on the original development machine is a separate machine-local deployment. Its `strawberry_service.py`, `strawberry_shim.cjs`, installed-browser patches, credentials, and runtime state are not part of this portable repository. Do not claim that Strawberry-specific integration is packaged here unless those pieces are deliberately productized and reviewed in a future change.

## Security Invariants

- Default to fail closed when a mutating action cannot be classified or its target cannot be resolved safely.
- Never forward a blocked action to the browser. A rejected HITL action must not be retried or translated into a lower-level bypass.
- Keep framework-specific behavior in adapters; keep `acira_cdp_proxy` framework neutral.
- Keep general and specific skills declarative and schema validated.
- Redact before persistence or emission. Never log raw credentials, tokens, card data, cookies, or unbounded page payloads.
- Preserve one auditable decision per guard decision and keep audit-schema changes backward compatible through the version dispatcher/migration validator.
- Bound persistence queues, shutdown waits, review waits, and recovery paths. Do not trade safety for an unbounded retry.
- Keep local dashboard/runtime files and secrets out of Git.

## Windows Setup

From the repository root in PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
.\.venv\Scripts\python.exe -m pip install pytest==9.1.1 pytest-asyncio==1.4.0
```

The example browser-use runner uses Vertex AI. Authenticate locally without committing credentials:

```powershell
gcloud auth application-default login
```

Pass the co-founder's own Google Cloud project with `--project`; do not assume the default example project is accessible.

## Run and Inspect

Run the guarded browser-use demo (without `--target-url`, it serves `demo/checkout_page.html` locally):

```powershell
.\.venv\Scripts\python.exe main.py --project YOUR_VERTEX_PROJECT
```

Run it against a safe page:

```powershell
.\.venv\Scripts\python.exe main.py --project YOUR_VERTEX_PROJECT --target-url https://example.com --task "Open {url}. Summarize the page without changing data."
```

Start the dashboard:

```powershell
.\.venv\Scripts\python.exe -m acira_dashboard --api-key change-me-dashboard-key
```

Serve the local extension fixtures:

```powershell
.\.venv\Scripts\python.exe -m http.server 4173 --directory demo
```

Then open `http://127.0.0.1:4173/element_lab.html` or `http://127.0.0.1:4173/checkout_page.html`. To use the capture extension, load `tools/acira-capture-extension` as an unpacked extension in Chrome or Edge.

The lower-level proxy CLI is also available:

```powershell
.\.venv\Scripts\python.exe -m acira_cdp_proxy --cdp-url http://127.0.0.1:9222 --config .\configs\acira.default.json
```

## Verification

Run before every handoff or push:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The GitHub Actions workflow runs the same test suite on Python 3.11 with the pinned core dependencies. If extension JavaScript changes, also run:

```powershell
node --check .\tools\acira-capture-extension\background.js
node --check .\tools\acira-capture-extension\content.js
```

Review the final Git diff and verify no `.env`, `*.secrets.local.json`, `.acira`, `.browser-use`, `local-runtime`, `.codex-*`, or per-machine settings are staged.

## Change Discipline

- Make the smallest coherent change and add or update tests for behavior changes.
- Do not mix UI redesign, policy changes, and adapter changes in one patch.
- Treat changes to allow/block/ask semantics, redaction, secret handling, audit persistence, and recovery as security-sensitive.
- Keep examples safe by default and label test credentials as test data.
- Update `README.md`, configs, schemas, and migration guidance when public behavior changes.
