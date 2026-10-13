# Connect your existing browser-use agent

Acira's browser-use integration uses the same local policy files, shared dashboard,
approval binding and history as Playwright. Start with the browser and model your
agent already uses; the SDK does not require a separate demo or an Ollama download.
The [general setup guide](quickstart.md) covers installing into your existing
environment. Optional free local-model setup and test-drive instructions follow
the real-project integration below.

## Install outside the repository

Use Python 3.11–3.15 and activate the environment that already runs your compatible
browser-use agent. Install Acira alone from PyPI:

```sh
# Create a .venv only if your project does not already have an environment.
# python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows PowerShell instead:
# .\.venv\Scripts\Activate.ps1
python -m pip install acira
python -m pip check
```

If browser-use is missing, the optional dependency extra supplies the tested framework
and integration libraries. This is an alternative for new environments, not a required
step for an existing compatible agent:

```sh
python -m pip install "acira[browser-use]"
```

For source or wheel installation, follow
[alternative installation methods](setup.md#alternative-installation-methods). This release
is tested with browser-use 0.11.12/0.11.13; check your version with `acira doctor` after
initializing. Keep your existing Chromium executable/session options. The SDK
does not silently install browsers, download models or rewrite application code.
The extra pins tested integration libraries, supplies the framework's missing
`pydantic-settings` dependency, and selects fixed AnyIO/pypdf releases. Browser-use
0.13.11 currently pins affected versions of those two libraries. This release uses
the compatible upstream 0.11.12/0.11.13 distribution, without modifying its metadata or
ignoring advisories. Review the dependency changes if you already use another version.

## Integrate your existing browser-use agent

Inside its project and environment:

The `submissions` starter below gates all native forms and
Submit/Send/Post/Publish/Apply/Finalize controls. **GET/search forms are
blocked while this policy is enabled because verified GET approval is not
supported.** Other matched controls need a supported `guard.submit(...)`
request contract; missing evidence blocks. Omit `submissions` to keep only
the other four protections.
Read the [form submission guide](form-submissions.md) before enabling it for your workflow.

```sh
acira init --yes --adapter browser_use --policies payment,credentials,destructive-actions,downloads,submissions
acira doctor
acira policy validate
```

Keep your existing model, task and Chromium settings. Create and run the session
inside the protection context in your existing async entry point:

```python
from acira import protect
from browser_use import Agent, BrowserSession

async with protect() as guard:
    browser = BrowserSession(**your_existing_browser_options)
    try:
        agent = Agent(
            task=your_existing_task,
            llm=your_existing_llm,
            browser_session=browser,
        )
        await agent.run()
    finally:
        await browser.kill()
```

The `your_existing_*` names are your application's values. Acira adds no model
provider requirement or model download to this integration. Use the actual project
and agent snippet printed by initialization when launching from elsewhere.

Use your existing browser executable/session setup as appropriate. Acira protection
starts through this integration, not installation alone. For critical submissions,
register a business tool using `guard.submit(session, exact_node, destination=...,
values=actual_complete_payload, facts=required_facts)`. Its supported JSON/form request
contract binds the reviewed values and destination. `guard.fill_fields(session,
[(exact_node, text), ...])` declares a bounded text operation; it excludes submission.
The packaged `acira.browser_use_demo` is a runnable reference. The
[protection contracts](protection.md) state action, browser, frame and payload limits.

Keep sensitive values out of task prompts and model logging. Acira redacts its
artifacts; your application's provider calls and framework logs need their own
privacy choices. The model must never become the reviewer for its own request.

## Optional: set up a free local model

This section is for users who want a local provider. Existing configured providers
do not need this setup. Local inference uses your computer's memory and compute.

Install [Ollama from its official distribution](https://ollama.com/download).
Start its server with cloud features disabled. In macOS/Linux:

```sh
OLLAMA_NO_CLOUD=1 ollama serve
```

In Windows PowerShell, in the terminal running the server:

```powershell
$env:OLLAMA_NO_CLOUD="1"
ollama serve
```

An already-running Ollama app must be restarted with the intended configuration;
changing a variable in another terminal does not change that server. See
[Ollama's local-only configuration](https://docs.ollama.com/faq#how-do-i-disable-ollama-cloud-features).
In another terminal, download the model once:

```sh
ollama pull qwen3:4b-instruct
ollama list
```

This non-thinking model downloads approximately 2.5 GB. Allow additional memory
for context, Chromium and your other applications. Speed and model quality depend
on the computer; a small model is not a guarantee of correct plans on arbitrary sites.
Use a downloaded local model supported by ChatOllama; this demo refuses cloud model
references and non-loopback inference URLs.

### Optional local-model integration example

Configure the free local provider explicitly and construct the session and agent
inside the protection context:

```python
import asyncio
import os
os.environ["ANONYMIZED_TELEMETRY"] = "false"
os.environ["BROWSER_USE_CLOUD_SYNC"] = "false"
os.environ["BROWSER_USE_VERSION_CHECK"] = "false"
from acira import protect
from browser_use import Agent, BrowserSession, ChatOllama
from playwright.async_api import async_playwright

async def main():
    async with protect() as guard, async_playwright() as p:
        browser = BrowserSession(headless=False, use_cloud=False,
                                 executable_path=p.chromium.executable_path)
        try:
            agent = Agent(
                task="Open https://example.com and summarize it without changing data.",
                llm=ChatOllama(model="qwen3:4b-instruct", host="http://127.0.0.1:11434"),
                browser_session=browser,
                use_vision=False,
                use_judge=False,
                enable_planning=False,
            )
            await agent.run(max_steps=10)
        finally:
            await browser.kill()

asyncio.run(main())
```

## Optional: run the separate test drive

This example uses Playwright only to locate its installed Chromium. For this
optional example, install `[browser-use,playwright]` extras and run
`python -m playwright install chromium` if that browser build is missing.

```sh
acira demo --adapter browser-use
```

Keep its terminal open. It starts/reuses the same per-user local dashboard and
opens a separate browser-use test drive. Its checkout and research projects are
independent of the Playwright demo and your application. To choose a location:

```sh
acira demo --adapter browser-use --directory /absolute/separate/demo-folder
```

For a private Ollama server on another port:

```sh
acira demo --adapter browser-use --ollama-host http://127.0.0.1:11435 --model qwen3:4b-instruct
```

1. Select **Start payment review**. Visible Chromium opens the local fake checkout.
2. The local model receives browser-use's page state and chooses the provided
   `review_payment` operation. Status shows model requests/replies and planning.
3. Acira pauses before dispatch. In the shared dashboard, choose
   **Approvals → Review operation** for **Acira Browser-use Playground**.
4. Inspect the screenshot, highlight, exact **10.00 USD**, **test-account**,
   **Acira test shop** and `/payment` destination. The screenshot supplements the
   reviewed request facts; a highlighted Pay button alone does not bind a payment.
5. Select **Approve once**. Acira revalidates and permits one matching request.
   The test-drive counter increases once after the local server actually receives it.
6. The result and run completion update automatically. Normal runs end and Chromium
   closes after three seconds. Activity retains decision, dispatch, response and end.
7. Repeat with **Reject**: no payment is received. **Try blocked delete** leaves its
   counter unchanged under the bundled policy. **Cancel checkout run** invalidates
   its pending review and stops the agent without retrying that operation.

The fake-data demo deliberately offers one bounded business operation plus `done`.
It does not offer the model a tool to approve itself, open the dashboard or bypass
the submission contract. These restrictions belong to the demo; they do not remove
the normal SDK's supported browser-use operations. The model is real; CI's
deterministic model tests are a separate mechanism for repeatable adapter/UI checks.

Manual human clicks in Chromium are outside SDK enforcement. Approval permits an
attempt; it does not itself establish success. An ambiguous outcome remains unknown
and is not retried. An unattended request never silently approves.

## Optional test drive: multiple projects and live policies

Start payment and draft review together. The research agent fills a text area with
a fake note and requests a separately bound save. Resolving its review does not
resolve the checkout agent's review. Both projects use the same dashboard as other
Acira-enabled projects, including Playwright projects in other environments.

Select **Keep this run active after its result** before starting to test live policy
updates. Edit/import a policy for the intended project and agent in **Policies**.
**Saved** means written to its files; **Active** means acknowledged by that run.
Another project's revision remains separate. End the held run when finished; this
mode expires after ten minutes. Policy changes invalidate stale pending approvals.

For your own agent, opening `acira open` after protection has started picks it up
without a restart. Keep `ACIRA_HOME` unset normally so virtual environments share
one account-wide service. File-only enforcement and recording work without that
server. The optional dashboard provides management, not every allow/block decision.

## Diagnose and stop

For your real project, use its normal shutdown and inspect the SDK run with
`acira status`. Local-model and test-drive troubleshooting below applies only
when you chose those options.

```sh
acira status
acira doctor
acira sync
acira server status
```

- **No local model:** finish `ollama pull`, then check `ollama list` against the exact
  `--model` name. A partially downloaded model cannot be used.
- **No server:** start Ollama, check its port and pass `--ollama-host` if needed.
- **Slow first request:** model loading and prompt evaluation can take longer than
  later runs. Status distinguishes planning from a human-review wait.
- **Model error/incomplete plan:** inspect the displayed error. The demo never
  swaps in a scripted success or falls back to a paid provider. Try a compatible
  downloaded local model if needed; existing decisions are not retried automatically.
- **Missing Chromium:** run `python -m playwright install chromium` in the intended
  environment, or restore the explicitly configured browser-cache location.
- **Expired dashboard login:** run `acira open` for a fresh local sign-in.

Cancel a pending run or end an explicitly held run through the test drive. Ctrl-C
stops the demo and its browser sessions. `acira server stop` stops the shared
dashboard separately; stopping it does not stop local enforcement. Stop Ollama
separately if you no longer want the model service running.

Inside the SDK context, browser-use `BrowserSession.stop()` and `kill()` cancel
that session's pending guarded operations and invalidate their reviews before
saving browser state. A late approval cannot dispatch them. Shutdown does not
release another agent or browser session's review. Create a fresh BrowserSession
for further protected work after stopping one; Acira prevents cleanup from
automatically reconnecting a deliberately stopped session.

Unexpected loss of the operation's browser connection marks the run failed and
requires a fresh protection context. Close its owned browser and inspect the
recorded unknown outcome before attempting anything again. Chromium may release
an already paused request on abrupt CDP detach; Acira cannot claim zero effects
after transport loss. Other agents' independent runs retain their own reviews.

Policies, extension imports/pairing, assignments, revision rollback, secret
references, routine grants, Activity, Backtesting, exports and retention remain
available in the shared dashboard. This is a same-computer/same-user deployment,
not an operating-system sandbox or a claim of protection for unrelated connections.
