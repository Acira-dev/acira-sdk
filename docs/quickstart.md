# Connect Acira to your existing project

For terminal policy selection and Chrome capture, see the
[general and specific policy guide](specific-policies.md).

The normal setup is **install → initialize → integrate → run your agent**.
The optional dashboard opens with `acira open`. You can complete this setup without
running a demo or creating a sample application.

## 1. Use your application's Python environment

Open a terminal in the project containing your agent. Activate the same environment
you use to run it. For an existing `.venv`:

```sh
# macOS/Linux
cd /absolute/path/to/your-project
source .venv/bin/activate
```

```powershell
# Windows PowerShell
cd C:\path\to\your-project
.\.venv\Scripts\Activate.ps1
```

Use Python 3.11 or newer. If you have no environment yet, create one with
`python -m venv .venv`, then activate it as above. Existing Conda, uv or other Python
environments also work; installing and running must use the same interpreter.

## 2. Install the SDK there

For the private preview, use the wheel supplied with the release build. It can remain
in Downloads or any other folder; it need not be copied into your project.

If your browser framework is already installed:

```sh
python -m pip install "/absolute/path/to/acira-0.1.0-py3-none-any.whl"
```

If you also need a framework, choose its extra instead:

```sh
# Playwright
python -m pip install "/absolute/path/to/acira-0.1.0-py3-none-any.whl[playwright]"
# Or browser-use
python -m pip install "/absolute/path/to/acira-0.1.0-py3-none-any.whl[browser-use]"
```

Replace the path with the actual downloaded wheel path. The base SDK includes the
CLI, dashboard and starter policies. Extras install the selected framework; they
do not install Chromium or download a model. This release's integration tests use
Playwright 1.60.0 and browser-use 0.11.12. Other versions are not established by
those tests; check the [protection coverage](protection.md) when evaluating them.

Reuse your existing Chromium installation or connection and your existing model.
For Playwright, install its Chromium build only if your workflow needs it:

```sh
python -m playwright install chromium
```

Playwright automation requires no model or paid API. Acira adds no model dependency
to an existing browser-use agent. An optional local Ollama setup is described in
the [browser-use guide](browser-use-quickstart.md).

## 3. Initialize your real project

In your project folder:

```sh
acira init
```

Select Playwright or browser-use, choose policy folders and starter protections,
and choose whether runs should automatically connect to the local dashboard.

For a Playwright setup without questions:

```sh
acira init --yes --adapter playwright --policies payment,credentials,destructive-actions,downloads
```

For browser-use, change the adapter flag to `--adapter browser_use`.
The starter policies ask about sensitive payment/credential actions and downloads,
and block destructive actions. Review their actual coverage before using them.

Acira creates:

```text
your-project/
  acira.json
  policies/general/
  policies/specific/
  .acira/                 # private runtime and history
  your existing files
```

Initialization registers this project and adds Acira runtime paths to `.gitignore`.
It preserves application code. Running it again preserves an existing agent's
configuration and prints instructions for that configured integration.

## 4. Add Acira to your existing entry point

Use the exact snippet printed by `acira init` for your project and agent.
Configuration alone does not protect code; the workflow must execute inside
`protect()`.

For Playwright, patch before your browser launch or CDP connection:

```python
import asyncio
from acira import protect
from playwright.async_api import async_playwright

async def main():
    async with protect() as guard:
        async with async_playwright() as p:
            guard.patch_playwright(p)
            browser = await p.chromium.launch(headless=False)
            try:
                await run_existing_workflow(browser)
            finally:
                await browser.close()

asyncio.run(main())
```

`run_existing_workflow(browser)` stands for your application's existing function.
Keep its actual URLs and supported browser operations. You can also keep your
existing launch options or `connect_over_cdp(...)`; perform the launch/connection
after patching. Use `headless=False` if you want to watch your agent's browser.

For browser-use, keep your provider and browser configuration and create/run the
session inside the protection context:

