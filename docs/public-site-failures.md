# Public-site failure investigation and repairs

The original 393-host dataset and 17-host follow-up remain unchanged. A failure is
evidence to investigate, not permission to disable protections or force a click.
The separate repair campaign repeats all 200 originally incomplete hosts plus
Python, MDN and Jest controls using freshly built, installed SDK wheels. The
repair evidence distinguishes the initial phase from the continuation after an
unresponsive baseline renderer stalled a worker. Repeated hosts remain separate
from the original distinct-host count.

## Reproducible issues corrected

| Issue | Correction and verification |
| --- | --- |
| A changing menu moves another link into an ordinal selector. | Retain the exact observed DOM element. The earlier repair and its real-browser regression remain in place. |
| A visible menu link is covered by an overlay or hidden during hydration. | Bounded native trial checks readiness without a click; rescan before selection, never after approval. The fixture contains a covered duplicate and confirms zero click events while choosing the visible content link. |
| Absolute fragment links are treated as public-page navigation. | Compare document identities, excluding same-document jumps regardless of href spelling. The regression confirms an absolute fragment is skipped. |
| Initial HTML has not yet hydrated its public links. | A bounded preselection rescan waits briefly for content; a delayed-link fixture verifies that selection performs no click. |
| A client router maps a link's href to another public path. | Record the baseline landing and require protected runs to match it exactly. Fixtures cover matching and divergent client routes; the divergent route remains a failure. The network allowlist is unchanged. |
| Exceptions or early returns omit dispatch/review evidence. | Collect evidence in finalization. A real browser fixture performs a guarded approved click and then throws; its failure retains the dispatch, approved review and final URL. |
| Handler removal races in-flight response callbacks. | Go offline, stop new test fetches, drain/cancel bounded callbacks, remove handlers and destroy the context. The existing negative control proves that the old online teardown window could send a POST to an inert local fixture; the fixed path sends none. |
| External cancellation of a native trial leaves an unresolved Playwright future. | Use native timeout options and a shared scan deadline. An obstructed-link fixture verifies zero clicks and zero unhandled-future reports after cleanup. |
| An unresponsive renderer blocks the rest of a worker's catalog. | A per-case watchdog retires the isolated context and records the timeout. A stalled inert renderer fixture verifies that the following case still works. An attempt with an uncertain result is never retried. |
| Every missing screenshot gets the same vague message. | The SDK emits fixed reason codes for unavailable geometry/targets, unstable pages, scan/image limits, timeout and unbounded sensitive visual effects. Tests check reason accuracy and ensure browser exceptions containing fake secrets never leave the runtime. |

Readiness follows Playwright's native [actionability checks](https://playwright.dev/python/docs/actionability).
The trial uses fixed options and no modifiers. It does not authorize a click.
All actual tested actions still enter Acira's guard. No screenshot masking limit,
approval binding, timeout/block default or starter protection was relaxed.

Screenshot diagnostics add `reason_code` alongside the existing `capture_status`
and human-readable `reason`. Existing readers can continue using those fields.
Images omitted for privacy remain omitted. This change does not make screenshots
available on every page.

## Remaining external and coverage limitations

| Evidence | Behavior |
| --- | --- |
| Robots denies access, requires a longer crawl interval, returns an access error, or cannot be retrieved securely. | Retain unavailable coverage. Keep pacing, certificate verification and access checks enabled. |
| HTTP 403/429, CAPTCHA, bot challenge, certificate/DNS/transport failure. | Respect the site response. No identity spoofing, challenge bypass or authenticated profile is used. |
| Consent dialog covers every eligible link. | Retain unavailable action coverage. Consent requires a separate human decision; the runner does not accept or dismiss it automatically. |
| No eligible public same-host content link, or a link needs a login, download, new tab or account action. | Retain partial/unavailable coverage; this is not proof of an SDK defect. |
| A server redirect leaves the selected document scope or resources require redirected/non-read methods. | The independent test fence can alter website behavior. Retain that limitation; do not weaken the URL/method boundary to raise pass counts. |
| A reviewed target or required values change. | Obtain a fresh operation and approval. Never retarget or replay the approved attempt. |
| A dispatched browser call has an uncertain result. | Retain an unknown outcome. Never automatically retry or label authorization as business success. |

The automated approval review rejected an attempted broader same-host redirect
change because it could expand permitted destinations. That change was not applied.
The completed approach preserves the fence and supports observed client routes
through reference comparison only; restricted HTTP redirects remain limitations.

Successful repeats do not prove that a specific code change caused a live site to
recover. Server, network and page timing vary. Original counts are not replaced,
and repeated hostnames are not counted as additional sites.

This work covers signed-out public content through asynchronous Playwright and
Chromium. It does not establish every action on every website, real transactions,
authenticated workflows, browser-use live-site coverage or other browser families.
