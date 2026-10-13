# General and specific policies, without a dashboard

Start with the [existing-project setup](quickstart.md): install Acira in your
existing agent's environment, run `acira init`, and add the integration snippet.
The dashboard is optional. These commands run in the initialized project folder;
use `--project /absolute/path/to/project` and `--agent NAME` to choose another
project or configuration explicitly.

## General protections

```sh
acira policy add
```

Choose a numbered starter protection: payments, credentials, destructive actions,
downloads/exports, or Submit/Send/Post protection. The `submissions` starter also
gates native GET/search forms, which block because verified GET approval is not
supported. Other matched controls require a supported request contract before
approving an attempt; see the [form submission guide](form-submissions.md).
To automate the choice, supply its name:

```sh
acira policy add payment
acira policy list
acira policy configure payment
```

The last command lets you choose a risk and its decision. General risks support
**ask**, **block**, and **allow**. Read the risk description before allowing it:
allowing a risk removes that review requirement. Configuration updates an existing
agent override when present; otherwise it changes the policy file's default and
lists every agent sharing that file.

## Capture an application-specific target

You do not need to clone Acira's source or open its dashboard to obtain the
extension. Export the extension included in your installed SDK:

```sh
acira extension install
```

The command prints its folder and these Chrome setup steps:

1. Open `chrome://extensions`.
2. Enable Developer mode, choose **Load unpacked**, and select the printed folder.
3. On the intended page, click **Acira Capture** in the extensions menu.
4. Start capture, click the intended control to select it, then choose **Ask first**
   or **Block target**. Add other targets if needed.
5. Choose **Download**. Import the downloaded JSON in your agent's project:

```sh
acira policy import /absolute/path/to/acira-capture.json
acira policy list
acira policy configure
acira policy validate
```

Some websites prohibit file downloads through their page security policy. If no
file appears after Download, choose **Copy JSON**, save the copied text as
`acira-capture.json`, and import that file with the same command. Do not disable
Chrome or website security settings. TechCrunch's complete copied-JSON journey
is recorded separately from its failed Download attempts in the validation report.

Close Acira Capture when finished before letting an agent use that same page.

While capturing, a transparent extension frame receives pointer input and selects
the control underneath it. The page's earlier click/pointer listeners do not
receive that selection input. Scrolling still works; Escape pauses selection.
Selection messages travel through an authenticated extension channel. Website
messages cannot select or export a target. If the selector cannot load or its
connection is lost, Acira pauses and keeps the recovery message visible. Close
and reopen capture to reconnect; an already captured draft can still be exported.
Acira does not fall back to clicking the page. Test capture on an inert page first.
Capture is not a sandbox for untrusted website scripts or an authorization to
transact on a real website.

The terminal distinguishes general protections from specific policies and lists
their names, IDs, JSON filenames, and enabled state. Configuration offers captured
targets by label, with **ask** or **block** decisions; specific targets cannot be
configured as an unconditional allow. No manual fingerprint editing is needed.

For a script, supply the policy and target IDs displayed by the interactive flow:

```sh
acira policy configure POLICY_ID --target TARGET_ID --decision ask
acira policy disable POLICY_ID --agent research
acira policy enable POLICY_ID --agent research
acira policy list --json
```

Repeated exports from the same capture draft retain the policy identity. Importing
the same unchanged capture again is idempotent. A changed capture with the same
identity is a deliberate update: use `acira policy import FILE --replace`. Clearing
the draft and starting a new capture creates a new identity.

The extension captures the current page's origin and path. Inspect that scope
before applying a policy to other pages; an exact page capture does not silently
become a whole-site rule. Advanced scope/fingerprint edits remain available in the
dashboard's JSON editor or the policy file, with schema validation.

A captured draft keeps its source-page scope even if a site later changes its
route without reloading. If the page changes between selecting and adding a
target, select it again. Download the current capture or Clear it before capturing
another page; Acira does not combine unrelated page targets under one scope.

After upgrading with `python -m pip install --upgrade acira`, run
`acira extension install` to update the exported extension, then reload its card
in Chrome's extensions page. An unchanged installation needs no reload. Acira
refuses to overwrite unrelated files; if you deliberately modified its exported
files, use `--replace` only after reviewing those changes.

## How general and specific policies combine

