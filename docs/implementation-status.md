# SDK and shared-dashboard implementation status

Updated 2026-10-12. The entire approved feature set remains committed. This is an
implementation ledger, not a claim that every release gate or the customer pilot is
complete. Local SDK/extension release artifacts are built. Publication and platform results
are recorded separately below; a local build is not proof of PyPI publication.

## Implemented in this checkout

| Area | Implemented behavior | Local evidence |
| --- | --- | --- |
| Setup and packaging | Interactive/noninteractive init, optional framework/provider extras, packaged assets/policies, explicit project resolution, legacy aliases | Clean wheel in two independent environments outside checkout |
| File-only SDK | Project policies, named agent profiles, external folders, local enforcement and history, terminal/external review, unattended denial | Project/runtime tests and clean-wheel agents with server absent |
| Shared service | Per-user registry, authenticated loopback discovery, protocol handshake, cross-process start lock, late connect, heartbeats, one server across environments | Shared-service tests and clean-wheel restart journey |
| Policies | General/captured imports, catalog pinning/update previews, per-agent assignments, conflicts, atomic saves, revision history, rollback/recovery, pending-operation reevaluation, Saved/Active | Policy/configuration tests; isolated project revisions; aggregate snapshots above 1 MiB replayed and restored |
| Playwright | Common Page/Locator/Frame/ElementHandle, popup/persistent/CDP boundaries, keyboard/coordinate/file-chooser coverage, unsupported paths rejected | Real browser tests inspect DOM, FileList and HTTP effects |
| browser-use | Per-run event/CDP enforcement, exact target revalidation, event-argument binding, isolated review, existing JIT secret integration, exact frame-node activation, bounded fills, request contracts and related-target interception | Real reviewed/rejected type, element/frame activation, dropdown, upload, tabs, grouped fields and critical requests; worker/popup escapes and changed values blocked |
| Approvals | One-attempt binding, five-minute timeout, first-response-wins journal, per-run pause, explicit routine grants, bounded Playwright/browser-use form fill and single HTTP submission contracts | Changed values/files blocked; HTTP mismatch/retry blocked; scoped grants and atomic responses tested |
| Images | Existing screenshot/highlight feature retained, masks before persistence, omission on unreliable capture, visual-viewport/frame coordinates | Pixel privacy checks; scroll/frame/zoom/device-scale tests; UI screenshots visually inspected |
| Extension | Offline JSON, pairing/reconnection proof, explicit destination, narrow capability, revocation, direct import, sanitized capture URLs/handler literals | Packaged extension captures/imports only into the selected project without fixture secrets; historical native consent/pairing checks passed, while the current 0.1.8 native toolbar/consent recheck remains unexecuted |
| Secrets | Metadata-only reference editor, private runtime resolution, existing providers retained, bounded shared-process redaction | .env values absent from API metadata; same-name references stay masked across integrations and long runs; numeric codes/cards redacted before journal, transport and audit persistence |
| Activity | Durable redacted events, distinct authorization/dispatch/outcome states, resumable IDs, filtered pagination, streaming retained-history export, age/physical-database/image limits and combined legacy archive quota | Runtime tests, finalized/active archive quota checks, service restart without duplicate IDs |
| Replay | Same configured evaluator with frozen normalized facts, revision comparisons, policy decision distinct from human response, explicit indeterminate results | Replay parity and missing-fact tests; separate policy/authorization test |
| Dashboard | Agents, Approvals, Policies, Activity, Backtesting, Settings, interactive assignment graph/table, policy copying/shared folders, raw JSON, references, grants, controls, recovery messages | Live review journey and rendered highlight pixels; critical facts visible directly; same-agent runs identified and approvals isolated; keyboard setup/edit/assignment/rollback/recovery; safe Enter behavior |
| CI/docs | Core, Chromium and wheel jobs for macOS/Windows/Linux; migration, protection contracts, schemas, pilot protocol, wheel/source/extension artifacts, checksums and publication workflow | Historical six-combination installed-wheel and all-platform core/clean-wheel jobs passed; current formal Windows/exact-source gates remain pending; sandbox-enabled Linux startup verified |

