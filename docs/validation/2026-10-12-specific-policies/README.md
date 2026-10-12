# Specific-policy repairs and validation — 12 October 2026 UTC

This report covers repairs to the packaged capture extension, captured-target
mapping, and the test driver. The final campaigns use one pip-installed preview
wheel and the real packaged Manifest V3 extension. Results below distinguish a
complete journey from a baseline, capture-only check, inaccessible site, or failed
attempt. Earlier failures remain in the data.

The final control-bound campaign attempted **393 distinct catalog hosts**, with
**430 attempts including retests**. **199 hosts completed the full journey**;
**23 remained partial** and **171 were unavailable** under the signed-out,
read-only test conditions. All catalog entries were attempted. Repeated hosts do
not increase coverage. There were 201 complete attempts including follow-ups;
all 201 passed the exact-control and specific-policy journal checks.

The final journals contain 12,808 events and 450 review records. They show **zero
blocked-decision dispatches, zero rejected-action dispatches and zero duplicate
dispatches**. Five browser calls have unconfirmed outcomes and remain unknown,
even where a later fresh journey completed. They are not success claims.

The full source suite passed **736 tests without skips**. The installed runtime
passed **731 tests without skips in each macOS/Linux Python 3.11/3.12 environment**;
the five subsequently added driver regressions are covered by the current source
suite and a 17-test installed-wheel driver run. Current Windows/formal release
verification is still pending.

See [sites.csv](sites.csv) for individual attempts, [summary.json](summary.json)
for phases, artifact identities, control hashes and audit checks, and
[verification.json](verification.json) for completed test and packaging checks.
See [remaining-results.json](remaining-results.json) for the remaining diagnoses
and the five unknown operations. Private browser profiles, raw captures, screenshots and runtime journals stay
local and are not included here.

## Required complete journey

1. Start with a new signed-out context and observe successful navigation through
   one harmless public content link as a baseline.
2. Use the installed extension to capture that exact control. Selection produces
   zero source-page navigation requests. Export twice and verify stable identity.
3. Invoke the installed CLI to import the actual export twice. One specific
   policy exists after the repeated import.
4. Configure its target to Block through the CLI. The supported SDK action has
   zero dispatches and zero navigation requests.
5. Configure the target to Ask. Reject its review; dispatches and navigation
   requests remain zero.
6. Approve one fresh review. Exactly one click dispatch reaches the observed
   expected destination. The same running SDK loads the policy revisions.

The baseline, capture and protected cases must agree on the bounded full DOM
path, control attributes and label. Two controls pointing to the same URL are
not interchangeable. The durable journal must show specific-policy decisions
Block → Ask → Ask and authorization decisions Block → Block → Allow for the
captured target. Target IDs undergo the existing journal redaction before
comparison; redaction is not disabled to obtain a match.

The four general starter protections remain installed. No generic click-block or
click-review override substitutes for the imported captured policy. A simulated
reviewer permits only the selected harmless link and concrete mappings with the
exact recorded label and destination; other requests are rejected. Separately,
a local terminal PTY journey and four real-site terminal journeys type rejection
and approval into genuine prompts. Go, Flask, Sphinx and Python documentation
completed these checks. Where mapping review was required, concrete source, label,
link kind and full destination were checked before approving the mapping; the
Block policy and separate action approval still applied. The initial deliberately
rejected mapping and too-short test-helper deadline are retained, and fresh
follow-ups use sufficient manual-review time without changing SDK defaults.

## Repairs and regressions

- **Captured identity and candidate ranking:** eligible controls rank above
  wrong-kind candidates. A unique unchanged identity can resolve despite a score
  tie only when the recorded page, DOM position, stable attributes, kind, label
  facts and state agree. Mapping establishes identity; it never approves an Ask
  action or overrides Block. Changed or missing evidence retains the ordinary
  resolver and review boundary.
- **Selection isolation:** the original extension could deliver native selection
  input to earlier page listeners. A transparent extension-origin frame now owns
  that input. Selection moves through authenticated Chrome runtime ports rather
  than forgeable page messages. Tab/frame/token routing, malformed sources,
  duplicate clients, disconnects and bounded payloads have regression coverage.
- **Panel interactions:** a host document listener could swallow Mark/Download
  controls. Trusted panel controls are handled once at window capture before
  those listeners, preserving native clipboard/download activation.
- **Recovery:** a missing or disconnected selection frame pauses capture, retains
  an explicit recovery message and never falls back to clicking the page. An
  existing draft remains exportable. Closing/reopening establishes a new channel.
- **Observational readiness:** the campaign no longer uses a trial pointer click
  to decide whether a link is ready. It observes scrolling, stable geometry and
  hit testing, then rechecks the same retained node before authorization.