Enabled general protections remain active on pages covered by a captured policy.
A capture identifies its declared targets; it does not exempt other buttons on
that page. For example, capturing an Archive control must not let an unrelated
Delete control bypass the destructive-action protection.

A Block from either policy prevents the operation. An Ask from either policy
still requires its review and operation evidence, even when the other policy
allows the action or the capture does not cover that control. Approving a target
mapping identifies a control; it does not approve an operation or override a
policy Block.

## Run and review from the terminal

Run your existing integrated agent normally. The SDK loads these files without
the dashboard and checks valid changes at the next action boundary. You do not
need `acira sync` after every edit. Terminal reviews are interactive; unattended
runs never approve by default.

On a new or changed page, Acira may first request a **target mapping review** if a
captured fingerprint cannot be identified unambiguously. This is separate from
approval to execute an action. Rejecting an unsafe mapping stops that run; do not
retry or bypass it. Repair the capture or investigate the page in a new run.

An unchanged target can map directly when its page, DOM position, stable identity
attributes, control kind, label facts, and recorded state agree. A score tie with
an unrelated or ineligible control no longer creates an unnecessary mapping
review. Link syntax or other recorded identity differences can still require mapping
review even when the destination looks familiar. Review the actual proposed
control and destination; a mapping approval only identifies the control.
This establishes the target only: an Ask policy still requires approval,
and a Block policy still prevents the action. Changed or uncertain targets retain
the normal resolver and mapping-review rules.

Ask policies then request an operation review. Rejecting it produces no guarded
browser side effect. Approval authorizes the reviewed attempt, subject to the
SDK's evidence and policy-revision checks. See [protection boundaries](protection.md)
for supported integrations and limitations.

## Optional dashboard and direct saving

`acira open` opens the shared local dashboard when you want it. Under **Policies**,
choose **Capture specific policy** for setup instructions or **Import policy**
for an offline capture. **Configure** exposes each specific target's Ask/Block
choice, website scope, and affected-agent list. Filenames stay compact; the full
location is available on hover or through the location control.

For direct extension saves, pair once in **Settings**, then choose the project
and agent in the extension's connection settings. The destination is shown before
saving. Repeated unchanged saves do not create extra policies. Offline JSON export
remains available if the service is stopped or pairing is unavailable.

Enable/disable applies to the selected agent's assignment. Editing a shared file
affects all agents listed as sharing it; create separate agent policy folders when
they should evolve independently. Saving persists the file; **Active** requires
the running agent's acknowledgement. Closing the dashboard leaves local
enforcement running.

## Repeatable validation

The ordinary offline browser tests cover extension pairing/export, terminal
imports and configuration, dashboard editing, and observed browser effects:

```sh
python -m pytest -q tests/test_terminal_policies.py tests/test_extension_browser.py tests/test_specific_policy_journey.py tests/test_specific_controls.py tests/test_specific_mapping_boundary.py tests/test_capture_mapping_regressions.py tests/test_specific_policy_sweep.py tests/test_specific_sweep_selection.py tests/test_capture_input_order.py tests/test_captured_general_boundary.py
```

`tools/specific_policy_sweep.py` is an opt-in, installed-wheel campaign for
signed-out public content links. It loads the real extension, imports a capture,
then verifies block, rejected review, and approved review against the same target.
It uses a separate read-only network fence and never submits forms, signs into
accounts, purchases, uploads, or downloads website content. Automated extension
injection pregrants only the selected website origins; native Chrome's toolbar and
permission UX is a separate check. Unavailable sites and uncertain mappings are
reported as limitations rather than protection passes.

Each capture makes one selection attempt. If its exact fingerprint does not match
within the original deadline, the campaign records
`capture_fingerprint_match_timeout` and bounded readiness, geometry and match
observations. It does not click again or export a policy from that failed attempt.
A later diagnostic match does not turn the original timeout into a pass. This
result alone does not establish whether a control moved or Chrome lost input.

```sh
/absolute/path/to/test-env/bin/python /absolute/path/to/acira-sdk/tools/specific_policy_sweep.py --output ./specific-check --minutes 60 --limit 300
```

Use a new output folder for each campaign and keep its private journals and images
local. Public links cannot establish authenticated, payment, or every-site safety.

See the [recorded validation campaign](validation/2026-10-12-specific-policies/README.md)
for verified workflows, capture repairs, unavailable sites, and remaining manual
and platform checks.
