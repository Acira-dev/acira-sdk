# Protection contracts and release coverage

Acira authorizes supported adapter operations. Application code deliberately using an
unrelated browser, HTTP client, private Playwright internals, or an independent CDP
connection can bypass that integration. Acira does not claim to sandbox application
code or browser extensions. Installation alone provides no protection.

## Supported boundaries

| Path | Enforced behavior | Evidence in this checkout |
| --- | --- | --- |
| Async Chromium Page / Locator / ElementHandle | Resolve a unique target, evaluate policies, review if needed, recheck, dispatch the pinned element once | Real DOM rejection, changed form, clear, input, keyboard tests |
| Frames / popups / persistent contexts | Inherit the owning run's policy and state | Real frame and popup rejection tests |
| Mouse / touchscreen | Resolve the pointed element; block ambiguous frame/shadow hosts and moved/outside targets | Real coordinate/touch rejection tests |
| Keyboard | Bound target and values; Enter variants classify as submission | Real Enter/Control+Enter and text rejection tests |
| File inputs / file chooser | Review filename/size, freeze files up to 32 MiB total, reject changed disk contents; directories unsupported | Actual empty FileList after blocked/changed upload |
| CDP-connected Playwright | Same public boundaries as local launch; agent-created raw CDP mutations/scripts denied | Real Chromium CDP connection with blocked HTTP side effect |
| Script execution | Undeclared script/CDP mutations fail closed; explicitly configured exact read-only JS exceptions remain advanced opt-ins | Real evaluate/CDP rejection tests |
| browser-use | Supported framework events plus CDP classification, isolated review, resolved target revalidation | Real type/element activation, resolved frame nodes, dropdown, frozen upload and tab workflows; unverified coordinate targets rejected |
| Critical request contract | Playwright and browser-use single JSON/form request contracts | Local HTTP server observes one exact approved request or zero on rejection/mismatch |

The dashboard's Inspect view and `acira doctor` report capabilities and limitations.
Firefox, WebKit and the synchronous Playwright API are not supported. Forced pointer
actions, arbitrary event dispatch, drag, unbounded key/mouse sequences, directory
uploads and unclassified agent scripts are rejected rather than shown as protected.

Playwright's selector engine executes internal Runtime scripts. CDP-connected
Playwright therefore uses the same public boundary as local Playwright instead of
passing those scripts through the generic CDP proxy. This avoids granting broad
script exceptions. The standalone proxy and browser-use CDP integration remain.

Executable/opaque navigation URLs (javascript:, data:, vbscript:, blob:) are
blocked through the shared evaluator. Creating a tab is evaluated as navigation;
activating an existing tab does not authorize its next action.

In project mode, browser-use element clicks use the exact DOM activation algorithm
once. The reviewed DOM object is pinned and its observed values checked again in the
same JavaScript task before activation. It does not generate pointer events or run
the native pointer/JavaScript fallback chain. Coordinate actions remain separate and
reject frame targets without verifiable point-to-element evidence. Secondary button
activation is unsupported. Legacy config-only native handlers remain available.
Frame screenshots are omitted when their coordinate projection is not verified;
structured review evidence remains visible. Browser-use uploads freeze
up to 32 MiB per file, recheck the source after review, and stage a private copy for
Chromium's later reads. Copies are removed at normal run shutdown, with a 256 MiB /
128-file per-run limit. Forced process termination can leave private temporary files
for operating-system cleanup. They are never included in history or exports.

### Ubuntu Chromium sandbox setup

Ubuntu 24.04+ restricts user namespaces used by downloaded Chromium builds. A
browser-use launch can fail before the SDK connects if the executable has no
matching AppArmor permission. Acira does not disable Chromium's sandbox or change
operating-system security settings during installation. Use the distribution's
supported browser installation, or have the system administrator configure a
profile for the exact trusted executable following the
[Chromium sandbox guide](https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md).
Avoid a system-wide restriction change or `--no-sandbox` for ordinary browsing.
The CI setup grants userns only to its disposable runner's exact browser path and
verifies sandbox-enabled startup before running the integration tests.

Read-only observations are not approvals. Download/export policies govern the browser
action that requests the download; a downloaded file is not automatically trustworthy.

## Operation contracts