The previous installed wheel passed **408 tests on each of Python 3.11 and 3.12** on macOS,
with Playwright 1.60.0/Chromium and browser-use 0.12.9 installed. Both full runs import
the installed packages from outside the checkout and reject source-package imports.
Verification also includes JavaScript syntax checks, Python compilation, staged
whitespace checks, wheel/source metadata validation, package-content inspection and
the two-environment clean-wheel journey. The
[historical source-repository GitHub run](https://github.com/FouadDim/Acira-Browser-Security/actions/runs/37967700112)
passed its entire 405-test installed-wheel suite on all six combinations of
macOS/Windows/Linux and Python 3.11/3.12, plus all core and clean-wheel jobs.
Linux's browser-use startup issue was repaired using an exact-executable AppArmor
permission; the Chromium sandbox stays enabled and its startup is explicitly tested.
The subsequent additions cover long-run/numeric secret masking and explicit review context.
The latest review UX also refreshes open review state, disables expired or externally
resolved decisions, and preserves queue focus only on the same request. Five new
real-browser regressions cover approved, rejected, expired and invalidated reviews,
including expiration between the fresh UI read and decision submission.
The final source suite passed **413 tests on macOS / Python 3.12**. The matching
installed-wheel/platform CI run remains the release gate for this revision.
Use [the current repository checks](https://github.com/Acira-dev/acira-sdk/actions)
for the matching final-source matrix rather than treating an earlier run as its result.

## Complete free Playwright journey

The wheel now includes `acira demo`: a visible Chromium test drive with two independent
projects, local policies, dashboard approvals, exact fake payment/draft requests,
blocked deletion and cancellation. It uses no model or paid API. Normal one-operation
runs end automatically after their actual result; optional bounded policy-testing
mode keeps a run active explicitly. The test drive reads durable review/run state,
and the Agents view refreshes even while a run control has keyboard focus. Focus is
restored only to the same project/run/action; a changed control cannot inherit Enter.

The matching source suite passed **420 tests** on macOS/Python 3.12. **18 real-browser
journey/dashboard tests** also passed against the installed wheel outside the source
checkout. Two clean base-wheel environments passed optional-extra setup guidance,
late connection, one shared service, separate revisions, restart independence and
stable event IDs. The visible installed-wheel checkout was approved through the real
dashboard: one matching request arrived, the run ended automatically, and Activity
retained dispatch/response/completion/end events. Public publication remains deferred
for user testing; final-source platform CI is recorded on the PR.

See [the Playwright setup and testing guide](playwright-quickstart.md).

## Free local browser-use journey

`acira demo --adapter browser-use` adds a real browser-use Agent using ChatOllama
and downloaded local weights. It supplies bounded fake payment/draft/deletion tools,
visible Chromium, independent project policies, the existing shared dashboard,
exact request review, observed counters and automatic run completion. It disables
browser-use telemetry/cloud sync and refuses remote inference URLs, cloud model
references and missing local weights. Local model errors remain visible; there is
no scripted-success or paid-provider fallback. The normal SDK's supported actions
remain available; the bounded tool list is specific to this safe demonstration.

The full source suite passed **432 tests** on macOS/Python 3.12 with both browser
integrations installed. **13 focused real-browser tests** passed for the Agent/UI
journey and form evidence. CI's Agent tests use a deterministic model to verify the
SDK/browser/dashboard contract; they do not establish real-model planning quality.
Two clean base-wheel environments passed installed assets, optional-extra guidance,
file-only operation, late connection, separate revisions and service restart.
The complete model-driven local acceptance check and final-source platform checks
are separate verification steps; use the matching PR checks for platform results.

Form evidence now explicitly records `checked: null` for non-checkable controls.
Text areas and selects no longer fail evidence construction; actual checked states
and changes to all three control types remain bound and invalidate stale approvals.
Both adapters' bounded text operations have real-browser regression coverage.

See [the browser-use setup and testing guide](browser-use-quickstart.md).

## Extended submission hardening

Adversarial HTTP fixtures reproduced unreviewed redirect forwarding in Playwright
and redirects arriving after browser-use's operation timeout. The repair disables
Playwright redirects/retries, retains browser-use request identities after timeout,
blocks redirected destinations and isolates old request failures from newer reviews.
The original reviewed endpoint may have received its one permitted attempt; a
blocked redirect is never reported as proof that nothing happened there.
Thirty-five focused redirect/contract tests and a separate newer-operation isolation
test passed before the additional full-suite run.

The first browser-use platform run exposed a background-polling race in five browser
jobs. Both previous installed adapters reproduced it deterministically: an unrelated
blocked read prematurely resolved the critical operation's outcome. The repair keeps
that read blocked and recorded, while awaiting the reviewed mutation's own matching
request/response. Model unavailability and invalid output also have tests proving no
submission, no fabricated success and no spurious human review. Final-source suite,
installed-wheel and platform results must be checked for this additional revision;
the earlier 432-test local result is not attributed to these subsequent changes.

Actual local Ollama inference exercised the installed browser-use Agent with two
independent projects. Each approved payment/draft reached the local HTTP fixture
exactly once and both runs ended automatically; resolving research left checkout
pending. Rejection and a destructive-policy block caused no additional requests.
The headed check exposed a target below the shorter viewport: exact element
operations now scroll that target into view before recording review evidence.
Its regression checks visibility at review time and the actual request effect.
The local demo also disables browser-use's optional online version check.
Required audit-write failure has real HTTP-effect tests for both adapters; no
request reaches the reviewed or redirected endpoint when persistence fails.

The final additional revision passed **462 full source tests** and **55 installed-wheel
tests** outside the checkout, plus a separate offscreen-target wheel regression.
Two clean base-wheel environments and both desktop dependency checks passed. A
policy edit/restoration each reached Active; actual pending-review cancellation
recorded zero HTTP dispatches. The upgraded visible Playwright journey also submitted
one matching fake request and ended automatically. Matching platform checks remain
attached to the current pull-request revision; earlier failed CI is not credited.

A subsequent lifecycle checkpoint passed 468 source tests and 61 installed-wheel
tests. Real-browser stop/kill checks invalidate that session's pending reviews
without releasing another agent or browser's review. Additional teardown probes
then exposed paused requests escaping when interception was removed; Playwright
also fetched a second copy. Both adapters now abort paused requests before removal,
retire callbacks, and recheck ended/expired authorization after asynchronous waits.
The final teardown revision passed **472 source tests in 455.47 seconds** and
**65 installed-wheel tests in 365.69 seconds**, including fifteen focused lifecycle,
teardown and audit cases with actual HTTP-effect assertions. Two clean base-wheel
environments also passed. Both desktop environments use the same runtime wheel.
A visible local-Ollama draft and Playwright payment each reached its fake server
exactly once and ended automatically; resolving the draft left payment pending.

The preceding d784775 platform run finished with fourteen successful jobs and one
failed Windows/Python 3.11 Chromium job (461 passed, one concurrent-project navigation
failure). That is an unresolved platform gate until the matching new revision passes;
earlier successful matrices and the 468/61 checkpoint are not credited to this code.

## Transport failure and dependency upgrade

The subsequent revision passed **474 source tests in 326.18 seconds**, using
browser-use 0.13.11, aiohttp 3.14.3, cryptography 50.0.2, Pillow 12.3.0, pytest
9.1.1 and pytest-asyncio 1.4.0. Abrupt operation-transport loss fails that run and
blocks further operations; another agent's review remains valid. Deliberate
browser shutdown retains its separate behavior. Chromium can release an existing
paused request on abrupt CDP detach, so zero-effect protection after transport or
process loss is explicitly outside the claim.

The upgraded environment's 116-package advisory check found two remaining affected
upstream pins: AnyIO 4.12.1 and pypdf 6.16.2. Other earlier flagged packages were
upgraded. Publishing now requires a passing dependency audit as well as the
installed-wheel platform matrix. Local fake-data testing remains available; no
dependency override, ignored advisory or public publication is used to claim safety.
The exact installed wheel passed **67 browser/dashboard/failure tests in 233.25
seconds** outside the checkout. Two fresh base-wheel environments passed file-only
operation, late connection, isolated policy revisions and service restart. Compilation,
JavaScript syntax and wheel/source metadata checks passed. Matching-platform checks
remain a separate gate; preceding 472-test results are not attributed to these changes.

## Review capture and storage-failure cleanup

Review images use Chromium's native rendered layout, including CSS-generated text,
closed shadow components and editable descendants. Page overrides of JavaScript
masking APIs cannot hide registered secret text from that scan. Sensitive shadows,
filters, strokes or reflected paint make an image unavailable when masking cannot
be bounded reliably. This is not optical recognition of secrets in arbitrary images.

Transient layout changes allow up to three captures within one five-second budget,
with matching before/after mask and target geometry. Exact adapter target overlays
use fresh coordinates; recapture never repeats a browser action. Real local-model
draft and payment probes produced masked images, rejected their reviews, observed
zero HTTP effects and ended their runs automatically.

Required journal failures remain errors. Review cancellation still clears local
pending state and cancels reviewer work, and runtime shutdown attempts all cleanup
even after a failed heartbeat or journal update. Closed runs cannot create or approve
new reviews. Pytest no longer defaults to one shared temporary directory; concurrent
test runs use independent fixtures.

Configured JavaScript allowlist and audit-redaction expressions have finite matching
budgets. Invalid, excessive or deeply nested patterns are rejected at configuration
load. A stalled allowlist cannot authorize the action; stalled redaction masks the
whole affected value. Fresh same-page submissions are separately reviewed: approve,
reject, approve produces three reviews and exactly two matching requests in both
adapters, with no automatic retry.

This revision passed **497 full source tests in 332.70 seconds** on macOS/Python
3.12 with browser-use 0.13.11 and Playwright 1.60.0. Native-layout privacy tests
inspect actual masked pixels, and the real local Ollama draft/payment probes each
produced a masked review image, zero requests after rejection and an ended run.
The refreshed advisory scan queried 117 installed package versions with no missing
responses; the two upstream-pinned affected packages above remain a publication
gate. The exact installed wheel passed **90 browser/dashboard/failure tests in
246.88 seconds**, and the full installed Python 3.11 suite passed **497 tests in
337.60 seconds**. Two fresh base-wheel environments also passed. Matching-platform
results remain a separate gate; earlier passing matrices are not attributed to it.

A subsequent visible Retina test found that native mask bounds needed device/CSS
conversion and that a Playwright target could sit below the captured viewport.
The repair converts native rectangles using browser metrics, captures an explicit
visual-viewport clip, scrolls exact element targets before review and omits an
image whose target is wholly outside it. Twenty-three focused pixel/target checks
passed across fractional/Retina scales, CSS/pinch zoom and scrolling. The 497/90
source/wheel checkpoint above does not cover this subsequent correction. The
corrected revision passed **508 full source tests in 345.79 seconds**, **508 full
installed Python 3.11 tests in 343.83 seconds**, and **101 installed Python 3.12
browser/dashboard/failure tests in 253.88 seconds**. Visible masked Playwright
payment and real Ollama draft approvals each sent one matching fake request and
ended automatically; resolving the draft left the independent payment pending.

The narrow Retina review journey additionally checks that the target outline stays
legible when its high-resolution image is scaled down. Its reproduced sub-pixel
outline is now drawn with a three-CSS-pixel width at display time. The full final
revision passed **509 source tests in 350.75 seconds**, **509 full installed
Python 3.11 tests in 347.34 seconds**, and **102 installed Python 3.12 browser,
dashboard, privacy and failure tests in 261.70 seconds**. Two clean base-wheel
environments passed again; both desktop setups match all 97 packaged runtime files
and pass dependency validation. The actual local-model destructive block sent zero
delete requests, and a dashboard rejection sent zero additional payment requests.

The new platform gate is unavailable: GitHub refused the latest matrix before jobs
started because the repository owner's payments/spending limit require repair.
Billing was not changed. The preceding successful 15-job shutdown matrix does not
establish Windows/Linux results for the subsequent capture, dependency, cleanup or
expression changes. Use the matching PR checks when that external gate is repaired.

## Acira-owned repository verification, 2026-10-10

The complete portable snapshot is now in the private
[Acira-dev/acira-sdk repository](https://github.com/Acira-dev/acira-sdk).
Commit `7111bbf9f61f0f8575b3912c50a6472a29888ffa` passed all **15 jobs** in its
[matching CI run](https://github.com/Acira-dev/acira-sdk/actions/runs/38045735594):
all-platform core and clean-wheel jobs, and installed-wheel Chromium tests on
macOS, Windows and Linux with Python 3.11 and 3.12. A fresh-checkout test-directory
setup failure in the initial migration run was repaired before this successful
run. Local default-path verification passed **509 tests in 347.32 seconds**.
Historical source-repository billing failures are not failures of this new run.

GitHub accepted the new repository's `pypi` environment, exact `main` branch
restriction and disabled administrator bypass, but rejected required reviewers
because of private-repository plan limitations. The release workflow verifies the
actual provider controls and blocks upload if approval rules are missing; it does
not interpret a partially created environment as an approved release route.
See [the release guide](release.md). Publication remains separately gated by the
dependency audit and the final revision's checks.

## Full delivery sequence, preserved

1. Protection foundation — implemented with the explicit supported-boundary matrix,
   real browser effects, exact operation binding and adversarial dispatch checks.
2. Installable SDK and setup — implemented and verified from a clean wheel.
3. Shared local service — implemented; late connection, separate environments and
   server restart verified.
4. Complete journey — implemented and exercised with two projects plus dashboard review.
5. Five-project pilot — protocol and empty measurement template prepared. Real project
   selection and two weeks of voluntary use remain external work.
6. Full policy and approval workflows — implemented for the documented supported
   contracts. Unsupported payload encodings and integration paths fail closed; their capability
   limits are explicit.
7. Full history and evaluation — implemented. Unavailable runtime/provider/mapping
   evidence remains indeterminate rather than being fabricated.
8. Dashboard and release hardening — all main views and installable artifacts implemented;
   the platform matrix has executed successfully. Final-source checks must also pass
   before publication. Native Chrome consent/denial and authenticated pairing passed.
   Publication access remains an account-owner operation. Independent security and
   assistive-technology review are recommended.

## Remaining release work and external validation

- Require the current source revision's full CI matrix before publication. The Linux
  repair and expanded six-combination run passed without skipping browser-use tests.
- Test Edge separately before claiming native Edge permission verification. Native
  Chrome's unchanged manifest passed the deny/allow/pairing journey; server-side
  revocation has automated coverage. Shipped host access remains optional. Store
  publication is separate from the SDK wheel.
- Exercise an actual screen reader as follow-up accessibility validation. Keyboard
  setup, editors, approvals and recovery have automated browser coverage.
- Current background polling plus mandatory action-boundary reads provides automatic
  policy updates. Platform-native watchers are a possible responsiveness optimization,
  not a missing synchronization requirement.
- Complete the five genuine project integrations and two-week pilot. No adoption,
  maintenance savings or product demand has been fabricated.
- The pending trusted publisher under **Acira-Dev** on PyPI now names
  `Acira-dev/acira-sdk`, workflow `release.yml`, environment `pypi`. The old
  `FouadDim/Acira-Browser-Security` publisher was removed after verifying the new
  route. GitHub's `pypi` environment restricts deployments to `main` and disables
  administrator bypass, but required reviewers are unavailable under the current
  private-repository plan. On 2026-10-11 the owner explicitly approved a free
  Acira-owner-only workflow approval after verification, replacing the unavailable
  environment reviewer. Exact source, completed test matrix, audit and artifact
  hashes remain enforced. GitHub administration access is verified for the new
  repository. See [repository provenance](repository.md).
  Publication also remains gated by passing checks and the dependency audit. Confirm
  extension-store access separately; repository migration is not store publication.
  The verification workflow never publishes. Publication requires a separate
  owner request after verification completes; it cannot approve a failed run.

These are retained commitments and explicit limits. They are not deleted scope.

## Public documentation test drive

`acira demo --public-sites` adds an installed-wheel Playwright journey on Python,
MDN and Playwright's real documentation sites. Each website has independent
browse, review and blocked agent configurations. The UI exposes navigation results,
pending review guidance, cancellation and automatic run completion. Existing local
and browser-use journeys remain available.

The runner uses fresh visible Chromium sessions and a read-only network boundary:
same selected documentation host, GET/HEAD, constrained document paths, no account
or payment paths, no downloads, service workers or WebSockets. A narrow version
parameter is allowed for Sphinx static assets; navigation queries remain blocked.
These are test-drive restrictions, not a new claim of an operating-system sandbox.

Real-site testing exposed hidden duplicate MDN links and Playwright's in-page
navigation. The runner selects a visible link and verifies both destination URL and
visible heading. Document responses must succeed when present; an in-page route
change does not require inventing a document request. Offline real-browser fixtures
exercise both paths for repeatable CI, plus no-side-effect blocks/rejections,
duplicate decisions, cancellation, request restrictions and preserved policy edits.

Public-site validation is separate from the existing offline regression matrix;
public sites can change or become unavailable. No account, transaction, paid model,
unattended approval default or SDK bypass is used by the interactive journey.
See [hands-on instructions](public-websites.md). PyPI publication remains gated as
described above; an installed local wheel is not a public upload.

## Verification commands

```sh
python -m pytest -q
python -m compileall -q acira acira_cdp_proxy acira_dashboard
node --check acira_dashboard/static/workspace.js
node --check tools/acira-capture-extension/background.js
node --check tools/acira-capture-extension/content.js
node --check tools/acira-capture-extension/connection.js
git diff --check
python -m build
python -m twine check dist/*.whl dist/*.tar.gz
python tools/verify_wheel.py dist/acira-0.1.0-py3-none-any.whl
python tools/prepare_release.py dist
```

The real-browser tests require installed Chromium. Browser-use tests explicitly disable
telemetry/cloud synchronization and use temporary profiles. Test runtime data, credentials,
profiles and review images are ignored and must not be committed.

## Large signed-out public-site validation — 2026-10-11

The installed SDK completed a 393-host real public-site campaign. Public HTML
was observed on 279 hosts, 242 loaded with Acira, and 193 passed all five baseline,
normal-policy, block, rejected-review and approved-review checks. The 57 partial
and 143 unavailable outcomes remain in the evidence. These counts cover public
content-link navigation, not every operation, authenticated tasks or live payment
flows.

A durable-journal cross-check found zero duplicate dispatches, zero blocked-profile
click dispatches and zero dispatches for rejected reviews across 10,215 events.
Screenshot availability is recorded independently; privacy masking was not weakened.
The test driver gained verified redirect/teardown boundaries and exact-element
selection after a real changing-menu issue was reproduced locally. Product runtime
files are unchanged by this testing work.

The full local suite passed 590 tests; all 25 sweep fixtures also passed against
the installed SDK outside the checkout. Current GitHub CI execution remains blocked
by the account's billing/spending restriction. This does not establish a new
cross-platform CI pass or complete the publication gate.

See [the recorded outcomes and limits](validation/2026-10-11-public-sites/README.md)
and [the repeatable public-site method](public-site-sweep.md).

## Public-site repairs and candidate verification — 2026-10-11

The [repair comparison](validation/2026-10-11-public-sites/repairs/README.md)
repeated the 200 incomplete hosts plus three controls. It retains **23 complete,
53 partial and 127 unavailable** outcomes, including 20 newly complete workflows.
These repeated hosts are not additional distinct websites. The comparison preserves
access failures, test-scope restrictions and uncertain outcomes instead of treating
them as successful coverage.

Playwright now distinguishes a detached target from one that cannot be inspected,
and reports that no browser action was dispatched. Two real-browser fixtures verify
zero effects and zero reviews for the failed attempt, followed by a separately
requested fresh target and review. No automatic retargeting or retry was added.
The complete source suite passed **603 tests in 509.67 seconds** on macOS/Python 3.12.
The entire suite also passed against the installed wheel outside the checkout on
Python 3.12: **603 tests in 505.80 seconds**, with enforced site-packages imports.
Python 3.11 passed the same installed-wheel suite: **603 tests in 502.72 seconds**.
Both integrations were installed: Playwright 1.60.0 and browser-use 0.13.11.

A fresh source archive and wheel built from that archive passed Twine validation.
All **84 Python modules and 26 resources/assets** in the wheel match the current
runtime source. The extension ZIP contains nine matching source files, and all
three distribution checksums were verified. The clean-wheel journey again passed
with two environments and projects, late dashboard connection, independent policy
updates and service restart. Wheel SHA-256:

```text
b8385d87a1698f0153ec75456adbb25fe07e2d0249ba49ff724d2da2cf117970
```

The dedicated core/Playwright environment had no active PyPI advisory records after
updating its old pip installer. Browser-use 0.13.11 still requires AnyIO 4.12.1 and
pypdf 6.16.2; their advisory records remain a publication blocker. These checks do
not establish an absence of undisclosed vulnerabilities.
The audit queried 123 distinct package versions across the three dedicated
environments with no unavailable queries. The old Python 3.11 build tool was also
updated; no declared browser-use pins were overridden. See the
[candidate verification record](validation/2026-10-11-public-sites/sdk-candidate.json).

The current Acira repository CI run did not execute any test steps: GitHub reported
failed account payments or a spending limit. The private repository's publishing
environment also still lacks the required reviewer controls. Local verification
does not replace the current cross-platform release matrix or authorize bypassing
these gates. The existing pending trusted publisher is not a public PyPI upload.

## Browser-use dependency repair — 2026-10-11

The release extra now selects the compatible upstream **browser-use 0.11.12**,
which permits fixed **AnyIO 4.15.1** and **pypdf 6.20.0**. Its missing
pydantic-settings dependency and the tested integration libraries are explicit
requirements. Browser-use remains included as an optional integration. No upstream
package metadata was overridden and no advisory IDs were ignored.

The adapter supports framework versions that recover through connect() directly,
and blocks reconnection after deliberate shutdown. Its review-aware action timeout
no longer depends on a newer private vendor helper; invalid timeout inputs retain a
finite bound. Real-browser fixtures verify shutdown cannot revive a stale approval.
The agent fixtures verify approval executes once, rejection/cancellation produces no
effects, and model failure does not fabricate success. No paid model/API was used.

The entire source suite passed **612 tests in 640.67 seconds**. Clean installed-wheel
suites outside the checkout also passed all **612 tests** on Python 3.12
(**624.48 seconds**) and Python 3.11 (**602.99 seconds**), with no skips.
Both integrations were installed. Clean installs selected websockets 16.1.1; the
source environment exercised 15.0.1. Dependency consistency passed in both clean
environments. The advisory check covered **118 distinct external package versions**
across those environments, with zero active findings or unavailable queries. The
pip-audit check independently covered 115 external dependencies; only the unpublished
local SDK lacked a PyPI registry record.

The wheel and source archive passed Twine validation. All 110 packaged runtime and
dashboard files, including 84 Python modules, match the source. The two-project
clean-wheel journey passed again: file-only startup, connection to a late-started
shared dashboard, separate policy updates, service restart and stable event IDs.
The exact tested wheel SHA-256 is
`c712e6758d3c458f7e7d05988ec7b955f4607ec07fbb10fac154ca66004557ab`.
See the [dependency repair record](validation/2026-10-11-public-sites/dependency-repair.json).

These checks resolve the recorded optional-integration dependency findings, not
every release gate. GitHub-hosted CI remains unavailable while its included allowance
is exhausted with paid usage disabled. The repository remains private. The owner
subsequently approved the free approval flow described below. No PyPI distribution
has been uploaded. Local macOS results do not establish a new Windows/Linux matrix
pass, and a clean advisory query does not establish an absence of undisclosed issues.

## Free release approval and local runners — 2026-10-11

The Acira owner explicitly approved replacing the unavailable private-repository
reviewer feature with a separate owner-only manual publish request after verification.
No paid service, repository visibility, budget, or spending limit was changed.

`verify-release.yml` builds one distribution pair and requires the full source and
installed-wheel suites on Linux, macOS, and Windows with Python 3.11 and 3.12,
browser-effect checks, clean-wheel journeys, script validation, dependency consistency,
and an advisory audit. Release-mode tests fail if any test skips. `release.yml` accepts
only a completed successful verification from the same main commit, an actual Acira
owner request made afterwards, and exact wheel/source-archive SHA-256 hashes. It
downloads the verified artifacts without rebuilding. Only the isolated publishing
job has a short-lived PyPI upload identity.

The temporary runner helper registers one-job runners with a label unique to the
selected workflow run. Its pre-job hook rejects unrelated jobs before executing their
steps. It validates the owner and commit, verifies the official runner download
checksum, bounds the runner lifetime, and removes its registration and files on exit.
It installs no background runner service.

Local validation of the final changes passed **671 tests in 604.20 seconds**, with
release mode enabled and no skips. The focused release-approval, skip-gate, and runner
checks passed **74 tests**. Both release workflows passed actionlint 1.7.12, and the
diff passed whitespace checks. These local results do not substitute for the required
six platform/version jobs or indicate that PyPI publication has occurred.

The first self-hosted request stopped before SDK tests: setup-python's Mac path
required a privileged `/Users/runner` directory. The runner now prepares isolated
environments from verified native Python 3.11 and 3.12 installations and selects
them without using that installer on a self-hosted Mac. Versions, architecture,
cache labels, and environment locations are checked before activation. Hosted
runners and Linux/Windows retain their official setup-python step. Real native
selection and GitHub environment activation were exercised locally. After this
repair, the complete suite passed **689 tests in 586.07 seconds**, without skips;
the focused release checks passed **92 tests**. Workflow validation also passed.

## Specific-policy capture repairs — 2026-10-12

The captured-policy workflow has further repairs for wrong-kind candidate ranking,
unchanged identity under score ties, early page input listeners, authenticated
extension selection routing, and document listeners swallowing the capture panel.
The extension is now **0.1.8**. It pauses visibly after selector failure and keeps
exportable drafts; it does not fall back to activating a website control.

The installed-wheel campaign uses actual extension exports and terminal imports
and configuration. Its driver now requires the same control identity across the
baseline, capture and protected actions. A duplicate-destination regression prevents
capturing one link and claiming another link was protected. Stage/cleanup evidence
also distinguishes a completed export from failed teardown. Original failures,
interruptions, unavailable sites and uncertain outcomes remain recorded in the
[new specific-policy report](validation/2026-10-12-specific-policies/README.md).

The final SDK runtime passed **731 tests, without skips**, in each installed-wheel
environment on macOS and Linux with Python 3.11 and 3.12. After adding five driver
regressions, the full source suite passed **736 tests** on macOS/Python 3.12;
all **17 current driver tests** also passed against the installed wheel. Two fresh
base-wheel environments passed setup/assets, file-only enforcement, late shared
service connection, separate revisions, restart and event identity checks. A real
terminal PTY verified capture/import idempotency, rejection without navigation,
and approval with exactly one observed navigation. Four real documentation sites
also completed that workflow through actual terminal prompts; explicit mapping
reviews remained separate from action reviews. The 118-version dependency
advisory query had no remaining findings or unavailable results after updating
cached Linux build tools. No paid API, new model or new VM was downloaded.

These are preview checks. They do not establish the current Windows gate, exact-head
formal release verification, every website, authenticated workflows or transactions.
Public PyPI publication is still pending the full release gates and owner approval.
Native Chrome setup was not credited: the UI tool could not select the isolated
signed-out process, and the existing user's profile was left unchanged. Follow the
[terminal-first guide](specific-policies.md) to replace an older same-version preview
wheel and reload or first-load the exported extension.

A TechCrunch export timeout was reproduced and traced to its response's CSP
sandbox lacking `allow-downloads`. The existing Copy JSON export completed the
full installed-wheel policy journey there with genuine clipboard readback;
no page restriction or production extension permission was changed. The setup
guide now explains this recovery path. Original download failures remain recorded.

The final control-bound campaign attempted all **393 distinct catalog hosts**:
**199 complete journeys**, **23 partial** and **171 unavailable**. It includes
430 attempts and 201 completed attempts after follow-ups. Every completed attempt
passed its exact-control/specific-policy audit check. Final journals contain zero
blocked or rejected click dispatches and zero duplicate dispatches. Five browser
calls remain unknown and are not retroactively marked successful. Per-host
results and the remaining diagnoses are in the validation report; these public
content checks do not establish authenticated or transaction coverage.

## First PyPI release platform scope (2026-10-12)

The owner explicitly selected a **macOS/Linux-first release**, with Windows
validation to follow. The release workflows retain their default all-platform
option and Windows remains in ordinary CI. The smaller scope is recorded in the
trusted verification run title and must match the publication request; it cannot
turn a failed or incomplete all-platform run into a successful release.

For the first release, all four native combinations of Linux/macOS and Python
3.11/3.12 must pass the complete source and installed-wheel suites, with skipped
tests rejected. Chromium checks, two clean installation environments, dependency
consistency, the advisory audit, exact source and artifact hashes, Acira-owner
approval, and the main-only publishing environment remain required. The repo
stays private and paid GitHub usage stays blocked. Windows support is not claimed
until its tests pass. See [release instructions](release.md) and [first setup](setup.md).

Preparing these controls or registering a pending publisher does not establish
that an upload occurred. Confirm public versions on the
[Acira PyPI page](https://pypi.org/project/acira/); only an actual successful upload
makes public pip installation available.

## Public release and separate Playwright setup (2026-10-12)

Acira **0.1.0** was published successfully on
[PyPI](https://pypi.org/project/acira/0.1.0/) from commit
`6c9f6f495780621c126445b58f5e1daca8a51601`. The
[verification run](https://github.com/Acira-dev/acira-sdk/actions/runs/38239393380)
passed all four Linux/macOS and Python 3.11/3.12 combinations: 750 source tests
and 750 installed-wheel tests in each combination, totaling 6,000 passes with
zero skips. The audit covered 128 installed packages with no known advisories.
The separate
[publication run](https://github.com/Acira-dev/acira-sdk/actions/runs/38244760852)
passed owner approval and exact-artifact checks, then uploaded through trusted
publishing. Fresh public-index installations passed on macOS and Linux, including
the CLI, policy initialization and extension export. GitHub stayed private, no
payment or spending change was made, and temporary runners were removed.

The **0.1.1** candidate makes the normal setup explicit: use or install
official Playwright independently, then install `acira` in the same project
environment and add the protection snippet to the actual application. The demo
remains optional after integration; `acira[playwright]` remains a secondary
convenience shortcut. Compatibility work adds explicit framework diagnostics, guarded rejection of external
drops/storage mutations/virtual credential APIs, safe dependency bounds and separate
reproducible release constraints. The release matrix now requires all five standard
CPython versions from 3.11 through 3.15 on each selected operating system.
These additional changes require a fresh source/wheel matrix; the documentation-only
verification is baseline evidence and is not sufficient to publish this candidate.
PyPI stores the first uploaded description for a version, so this README change
requires a patch release. Version 0.1.1 must pass fresh verification and publication
before it is described as uploaded. Windows validation remains pending.

## Compatibility expansion candidate (2026-10-12)

The documentation baseline at `1f8943bf1145dd2c8cd2717a8df7023197aaf173` passed
6,000 native source/installed tests on macOS/Linux × Python 3.11/3.12 in
[verification 38249530317](https://github.com/Acira-dev/acira-sdk/actions/runs/38249530317).
Additional isolated macOS Python 3.13.13/3.14.7 source and wheel suites passed
750 tests each, totaling 3,000 further passes with no skips; both clean two-project
journeys passed. These are baseline proofs, not verification of the final candidate.

The safe browser-use 0.11.13 dependency candidate resolved normally with aiohttp 3.14.5
and Pydantic 2.14.1 and passed 103 focused tests with no skips, plus a 130-package
third-party advisory audit with no known vulnerabilities. Playwright 1.63 testing
found unguarded new mutation surfaces, now explicitly rejected with no browser
side effects in focused tests. Full latest-framework and final source/wheel/platform
verification remain required before publication or expanded support claims.

The setup continues to install official Playwright separately before base Acira.
The demo and dashboard remain optional. GitHub remains private; Windows remains
pending its native release checks. No paid API, subscription or spending change
is part of this compatibility work.


## First-run polish candidate (2026-10-12)

The glitch-experience and SDK-polish reports were checked against the current
implementation, not treated as release evidence or instructions to weaken protection.
The integrated changes include idempotent initialization, catalog-name imports,
path-aware capture errors, safe extension-export updates, human/JSON doctor output,
truthful attachment status, public `ActionBlocked` handling and visible review waits.
New notices respect quiet logging; human-review prompts remain independent.

The isolated setup review passed 65 focused tests without skips. Diagnostics are
read-only: default bundled-Chromium absence is an actionable failure, while explicit
`--skip chromium` marks an application's custom browser unvalidated. Successful
setup checks do not establish that application actions are protected.

The universal optional-provider stack uses protobuf 6.33.6 and normally resolved
Vertex dependencies. On native Python 3.15, all extras install consistently and
Google provider imports and generated-message round trips pass without clients,
credentials or cloud calls. Its complete installed advisory audit covers 141
third-party packages with no known advisories or third-party skips. Final source,
installed-wheel and native-platform verification remain required for this revision.
See [troubleshooting](troubleshooting.md) and [compatibility](compatibility.md).


The integrated native macOS/Python 3.12 source suite passed **916 tests, zero
skips**, using Playwright 1.63.0, browser-use 0.11.13 and every optional provider.
The unchanged all-extras cases affected by optional development collection guards
also passed 27 focused tests without skips. A fresh base-only environment passed
635 core tests; its 46 optional integration exclusions are development evidence,
not release passes. The final native source/installed-wheel matrix must execute
the full collected candidate suite with no skips in each selected slot before publication.

Independent review also repaired malformed service descriptors: invalid URL/key
types fail before authenticated connection and diagnostics do not echo private
field values. Seventeen new regressions and ten existing authentication/diagnostic
cases passed; the full source result above includes the integrated repair.


## Native test setup and cleanup corrections (2026-10-12)

The first expanded verification exposed Ubuntu user-namespace restrictions on
Playwright’s default headless-shell executable. The CI setup previously permitted
only the full Chromium executable. It now resolves both installed executables from
the official Playwright CLI, rejects ambiguous or escaped paths, and grants only
two literal executable profiles. Both ordinary headless and explicit full Chromium
startup are verified with the sandbox enabled; global kernel restrictions remain
enabled. This setup runs only in disposable CI, not during SDK installation.

The external-CDP test now retains browser stderr and asynchronously terminates,
kills when necessary, and reaps only its owned process. Cleanup no longer masks an
original connection failure or blocks the event loop. Regression tests exercise
normal termination, forced shutdown, failure evidence and repeated cancellation.
The initial intermittent framework startup timeouts are retained as failed attempts;
a later unchanged pass is diagnostic evidence, not proof of their exact cause.

The native verification job budget accommodates ordinary dependency/browser setup
and two complete suites on the retained free Linux VM. Browser startup, action,
approval and protection assertions remain unchanged. A fresh exact-source build,
full source/installed-wheel matrix and dependency audit are required before
publication; earlier successful slots cannot authorize the changed candidate.

The lifecycle review also reproduced an active page-preflight task surviving its
proxy connection shutdown. The guard now tracks each task’s owning connection,
cancels only that connection’s tasks and reports cancellation exceeding its
one-second bound. The proxy preserves unknown-outcome recording, client closure
and the original error when cleanup fails. Local-loopback regressions verify actual
server stop, two-connection isolation and bounded cancellation; this does not
establish the cause of the earlier browser startup timeouts.

Pending-review ownership is also cleaned on cancellation and setup exceptions,
including closed terminal output or interruption. Canceling a duplicate waiter
keeps the legitimate owner review active; canceling the owner releases its shared
waiters without approving an action. Unrelated review keys remain independent.
The integrated candidate collects 959 tests; 109 focused lifecycle, preflight,
browser-setup and release-gate cases passed without skips. Full exact-source
verification remains required.


### 2026-10-12 — Outbound submissions and native confirmations

The integrated patch adds the `submissions` catalog policy and terminal setup for native forms and recognized Submit/Send/Post/Publish/Apply/Finalize identities. Existing initialized projects opt in; saving a new bundled policy does not silently rewrite their policies. Verified JSON/form mutation requests use the existing one-attempt `guard.submit(...)` contract. GET/search and unknown keyboard transports remain blocked with this policy enabled.

General evidence requirements survive captured, secret, core and mapping reviews. Page denials override action reviews; page acknowledgement and operation approval have separate scopes. Review requirements come from the current authoritative decision rather than caller-supplied or stale page metadata. Historical events without precise native-association facts remain indeterminate when replayed against a policy requiring those facts.

Native confirmation acceptance cannot inherit a click approval. Protected Playwright modals are rejected and dismissed on their owning page; browser-use cancels the exact detecting session instead of invoking upstream automatic confirmation. Free-text modal data is redacted before persistence. This does not contain arbitrary page JavaScript or unrelated connections.

The [submission validation report](validation/2026-10-12-submissions/README.md) records real local browser effects, Chrome capture/terminal import, review binding and signed-out public inspection. Full qualification of the new frozen source and installed wheel remains required; earlier verification of `1bc1006` does not qualify this changed candidate.
