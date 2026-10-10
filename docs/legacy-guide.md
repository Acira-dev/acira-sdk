# Acira Production Browser Guard

This folder is the production handoff for Acira's browser-agent guard. It contains the guarded browser-use integration, the CDP guard core, the local dashboard, general declarative skills, and the Chrome/Edge capture extension.

Local demos, archived tests, fixture pages, prototype CLI code, runtime captures, and local secrets are intentionally not part of this handoff.

## What Is Included

- `main.py`: minimal browser-use runner wrapped with Acira.
- `acira/`: public SDK facade and browser-use adapter registry.
- `acira_cdp_proxy/`: CDP classification, enforcement, policy, redaction, secrets, and proxy core.
- `acira_dashboard/`: local dashboard server, API, runtime store, session review, backtest, config, skill, and secrets management.
- `configs/`: production-safe default configs.
- `skills/general/`: declarative general guard skills for checkout, credentials, destructive actions, and downloads.
- `skills/specific/`: empty by default; add customer/application-specific captured skills here.
- `tools/acira-capture-extension/`: unpacked Chrome/Edge extension for capturing specific skills.

## Setup

Run these commands from this `production` folder.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If `py -3.11` is not available, use another supported Python launcher:

```powershell
python -m venv .venv
```

## Vertex AI / Google Auth

The example runner uses browser-use's `ChatGoogle` integration with Vertex AI.

1. Install and authenticate the Google Cloud CLI.
2. Grant the selected project access to the model you plan to use.
3. Set application default credentials:

```powershell
gcloud auth application-default login
```

You will pass the project id with `--project`.

## Run The Minimal Guarded Agent

The default task is intentionally safe: it opens a page, inspects visible content, and avoids submitting forms or changing data.

```powershell
.\.venv\Scripts\python.exe main.py --project YOUR_VERTEX_PROJECT --target-url https://example.com
```

Use another task inline:

```powershell
.\.venv\Scripts\python.exe main.py --project YOUR_VERTEX_PROJECT --target-url https://example.com --task "Open {url}. Summarize the page without clicking buttons or changing data."
```

Or read a task from a text file:

```powershell
.\.venv\Scripts\python.exe main.py --project YOUR_VERTEX_PROJECT --target-url https://example.com --task-file .\task.txt
```

Useful options:

```powershell
.\.venv\Scripts\python.exe main.py --help
```

## Configs

Default terminal-review config:

```powershell
configs\acira.default.json
```

Dashboard-enabled config:

```powershell
configs\acira.dashboard.json
```

Both configs enable:

- browser-use adapter integration
- arbitrary agent JavaScript blocking
- general skill preflight
- checkout, credential, destructive action, and download/export guard skills
- no captured specific skills by default

To add a specific skill, put the JSON file under `skills\specific\` and reference it from a config:

```json
{
  "specific_skills": [
    "../skills/specific/customer-app.json"
  ]
}
```

## Dashboard

Start the local dashboard:

```powershell
.\.venv\Scripts\python.exe -m acira_dashboard --api-key change-me-dashboard-key
```

Open:

```text
http://127.0.0.1:8765/
```

Run the browser agent with the dashboard-enabled config:

```powershell
.\.venv\Scripts\python.exe main.py --project YOUR_VERTEX_PROJECT --config .\configs\acira.dashboard.json --target-url https://example.com
```

The dashboard supports live review, session state, config editing, skill management, local secrets configs, and backtesting recorded runtime events against configs.

## Capture Extension

The extension creates declarative specific-skill JSON for a real application.

1. Open `chrome://extensions` or `edge://extensions`.
2. Enable developer mode.
3. Load `tools\acira-capture-extension` as an unpacked extension.
4. Open the application page you want to protect.
5. Click the Acira Capture extension icon.
6. Select a button, link, input, or select.
7. Mark the target as `Ask` or `Block`.
8. Export the JSON and save it under `skills\specific\`.
9. Add the skill path to the config you want to run.

The exported skill is data only. It should contain URL scope, target fingerprints, decisions, reasons, and capture metadata, not executable code.

## Secrets

Never commit local secrets. Files ending in `.secrets.local.json` are ignored.

Use the template:

```powershell
configs\acira.secrets.example.json
```

Create a local copy when needed:

```powershell
Copy-Item configs\acira.secrets.example.json configs\acira.secrets.local.json
```

Then link it in a config:

```json
{
  "secrets_config_path": "acira.secrets.local.json"
}
```

The dashboard can also create and manage local secrets configs.

## CDP Proxy CLI

Preview a proxy for an existing browser CDP endpoint:

```powershell
.\.venv\Scripts\python.exe -m acira_cdp_proxy --cdp-url http://127.0.0.1:9222 --config .\configs\acira.default.json
```

Run a child agent through the proxy:

```powershell
.\.venv\Scripts\python.exe -m acira_cdp_proxy run --cdp-url http://127.0.0.1:9222 --config .\configs\acira.default.json -- YOUR_AGENT_COMMAND
```

## Troubleshooting

- If imports fail, run commands from `production` and use `.\.venv\Scripts\python.exe`.
- If the dashboard rejects mutations, make sure `--api-key` matches `dashboard.api_key` in the config.
- If a config fails to load, check that every skill path is relative to `configs\`.
- If browser-use stores local state, it goes to `.browser-use\`, which is ignored.
- If runtime/dashboard files appear, they should be under `local-runtime\`, which is ignored.
