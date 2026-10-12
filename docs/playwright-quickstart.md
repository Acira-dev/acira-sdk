# Connect your existing Playwright workflow

Start with your actual project, browser configuration and entry point. The
[general setup guide](quickstart.md) covers environment activation and installation.
The bundled test drive is optional and appears after the integration steps below.

## Install Playwright separately, then add Acira

Activate your existing project's Python 3.11/3.12 environment. If Playwright is
already installed, keep that compatible installation and skip its setup below.
For a new Playwright project, install the official package first:

```sh
python -m pip install playwright==1.60.0
# Only if this compatible Chromium build is not already installed:
python -m playwright install chromium
```

Keep your actual Playwright workflow and browser settings. Add Acira to the same
environment using its published package:

```sh
python -m pip install acira
python -m pip check
```

Playwright 1.60.0 is the version tested for this release. Acira supplies the protection
adapter for your application's Playwright calls; the Playwright package remains
separate. Acira does not silently install browsers, download a model, or replace your
entry point. Playwright automation needs no model, paid API or cloud account.

The optional `acira[playwright]` shortcut installs both official packages together.
It is not required. For source or wheel installation, see
[alternative installation methods](setup.md#alternative-installation-methods).

## Use Acira in your existing project

Inside that project, with your intended environment active:

```sh
acira init --yes --adapter playwright --policies payment,credentials,destructive-actions,downloads
acira doctor
acira policy validate
```

Wrap your actual asynchronous workflow:

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

`run_existing_workflow(browser)` denotes your application's existing function.
Keep its actual URLs, supported operations and desired browser launch/connect
options. Patch before launching or connecting to the browser.

Protection begins through this integration, not package installation alone. Relative
policy paths are anchored to `acira.json`. When launching elsewhere, pass
`protect(project="/absolute/project", agent="name")`.

For critical submissions use `guard.submit(...)` with the actual complete request
payload, destination and required verifiable facts. The demo is a runnable example;
[protection contracts](protection.md) describe supported actions and limits. Unknown
scripts, forced pointer actions and unsupported browser/API modes fail closed.

Run your agent first, then `acira open`: it connects automatically without restart.
Normally keep ACIRA_HOME unset so projects and virtual environments share one user
service. The dashboard is optional for enforcement. For file-only operation use
`protect(dashboard=False)` and an interactive terminal/external reviewer; unattended
review requests never default to approval.

## Optional: run the bundled journey

```sh
acira demo
```

Keep its terminal open. The command starts/reuses the shared local dashboard and
opens a test-drive page. It creates two independent demo projects in your private
Acira state directory. It works outside the source checkout. To choose a separate
folder explicitly, use `acira demo --directory /absolute/demo-folder`.

1. Start a payment review. Visible Chromium opens a local checkout using fake data.
2. Chromium waits before clicking Pay. In Acira, choose **Approvals → Review operation**.
3. Inspect the screenshot, target highlight, **10.00 USD**, **test-account**,
   **Acira test shop** and the local `/payment` destination.
4. Choose **Approve once**. Acira rechecks the exact operation before dispatch.
5. The test-drive status updates automatically and the payment counter increases once.
   Chromium displays the acknowledgement, then closes after three seconds. The SDK
   run reports **ended**, with its decision and execution events retained in Activity.
6. Repeat with **Reject**: the counter stays unchanged and the run ends automatically.
7. **Try blocked delete**: its counter stays zero under the bundled destructive policy.
8. Start another review and **Cancel checkout run**: its review becomes invalidated,
   no cancelled operation is retried, and the SDK run ends.

Decisions, execution and run completion are separate states. Approval is permission
for one attempt, not proof of a successful payment. Unknown results remain unknown.
Waiting without a decision blocks the action after five minutes.
Manual human clicks on Chromium controls are outside the SDK automation boundary;
use the test-drive and Acira review buttons for these checks.

## Optional test drive: two agents and live policy changes

Start payment and draft reviews together. The research agent types a fake note in
another Chromium window. Approve the research draft: its counter increments and
its run ends while checkout remains awaiting its independent decision. Reject
checkout: no payment request is added.

For live policy activation, select **Keep this run active after its result** before
starting. After resolving it, choose Policies in the dashboard. Select the intended
project and agent, then edit/import a policy and save. **Saved** means persisted;
**Active** means acknowledged by that run, normally within five seconds. The other
project's revision remains unchanged. End the held run when done; this explicit
mode is limited to ten minutes.

Policy changes invalidate existing reviews and cause reevaluation. Invalid external
JSON blocks affected mutations until repaired. Revision history and rollback are
available; rollback creates a new revision. Shared policy edits display their scope.

## Explore the remaining workflow

- **Policies:** bundled catalog, JSON imports, per-agent assignments, custom/shared
  folders, explicit update previews, validation, conflicts, revisions and rollback.
- **Approvals:** operation evidence, masked images, bounded form operations and
  explicit temporary routine grants. Critical operations require fresh review.
- **Activity:** searchable history, distinct authorization/dispatch/outcome events,
  filtered export and retention.
- **Backtesting:** recorded-action replay and revision comparisons using the live
  evaluator. Missing facts are reported as insufficient evidence.
- **Settings:** project locations, reference-only secrets management, extension
  pairing/revocation and diagnostics. The extension can also export JSON offline.

## Stop and repair

Stop your application using its normal shutdown. Its Acira run ends when the
`protect()` context exits. The dashboard service continues until `acira server stop`.
In the optional test drive, resolved runs end themselves; cancel or end held runs
through that test drive, or use Ctrl-C to stop the demo and its browsers.

```sh
acira status
acira doctor
acira sync
acira server status
```

If Chromium is missing, install it with `python -m playwright install chromium`.
If a dashboard sign-in expires, use `acira open` instead of copying service keys.
Read-only folders and incompatible SDK/server versions receive repair guidance.
Do not retry an ambiguous critical operation automatically.

If Acira reports that a selected target detached before authorization, or could not
be inspected safely, it has dispatched no browser action. Dynamic pages can replace
an element during inspection. Reevaluate the page and select a fresh target for a
new operation; any required review happens again. Do not force the click, reuse an
old approval, or turn this diagnostic into an automatic retry loop.

This is a local, same-user deployment. Other browser connections, independent HTTP
clients and manual human actions are outside Acira's SDK boundary. The SDK is not
an operating-system sandbox. No public publication or external customer-pilot
completion is implied by this local test drive.