```python
from acira import protect
from browser_use import Agent, BrowserSession

# Inside your existing async entry point:
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

The `your_existing_*` names refer to values already supplied by your application.
Acira does not choose or pay for a provider, download a model, or substitute a demo
workflow. Keep secrets out of prompts and framework logs.

The supported family is Chromium with asynchronous integrations. Critical
submissions need the actual operation evidence and, where required, a supported
`guard.submit(...)` contract. Read [protection contracts](protection.md) before relying
on a sensitive operation; normal integration is not an operating-system sandbox.

## 5. Verify configuration, then run your agent normally

```sh
acira doctor
acira policy validate
```

Fix reported configuration or missing-integration issues. These checks inspect
configuration and dependencies; they do not execute or certify your workflow.

Start your application using its usual command: `python your_agent.py`, your project
runner, or your existing IDE launch configuration. Replace that illustrative filename
with your actual entry point. Acira starts protecting when execution enters
`protect()` and records actions locally.

Then inspect it with:

```sh
acira status
acira open
```

The dashboard has a **Connect a project** guide in Agents. It shows initialized
projects even before their first run. With automatic connection enabled, running
agents connect when the server becomes available, including if you open it later.
Use **Agents** for live runs, **Approvals** for reviews and **Activity** for recorded
decisions and outcomes. A request only appears when the policy requires review;
an approval grants one attempt, and the outcome is recorded separately.

The run ends when your protection context exits. Keep your actual application
running if you want to inspect a live run or apply policies while it is active.
The dashboard's lifetime is independent of your agent's lifetime.

## 6. Manage your real policies and other agents

Choose and configure protections directly from the terminal:

```sh
acira policy add
acira policy list
acira policy configure
```

To create a website-specific policy, run `acira extension install`, load the printed
folder through Chrome's **Extensions → Developer mode → Load unpacked**, then click
Acira Capture on your website. Select a target, choose **Ask first** or **Block target**
and download JSON. Capture selection does not activate the control. Import it:

```sh
acira policy import /absolute/path/to/captured-policy.json
acira policy configure POLICY_ID --decision ask
acira policy validate
```

The configure command offers target selection and Ask/Block choices; general policies
also support Allow. `acira policy disable POLICY_ID --agent NAME` changes only that
agent's assignment. `acira policy list --json` provides machine-readable output.
All these commands work without a dashboard. Terminal reviews are available in an
interactive terminal; unattended reviews block. Dashboard users can instead select
**Policies → Capture specific policy**, **Import JSON**, or **Configure** for target
controls. A changed page can require a separate mapping review before its first action.

Dashboard saves write to that project's allowed policy folders automatically.
**Saved** means the files were written; **Active** means the selected run loaded them.
Changes apply at the next guarded action boundary. No routine manual sync command
is required. Invalid policies stop affected mutations until repaired.

To add another agent configuration in the same project:

```sh
acira init --agent checkout --adapter playwright
acira doctor --agent checkout
acira policy validate --agent checkout
```

Use `protect(agent="checkout")` for that workflow. Agents have separate policy folders
by default. Repeat initialization inside another project folder to connect it to
the same dashboard. Virtual environments share that service when running as the
same OS user; normally leave `ACIRA_HOME` unset.

If your entry point starts from a different working directory, use the explicit
location printed by initialization:

```python
async with protect(project="/absolute/path/to/your-project", agent="checkout") as guard:
    # Your existing protected workflow.
    ...
```

For file-only use, initialize with `--no-dashboard` or use `protect(dashboard=False)`.
An interactive terminal can handle reviews. Without an available reviewer, review
requests block rather than silently approve. If a project opted out and you later
want to connect a run, use `protect(dashboard=True)`.

## Optional: use a separate test drive

You may use `acira demo` to learn the review controls or `acira demo --public-sites`
to try harmless documentation-site navigation. For the local-model browser-use
example, use `acira demo --adapter browser-use` after its optional model setup.
Each creates separate sample projects. None is required for the setup above.

Further details: [Playwright](playwright-quickstart.md),
[browser-use](browser-use-quickstart.md), [project configuration](project-configuration.md),
[extension capture](../README.md#extension-and-policies), and
[build/publication status](release.md).