An ordinary reviewed DOM operation binds project, agent, run, policy revision, target,
page/frame, observed form state, destination and bounded expiry. It rechecks immediately
before dispatch. It cannot establish server-side account identity from a domain or
prove all behavior of arbitrary JavaScript handlers.

`guard.submit(locator, destination=..., method="POST", encoding="json", values=...,
facts=...)` adds an explicit single-request contract:
- `values` is the complete flat JSON or form body (at most 100 scalar fields and 64 KiB).
- `facts` maps semantic labels such as amount, recipient and account to body fields.
- The actual URL, HTTP method, content type and body must match.
- Only one request can be sent. Subsequent application retries to that endpoint remain
  blocked until a new explicit contract authorizes another operation.
- A mismatch, timeout or missing response does not authorize a retry.
- Redirects never extend approval to another destination, including after timeout.
  The original reviewed request may already have taken effect; blocking its redirect
  records an unknown business outcome and never authorizes automatic retry.
- Unreviewed background fetches are blocked while the contract is active. A blocked
  unrelated GET/HEAD/OPTIONS request does not resolve the reviewed mutation's outcome;
  that operation still requires its own matching request and observed response.
- A response's HTTP status is recorded as observed; business success remains unverified.
- A callback that was paused at its final authorization boundary is checked again
  after that wait. Expired contracts and ended runs cannot dispatch it. SDK exit
  aborts pending HTTP routes/Fetch requests before removing interception, then
  retires the callbacks; it must not release the original browser request or fetch
  a second copy. Already dispatched attempts remain potentially effective and are
  never retried automatically. If abort acknowledgement fails, report teardown
  failure and close the owned browser rather than detach its interception.
- Playwright requires a context owned by one Acira run, with service workers blocked.
  Its routing covers frames, workers and popups. Remove page-level routes first;
  after installing a contract, Acira owns network routing until the run ends. Only
  one submission may be active per browser context.
  The matched request is fetched with redirects and transport retries disabled;
  only a direct response is fulfilled to the browser.
- Browser-use uses `guard.submit(browser_session, node, ...)`. It bypasses page service
  workers, intercepts the page request, and pauses newly attached popup/frame/worker
  targets until interception is installed. An unreviewed request from those related
  targets is blocked. Framework auto-resume cannot race the interception setup.
  One interception owner covers the exclusively owned browser, so different tabs
  can each receive their own explicit contract without conflicting interceptors.
  One submission may be active in that browser session at a time.
  Explicit browser-use stop/kill cancels that session's guarded operations and
  invalidates pending reviews before vendor state saving. Lifecycle waits retain
  their normal time bounds when another session is under review; those other
  reviews stay held and their mutations remain guarded. A stopped session cannot
  automatically reconnect or reuse a stale operation. Use a fresh BrowserSession
  for subsequent protected work.
- Unexpected operation-CDP transport loss is different from controlled SDK exit.
  Chromium may release an already paused request when that interception connection
  abruptly disappears. Acira records an unknown outcome, fails the affected run,
  invalidates reviews/grants and blocks further guarded operations; close the owned
  browser and start a fresh run. This integration cannot promise zero browser
  effects after its controlling transport or runtime process is forcibly lost.
  A surviving external browser is not an operating-system/network sandbox.
  While waiting for an authorized submission's activation or network response,
  this loss raises `AciraActionBlocked`. The error denotes
  an unknown outcome and prohibits automatic retry; it does not establish that
  the browser produced no side effect.
- The initial endpoint budget is 1,000 contracts per run and related-target budget is
  64 attached targets. Exceeding a budget requires a fresh run rather than unbounded
  routing or event state.
  Browser-use also limits remembered critical request identities to 1,000 attempts
  per run, so expired approvals remain restricted without unbounded state.
- Nested JSON, multipart bodies and server-side account verification require additional
  contract implementations; unsupported evidence cannot receive a protection claim.
- The built-in payment submission policy requires amount, currency, account and request
  binding. Missing evidence blocks instead of showing an incomplete approval.

`guard.fill_fields([(locator, text), ...], purpose=...)` groups 1–30 unique text fields
in one form on one page. It evaluates every step, displays proposed changes together,
and checks scope at each dispatch. It does not submit. Unexpected state changes stop
remaining steps; already executed steps cannot be rolled back. Captured specific-policy
matching in a grouped fill is not supported; use separately guarded fills there.
Browser-use provides the same bounded API as `guard.fill_fields(browser_session,
[(node, text), ...])`, with unique text controls in one verified HTML form.