- **Duplicate destinations:** Jest exposed a driver picking “Get Started” for
  capture and “Docs” for the Block case although both shared a URL. The original
  failure remains recorded. The driver now binds the exact control through all
  phases; a real extension → CLI → SDK fixture reproduces the ambiguity and
  verifies Block/reject with zero effects and approval with one HTTP effect.
- **Cleanup evidence:** stage timings separate capture/export from teardown.
  Uncertain capture cleanup retires its isolated browser and stops the worker.
  A fault-injected real-export fixture verifies that no completed journey is
  claimed after cleanup failure.

Local fake pages exercise five control families, changed identities and values,
page-event interference, rejected and approved reviews, and actual DOM/HTTP
effects. Capture selection failure from extension 0.1.5 and the original Jest
expectation failure are preserved; neither is counted as a protected-action pass.

## Page-restricted export recovery

TechCrunch reproduced an export timeout after a correctly marked target. Its
source response's CSP sandbox directive omitted `allow-downloads`; native Mark
and Download inputs were observed. The page's restriction was left unchanged.
The existing **Copy JSON** control exported the genuine policy through the native
clipboard, twice with stable identity. That JSON then completed terminal import,
Block, rejection and approval with the same control binding and audit checks.
Only this fresh copied-JSON journey is complete; the original failed Download
attempts remain failed. The disposable test profile granted clipboard readback
for this check only. Production extension permissions were not expanded.

GitHub, Getty and Sydney also completed fresh follow-ups after their original
transport, approval-invalidation and detached-element failures. Earlier uncertain
operations were not automatically replayed. A new profile, capture, policy and
review were used for each follow-up.

## Remaining partial results

| Diagnosis | Hosts | Result |
| --- | ---: | --- |
| Capture control changed, moved or became unavailable | 11 | Capture cannot establish the identical control; no export/protection pass claimed. |
| Identical control unavailable in at least one protected case | 5 | Only verified individual cases count; recapture or select a stable visible control in a fresh run. |
| Fresh follow-up source/destination unavailable | 5 | Earlier partial results remain partial; respect access, consent, robots and transport limits. |
| Approved click without a confirmed browser result | 1 | ICRC dispatched once, timed out and recorded unknown; no automatic retry. |
| Different mapping after locale redirect | 1 | Samsung proposed Galaxy S26 Ultra instead of the captured Learn more control; mapping was rejected and the run locked down. |

The Samsung follow-up reproduced its mismatch; later commands in the same locked
run remained blocked and are not additional independent tests. Getty's initial
approval was invalidated before dispatch when operation evidence changed. Its
fresh follow-up completed, but the original rejection remains recorded. The
unknown calls on Bulma, Sydney and CBS retain their original state even though
fresh follow-ups completed; Tokyo remained unavailable and ICRC partial.

No mapping threshold, page-security restriction or consent boundary was weakened
to convert these results into passes. The diagnosis file records every remaining
host and its practical recovery path.

## Limits and remaining checks

Only public content navigation is exercised on real sites. Every case starts
without cookies; requests are limited by robots rules and an independent
GET/HEAD fence. Account/login, transactions, uploads, download links, WebSockets,
challenge bypass and arbitrary remote browser control are excluded. A blocked
request can make a site unavailable under this test boundary even if it works
in ordinary browsing. External failures and changed/occluded controls remain
partial or unavailable, not passes.

Screenshots are supplementary evidence. The SDK may omit them when masking or
page stability cannot be established; it does not persist an unsafe image to
make a test pass. An allowed action, a dispatch, a completed browser call and a
verified destination are separate recorded facts. Unconfirmed calls remain
unknown in the audit counts. A replaced target, changed operation evidence or
policy revision invalidates an approval before dispatch. That fail-closed outcome
is retained as incomplete; no uncertain approved operation is automatically retried.

Automated extension tests narrowly pregrant selected origins in fresh Chromium
profiles; they do not establish native Chrome toolbar/consent UX for every
managed-browser policy. The production manifest retains its existing narrow
permissions and does not require the Chrome debugger API. Native setup evidence
is recorded separately in verification.json.

The wheel is a local preview, not a public PyPI release. Its 123 shipped runtime
files match runtime commit `c6c93e9`. The source archive includes diagnostic/docs
working changes from build time; it is not an exact final-HEAD release artifact. Current Windows checks,
formal exact-source release verification, dependency audit and the Acira owner's
approval remain publication gates. Earlier successful matrices are not results
for this revision. Use the [terminal-first setup](../../specific-policies.md) to
install the preview, reload the exported extension and import a capture into an
existing project.
