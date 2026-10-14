# Acira

Local policies, human review, and action history for your browser agent.

Acira adds a protection layer to an existing asynchronous Playwright or browser-use
application. Keep your browser workflow and model configuration; define which
supported actions to allow, review, or block through local policy files.

- **Policies:** bundled protections and specific rules for captured website targets.
- **Human review:** approve one supported action, with its target and operation details.
- **Local history:** inspect recorded decisions and observed browser outcomes.
- **Optional dashboard:** manage projects, agents, policies, and reviews on your computer.
- **Capture extension:** select a website target and export a policy without activating it.

The base SDK needs no model, API key, or cloud account. Playwright and browser-use
are separate dependencies; your existing agent may have its own model requirements.

[Setup guide](docs/setup.md) · [Playwright guide](docs/playwright-quickstart.md) ·
[browser-use guide](docs/browser-use-quickstart.md) · [Protection boundaries](docs/protection.md)

## Version scope

This guide describes **Acira 0.1.1**. `pip install acira` installs the latest
published PyPI version, which may be older than these instructions. Match the guide
to the version you install; [0.1.0 has versioned installation instructions](https://pypi.org/project/acira/0.1.0/).
Check your project's framework compatibility with `acira doctor`.

## Installation

Requires Python 3.11 or newer. Use your project's virtual environment and see
[compatibility](docs/compatibility.md) for integration and platform boundaries.

If Playwright is already installed, keep your compatible installation and skip the
first two commands. For a new Playwright project, install the official framework
and its Chromium build, then add Acira to the same environment:

```sh
python -m pip install playwright==1.63.0
python -m playwright install chromium
python -m pip install acira
python -m pip check
```

See [Playwright's installation guide](https://playwright.dev/python/docs/intro)
for platform prerequisites. Installing base Acira does not install a browser or model.
The optional `acira[playwright]` extra installs the Python framework alongside Acira;
the browser installation remains explicit.

From your existing project directory:

```sh
acira init
acira doctor
```

The initialization wizard creates `acira.json` and policy folders, lets you choose
starter protections, and prints the integration snippet. It does not rewrite your
application. `acira doctor` reports framework compatibility and configuration issues.

## Protect your existing workflow

Patch Playwright inside the protection context, before launching or connecting to
a browser. Keep your current workflow in the marked function:

```python
import asyncio

from acira import protect
from playwright.async_api import async_playwright


async def run_existing_workflow(browser):
    # Replace this example with your application's existing browser code.
    page = await browser.new_page()
    await page.goto("https://example.com")
    print(await page.title())


async def main():
    async with protect() as guard:
        async with async_playwright() as playwright:
            guard.patch_playwright(playwright)
            browser = await playwright.chromium.launch()
            try:
                await run_existing_workflow(browser)
            finally:
                await browser.close()


asyncio.run(main())
```

Acira discovers the nearest `acira.json` above the working directory. To select a
project explicitly, use `protect(project="/path/to/project", agent="checkout")`.

For browser-use, select that adapter during `acira init`, then construct and run
your existing compatible agent inside `async with protect() as guard:`. Keep its
model, task, and browser settings. Follow the [browser-use integration guide](docs/browser-use-quickstart.md)
before changing framework versions. The optional `acira[browser-use]` extra is
dependency setup for projects that need it, not a requirement for a compatible
existing installation.

## Policies and human review

Manage policies from your project directory:

```sh
acira policy add credentials
acira policy list
acira policy configure
acira policy validate
```

Bundled protections cover payments, credentials, destructive actions, downloads,
and submissions. General risks can be configured to Allow, Ask, or Block; captured
targets support Ask or Block. Read the [policy guide](docs/specific-policies.md)
for agent assignments and specific website rules.

Actions configured to Ask need an available reviewer. Without one, they block.
An approval covers one attempt, and relevant target, values, and policy revision
are checked again before dispatch. An observed click or HTTP response does not
prove that a business transaction succeeded.

The `submissions` policy has a stricter boundary: GET/search forms are blocked, and
other critical submissions need a supported `guard.submit(...)` request contract.
Read the [form submission guide](docs/form-submissions.md) before enabling it.

## Optional dashboard and capture

```sh
acira open
```

The dashboard starts or reuses an authenticated loopback service for your operating
system user. It brings together opted-in projects and agents, pending approvals,
policy editing, and history. Project policy files remain authoritative; stopping
the dashboard does not stop SDK enforcement.

Use `acira init --no-dashboard` or `protect(dashboard=False)` for file-only operation.
Interactive terminal review is also available.

To capture a website-specific policy:

```sh
acira extension install
```

Load the printed folder through Chrome or Edge's **Extensions → Developer mode →
Load unpacked**. Open the website, choose **Acira Capture**, select a target, and
export its JSON. Selecting a target does not click it.

```sh
acira policy import /path/to/capture.json
acira policy validate
```

Dashboard pairing for direct imports is optional. Exported JSON works without it.
The extension cannot capture browser-internal pages. See the [capture and policy guide](docs/specific-policies.md)
for setup, pairing, and updating an exported extension.

## Optional demo

With Playwright and Chromium installed:

```sh
acira demo
```

Try reviews and blocked actions against local pages with fake data. The demo opens
visible Chromium and the local dashboard, creates separate demo projects, and needs
no model or paid API. It is a test drive, not an integration step. Ctrl-C stops it;
`acira server stop` separately stops the dashboard.

`acira demo --public-sites` offers a limited read-only documentation-site example.
`acira demo --adapter browser-use` is a separate optional local-model example;
its model and Ollama setup are explained in the [browser-use guide](docs/browser-use-quickstart.md).

## Supported boundaries

The integrations support asynchronous Chromium with Playwright 1.60.0, 1.61.0,
1.62.0, or 1.63.0, and browser-use 0.11.12 or 0.11.13. Other framework versions are rejected at attachment;
Acira does not silently change an existing installation. Firefox, WebKit, and
synchronous Playwright are outside the supported integration.

Protection applies to documented operations through the attached adapter. Unrelated
HTTP clients, independently controlled browsers, private framework internals, and
manual human actions are outside that boundary. Acira is not an operating-system
sandbox and does not guarantee protection from every prompt injection or website.
Unsupported mutations and operations without required evidence are blocked in
protected mode. See the [capability and contract guide](docs/protection.md) before
relying on a particular workflow.

Policy and action history is stored locally. Review images mask known sensitive
controls when reliable masking is available; otherwise the image is omitted.
Review your retention settings and [privacy boundaries](docs/protection.md) before
using sensitive projects.

## Troubleshooting

- **Framework or dependency mismatch:** run `acira doctor` and `python -m pip check`.
  Use a compatible project environment and review dependency changes before installing extras.
- **Chromium is missing:** run `python -m playwright install chromium` in the same environment.
- **Project is not found:** run from your initialized project, or pass `project=` to `protect()`.
- **An action is blocked:** inspect its policy decision and required operation contract;
  approval does not enable an unsupported action.
- **Dashboard connection:** use `acira server status`. File-only enforcement remains available.

See [setup and troubleshooting](docs/setup.md) for detailed platform instructions.

## Contributing

Small fixes, reproducible bug reports, and documentation improvements are welcome.
Include the framework version and a minimal example, and remove credentials,
personal data, and private policy/history files from reports.

For a development checkout, install the adapters and test dependencies, matching
Chromium, and Node.js 22 or newer for extension tests:

```sh
python -m pip install ".[dev,playwright,browser-use,vertex]"
python -m playwright install chromium
python -m pytest -q
```

Node.js is a development test prerequisite; base SDK use does not require it.
Changes to protection behavior should include tests for the relevant browser effects
and failure paths. See the [source repository](https://github.com/Acira-dev/acira-sdk)
for the project files.

## License

The SDK is [MIT licensed](LICENSE). Bundled dashboard fonts use the SIL Open Font
License; their notices are included in
[GoogleSans-OFL.txt](acira_dashboard/static/GoogleSans-OFL.txt) and
[GeistMono-OFL.txt](acira_dashboard/static/GeistMono-OFL.txt).