Routine grants are separate from operation grouping and event deduplication. They are
explicit, limited to one run/policy/context/target/action, expire within 15 minutes and
are revoked by context/policy changes, run completion or the user. Critical operations
always require fresh operation-specific authorization.

## Review and privacy

Terminal and dashboard answers use one persistent compare-and-set decision. The first
valid answer wins. Reviews expire after five minutes; missing reviewers never imply
approval. Per-run pause and review state do not freeze another agent.

Review screenshots use the browser integration, not desktop screen recording.
Sensitive controls, embedded frames/canvases and detected secret text are masked
before any persistence. Mask discovery uses Chromium's native rendered layout,
including closed shadow components and CSS-generated text, rather than page-owned
JavaScript APIs. Sensitive text with unbounded paint effects, such as text shadows
or ancestor filters, makes the image unavailable. This is not optical detection of
secret values drawn inside arbitrary images. Capture takes at most three attempts within five seconds;
each image requires matching page/mask geometry before and after capture. Exact
adapter targets also require matching fresh highlight coordinates. Recapturing an
image never repeats the guarded operation. Native mask coordinates are converted
using Chromium's reported device/CSS metric ratio, and capture uses an explicit
visual-viewport clip. Fractional/Retina scale, page zoom and scrolled masks have
pixel regressions. Playwright scrolls a resolved element into view before capturing
review evidence; coordinate actions preserve their original point. A target wholly
outside the captured viewport cannot produce a misleading highlight.
If stability cannot be verified, Acira
omits the image. Structured evidence remains visible. These rules cover Acira review artifacts;
they do not control screenshots independently captured by an agent or another tool.

Secret values with the same reference name in different projects retain separate
redaction entries. Starting or ending one integration does not erase another's
masking. Entries remain private to the process and stay valid while a guard is
active, then expire within one hour of the last guard closing. Count/byte limits
remain enforced. Reaching a limit refuses new plaintext releases; it never evicts
an existing entry to permit a fill.

The shared service binds only to loopback, authenticates reads/writes, validates Host
and Origin, uses a one-time UI ticket and same-site cookie, and separates SDK,
dashboard and extension capabilities. Project registration establishes policy write
locations. Operating-system filesystem permissions still apply.

Configured regular expressions use Python-compatible syntax and bounded matching.
Custom audit patterns share a 100 ms budget per event; exhaustion masks the whole
affected value and marks `custom_pattern_timeout`. Read-only script patterns share
a 50 ms classification budget; exhaustion returns an unsupported/read-write result,
never an allowlist match. Each configuration accepts at most 32 expressions of 2,048
characters. Oversized configurations fail validation with an explicit repair error.

Google's [Chrome 155 announcement](https://developer.chrome.com/blog/debugger-enterprise-policy-restrictions?hl=en)
confirms stricter `chrome.debugger.attach()` restrictions in managed browsers with
configured host, screenshot or DLP restrictions, starting October 6, 2026. Acira
Capture 0.1.2 requests `activeTab`/`scripting` and never calls `chrome.debugger`, so
that attach-time change does not affect its capture path. Ordinary enterprise
extension/host restrictions still apply; native managed-browser testing remains
an external verification gate. Do not disable administrator protections to test it.

## History and replay

Durable events distinguish proposed, evaluated, authorized, dispatched, browser-call
completed and unknown outcomes. Required journal persistence fails closed.
Redaction happens before storage/emission. Replay consumes normalized recorded facts
through the live evaluator. Session-dependent mapping/preflight, unavailable evidence
and secret provider state return indeterminate; missing values are not invented.

Replay compares policy decisions (allow/block/ask) separately from recorded human
authorization. Comparison pagination freezes an event high-water mark. Reconnects reuse persisted
event identities rather than copying duplicate history. Exports include only retained
redacted records. Expiration/quota pruning is visible in history.

## Remaining release evidence

The capability matrix is intentionally specific. Cross-platform executions and native Chrome/Edge
extension permission paths still need their matching release evidence. Independent
security and assistive-technology review remain recommended follow-up validation. Automated checks cover loaded-extension capture and
scroll/frame/zoom screenshot coordinates.
The absence of a test is not evidence of protection. See the implementation ledger.
