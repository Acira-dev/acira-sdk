# Acira

Add local policies, human review, and action history to your existing browser agent.

The maintained SDK repository is [Acira-dev/acira-sdk](https://github.com/Acira-dev/acira-sdk).
It is private during preparation for the open-source release. SDK code, the optional
local dashboard, capture extension, policies, tests and release tooling live together
here. See [repository provenance and publishing](docs/repository.md).

Acira enforces policies inside the SDK. Its optional browser dashboard manages several
projects and agents on the same computer and operating-system account. Project files
remain the policy authority; stopping the dashboard does not stop enforcement.

**Start with your own project.** Install Acira in its Python environment, run
`acira init`, add the SDK to your existing entry point, then run your agent normally.
Open `acira open` whenever you want the dashboard. A demo, model download and separate
sample project are not part of this setup.

Follow the [step-by-step existing-project setup](docs/quickstart.md), or the
[Playwright](docs/playwright-quickstart.md) and
[browser-use](docs/browser-use-quickstart.md) integration guides.

This checkout contains the SDK/shared-dashboard implementation. See
[implementation and release status](docs/implementation-status.md) for verified behavior
and remaining release gates. Do not infer full adapter coverage from package installation.

## Install and initialize

Python 3.11 or newer is required. Until publication access for the `acira` distribution
has been verified, install this checkout or its built wheel rather than an unrelated
package with the same name.

Activate the environment that runs your existing agent. If Playwright or browser-use
is already installed, install the base SDK wheel there:

```sh
python -m pip install "/absolute/path/to/acira-0.1.0-py3-none-any.whl"
```

If you also need Playwright, install the wheel with `[playwright]`. From this checkout,
the equivalent installation is:

```sh
python -m pip install ".[playwright]"
```

Alternatively build with `python -m build --wheel`, then install
`/absolute/path/to/acira-0.1.0-py3-none-any.whl[playwright]` in your existing project's
environment. The base wheel includes the CLI, policies and dashboard. The
`playwright`, `browser-use` and `vertex` dependencies are separate optional extras.
The Google Cloud stack is not required for an ordinary Playwright project.

Inside your existing project:

```sh
acira init
acira doctor
```

The wizard selects an integration, policy folders, starter protections and automatic
dashboard connection. It shows the integration snippet; it does not rewrite your
application or install browsers. If your environment needs Chromium, install it
explicitly with `python -m playwright install chromium`. Reuse your existing browser
setup when it is compatible with the supported integration. Framework versions tested
for this release are Playwright 1.60.0 and browser-use 0.13.11; `acira doctor` reports
your installed versions and configured capabilities.

For a reproducible setup:

```sh
acira init --yes --adapter playwright --policies payment,credentials,destructive-actions,downloads
```

## Protect an existing Playwright workflow

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

`run_existing_workflow(browser)` is your application's existing function. Keep its
actual URLs and supported operations, or place your current browser code inside
the protection context. Patch before your launch or CDP connection.

Playwright automation itself needs no model provider or API key. `headless=False`
keeps Chromium visible while your workflow runs; keep the browser open while you
inspect a result. Acira protects the supported SDK calls, not manual human clicks.

Acira finds the nearest `acira.json` above the working directory. If you launch from
elsewhere, use `protect(project="/absolute/project", agent="checkout")`.
`protect(config="acira.json")` also recognizes project configurations.
Existing `protect(config="legacy-config.json")` remains supported.

For browser-use, select `--adapter browser_use`, install the `browser-use` extra,
and construct/run your existing agent inside `async with protect() as guard:`.
Your existing model provider configuration stays in your application.

**Chromium and asynchronous integrations are the supported family.** Read the
[capability matrix and boundaries](docs/protection.md) before relying on an operation.
Unknown scripts, forced pointer actions and unbounded low-level sequences are blocked
in protected Playwright mode. Acira is not an operating-system sandbox.

## Files, dashboard, and several agents

Initialization creates:

```text
acira.json
policies/general/
policies/specific/
.acira/                 # private runtime/history; ignored by Git
```

```sh
acira policy add credentials
acira policy import captured-policy.json
acira policy validate
acira open
```

`acira open` starts or reuses one authenticated loopback server for your OS user and
opens its browser dashboard. No project IDs need to be pasted. Already-running,
opted-in SDKs discover it without restarting. Merely installing the package does not
register or protect application code.

Use `acira init --no-dashboard` or `protect(dashboard=False)` for file-only operation.
Terminal review is available with an interactive terminal. Without an available
reviewer, actions requiring approval block; they never silently pass.

Add a named agent with `acira init --agent checkout`, or through Settings.
Each agent gets separate policy folders by default. You may deliberately configure
shared folders; the editor displays affected agents before saving.

The dashboard includes Agents, Approvals, Policies, Activity, Backtesting and Settings.
Policy saves are validated, checked for edit conflicts and written atomically.
**Saved** means the intended files were written; **Active** means the selected run
acknowledged that revision. Invalid edits stop affected mutations. A policy change
invalidates pending reviews and applies at the next guarded action boundary.

## Human review and critical submissions

Reviews show operation details, a masked browser screenshot when available, and the
target highlight. The dashboard's Pill companion marks the proposed target in an
enlarged masked preview; the full-page capture and technical details remain available.
Companions can be animated, kept still (the default), or hidden in Settings; reduced
motion is respected. One approval covers one attempt. DOM targets, form values and
policy revision are checked again before dispatch. There is no implicit approval
reuse by policy ID.

For a critical submission whose actual values cannot be established through page
inspection, use an explicit supported request contract:

```python
await guard.submit(
    page.get_by_role("button", name="Submit"),
    destination="https://your-test-app.example/payments",
    method="POST",
    encoding="json",
    values={"amount": "10.00", "currency": "USD", "account": "test-account"},
    facts={"amount": "amount", "currency": "currency", "account": "account"},
)
```

This Playwright API reviews and permits one matching HTTP request within the run's
browser context, including related frames, workers and popups. Page-level routing
must be removed before using a critical contract; Acira owns routing afterward. A different
destination, method or payload is blocked. It does not prove business success or
infer the signed-in account from a domain. Use your application's actual complete
payload, including recipient and other policy-required facts. See
[operation contracts](docs/protection.md#operation-contracts) for constraints.

For browser-use, the equivalent APIs are `guard.submit(browser_session, node, ...)`
and `guard.fill_fields(browser_session, [(node, text), ...])`. Project element clicks
use one exact DOM activation, including resolved frame nodes, with no native fallback
retry. Coordinate actions keep their stricter pointer-resolution boundary.

`guard.fill_fields([(locator, text), ...])` can review one bounded form-fill operation.
It never approves submission. Routine grants are explicit, scoped and revocable,
expire within 15 minutes, and cannot cover critical actions.

## Extension and policies

Load `tools/acira-capture-extension` as an unpacked Chrome/Edge extension.
Offline capture supports copy/download JSON. For direct import, open dashboard
Settings → Pair capture extension, then enter the displayed address/code in the
extension's connection settings. The extension asks for optional loopback access
during pairing. Select the destination project and agent before saving.

Paired credentials permit capture import and destination listing, never approvals
or secret retrieval. Pairing can be revoked. Repeated identical imports are idempotent.

The four starter policies are bundled. Additional declarative catalogs are explicit:

```sh
acira policy add policy-name --catalog https://your-catalog.example/catalog.json
acira policy update
acira policy update --accept
```

Preview changes before accepting them. Runtime enforcement never downloads updated
policies automatically. Catalog import and update previews are also in the dashboard.

## History, privacy and diagnostics

History persists locally before critical dispatch. It distinguishes authorization,
dispatch, browser completion and unknown outcomes. A successful click does not prove
a successful payment. Backtesting uses recorded normalized facts and the same guard
evaluator; unavailable evidence returns an indeterminate result.

Defaults: 30 days of history, seven days of review images, 1 GiB for the runtime audit database
and 250 MiB of images per project. Settings can change these limits.
The audit budget includes indexes, runtime metadata and explicitly enabled legacy
persistent archives. Finalized archives and older history are pruned together;
active/unfinalized archives are preserved and can block further mutations when the
budget is exhausted. Policy snapshots remain configuration history.

Known sensitive controls and text are masked before review images are persisted.
If a reliable mask cannot be established, Acira omits the screenshot and explains why.
Capture uses browser-page APIs rather than desktop screen recording.
Secret values stay in the runtime; Settings edits references, never their values.

```sh
acira status
acira sync
acira doctor
acira doctor --recover
acira server status
acira server stop
```

`sync` is a diagnostic rescan/retention operation; normal policy updates are automatic.
The local service uses your existing file permissions. Read-only folders produce
errors rather than repeated permission prompts. No external telemetry is enabled.

## Optional: try the complete free Playwright journey

After installing the `playwright` extra and Chromium:

```sh
acira demo
```

This opens a local test drive and the shared dashboard, using visible Chromium,
fake local payment/draft data and no model, paid API or API key. Approve or reject
in the dashboard; the test drive updates automatically and the one-operation run
ends after its observed result. Approval alone is not reported as execution success.
Cancellation and blocked deletion produce no automated request. Two project folders
have independent policies and reviews in the same dashboard.

Normal demo browser windows close three seconds after the result. Select **Keep this
run active after its result** before starting to test live policy changes; that
explicit mode stays active for up to ten minutes. Real agents may continue to further
operations after a review; their runs end when their `protect()` context exits.

The demo creates separate projects in Acira's private user state, leaving your current
project untouched. `acira demo --directory /your/separate/demo-folder` chooses a location;
`--no-open` prints the address and `--port` reuses a test-drive port. Ctrl-C stops the
demo; `acira server stop` separately stops the optional dashboard.
See the [complete Playwright setup and testing guide](docs/playwright-quickstart.md).

### Try real public websites without accounts

```sh
acira demo --public-sites
```

Choose Python documentation, MDN or Playwright documentation. **Browse normally**
follows a real documentation link. **Try a review** pauses that link until you approve
or reject it in the shared dashboard, with a screenshot and target highlight.
**Try a blocked click** demonstrates a policy stopping the same harmless link.
The test drive verifies navigation, updates automatically, and ends the run.

Each run uses a fresh visible Chromium session. This mode restricts traffic to
read-only requests on the selected documentation host, disables downloads and
service workers, and rejects account/payment paths. It deliberately limits site
selection; it does not ask for credentials, submit forms or use a paid API.
The example policies demonstrate protection on harmless links and are editable
separately for each agent. This is scripted Playwright automation through the SDK.
See [public-website instructions and limitations](docs/public-websites.md).

## Optional: try a free local browser-use agent

Install the wheel with `[browser-use,playwright]` extras and install Chromium. Start
Ollama in local-only mode and download the non-thinking local model:

```sh
# In one terminal; use your platform's environment-variable syntax.
OLLAMA_NO_CLOUD=1 ollama serve
# In another terminal:
ollama pull qwen3:4b-instruct
acira demo --adapter browser-use
```

This journey uses a real browser-use `Agent` with `ChatOllama`, visible Chromium,
local fake-data pages and the same Acira dashboard. The model requests a predefined
business operation; Acira reviews its exact request, observes the result and ends
the run automatically. It never substitutes scripted success or a paid model when
local inference fails. The demo verifies downloaded weights and a loopback endpoint.
The existing SDK still supports normal browser-use actions within its documented
protection boundary. See the [browser-use setup and testing guide](docs/browser-use-quickstart.md)
for platform setup, integration, cancellation, model limitations and troubleshooting.

## Development and release verification

```sh
python -m pip install ".[dev,playwright,browser-use]" build
python -m playwright install chromium
python -m pytest -q
python -m build --wheel
python tools/verify_wheel.py dist/acira-0.1.0-py3-none-any.whl
```

The wheel check creates two clean environments and projects outside the checkout,
starts agents first, verifies one shared service, changes one project's policies and
restarts the service. Browser tests observe actual DOM/HTTP effects.

- [Protection coverage, contracts and limitations](docs/protection.md)
- [Project configuration and migration](docs/project-configuration.md)
- [Implementation status and full delivery commitments](docs/implementation-status.md)
- [Build and publication workflow](docs/release.md)
- [Five-project pilot protocol](docs/pilot.md)
- [Legacy dashboard, lower-level proxy and Vertex runner](docs/legacy-guide.md)
- [Existing audit schema migration](docs/audit/migration.md)

Legacy `python -m acira_dashboard` and `python -m acira_cdp_proxy` entry points remain
available for existing configurations. They are compatibility interfaces; new
multi-project installations use `acira open`.
