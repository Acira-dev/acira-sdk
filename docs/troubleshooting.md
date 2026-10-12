# Setup problems and their fixes

Acira integrates into your existing asynchronous Chromium workflow. Install the
normal framework first, add `acira` to the same Python environment, run `acira init`,
and add its protection snippet. The dashboard and test drive are optional. See
[Start here](setup.md) for the complete setup.

## Check the intended environment

Run `acira doctor` inside your project. Interactive terminals show a readable
summary; redirected output and `acira doctor --json` provide structured checks,
versions and repair instructions. Failed required checks return exit code 1.
Read-only policy files and an absent optional dashboard are warnings: neither means
that local enforcement is disabled.

The browser check resolves Playwright's matching bundled Chromium executable
without launching a browser. It does not validate a custom browser channel or
executable, desktop display access, or successful guarded execution. Acira never
installs a browser automatically. Use the exact interpreter command printed by
doctor to install a missing build. After upgrading Playwright, its browser build may
need to be installed again. If your existing workflow deliberately uses `channel=`
or `executable_path=`, use `acira doctor --skip chromium`. The result marks that
check **skipped**, and your custom browser remains unvalidated by diagnostics.
This flag does not change enforcement or framework compatibility requirements.

Keep base Acira installation separate from your framework. Optional framework
extras are convenient for new environments and can change installed dependency
versions. `python -m pip check` diagnoses dependency conflicts; Acira does not silently
repair or downgrade your existing project.

## The run says policies loaded, but no browser is attached

`protect()` loads policy configuration. For Playwright, it initially reports that
it is waiting for attachment. Call `guard.patch_playwright(p)` **before** your
Chromium launch or connection. That arms future launches; it does not retroactively
protect a browser that was already created.

For an existing async Chromium page, call `guard.patch_page(page)` before its first
action. This attaches the page's whole browser context to one Acira run, including
its other pages. Another Acira run cannot own that same context.

A successfully attached context produces a separate message. A run ending without
any attached context prints a warning. This warning cannot undo earlier actions:
verify the integration order before running a sensitive workflow. Protection claims
apply to actions inside the supported, attached integration. Unrelated connections
and manual browser clicks remain outside it.

## A payment click blocks without presenting a review

The starter payment policy needs verified amount, currency, account and request
binding. A button label or screenshot cannot establish these facts. The diagnostic
names missing fields and points to `guard.submit(...)`.

Use an [operation contract](protection.md#operation-contracts) with the actual
complete destination, method, payload and verifiable facts. Account identity must
come from established evidence, not a guess based on the website's domain. Missing
evidence blocks the action; changing the reviewed values invalidates approval.

Application code can catch `from acira import ActionBlocked` for both the core and
legacy Playwright block exceptions. Inspect its reason and metadata, then explain
or repair the operation. Catching the exception does not authorize retrying or
bypassing a blocked action.

## A review appears to be waiting

Acira prints the available review channel and remaining deadline before gathering
screenshot evidence. Respond in the interactive terminal, configured review backend,
or `acira open` → Approvals. When no dashboard is connected, the notice says so.
Opening it later can connect the existing run without restarting the agent.

Runtime setup and review notices honor the configured `guard.terminal_log: false`
setting. Human approval prompts still appear when terminal review is enabled;
quiet logging never approves a request.

The unchanged maximum wait is five minutes, then block. A file-only unattended run
with no reviewer blocks. Neither a missing reviewer nor a disconnected dashboard
can silently approve an action. Press Ctrl-C to stop your application when you do
not intend to supply a reviewer.

## Initialization asks again or rejects a choice

Running `acira init` for an existing agent now reports **Already initialized** and
keeps its saved settings without prompting. Use `acira policy` or the dashboard to
edit them, or add an independent named agent with `acira init --agent NAME`.

The wizard validates framework, policy and connection answers before writing
project files and reasks invalid interactive choices. `browser-use` and
`browser_use` spellings are accepted. Cancelling a prompt exits cleanly without a
Python traceback.

## A capture fails to import

Export JSON from Acira Capture; the filename is normally
`acira-skill-<host>.json`. Project configuration and arbitrary JSON are not policies.
Import failures name the input file; malformed JSON includes its line and column.

Importing identical content is idempotent. If a new capture has the same policy ID
but different content, compare the existing and new files before using:

```sh
acira policy import /path/to/acira-skill-example.com.json --replace
```

This explicit flag replaces the matching policy. It is not needed for the first
import or an unchanged capture. General policies can be added by catalog name,
catalog filename stem or exact policy ID, for example `acira policy add payment`.

## Chrome cannot find the exported extension

`acira extension install --directory /your/chosen/folder` accepts a new or empty
folder. Existing verified Acira exports update automatically when their files still
match their installation receipt. Modified files need explicit `--replace`;
unrelated files and symbolic links are never deleted as part of an update.

In Chrome, open `chrome://extensions`, enable Developer mode, choose **Load
unpacked**, and select the printed folder containing `manifest.json`. On macOS,
Cmd+Shift+G in the folder picker can navigate to Acira's default hidden user-state
folder. Reload the extension card after updating its export. Managed browser policy
can disallow unpacked extensions; offline capture import remains an SDK capability,
but Acira cannot override your browser administrator.

## Configuration or local-service state needs repair

Invalid configuration blocks affected guarded mutations. Doctor reports the named
file rather than claiming the empty policy set is valid. Interrupted policy writes
require `acira doctor --recover`, which restores the previous complete snapshot;
plain doctor remains read-only.

Unsafe server-descriptor permissions and incompatible protocols appear as failed
checks with repair guidance. Do not delete arbitrary service state or kill a process
based only on its port. Fix the named ownership/permissions or stop the original
Acira service process before reopening. Local enforcement remains independent of
the dashboard.
