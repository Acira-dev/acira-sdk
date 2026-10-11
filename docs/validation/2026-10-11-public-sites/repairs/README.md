# Public-site failure repairs: 2026-10-11

The separate repair campaign repeats all **200 incomplete hosts** from the original
393-host campaign, plus Python, MDN and Jest controls. Original results and the
17-host follow-up are unchanged. These are repeats, not additional distinct hosts.

| Repair result | Hosts |
| --- | ---: |
| Complete five-case public-link workflow | 23 |
| Partial coverage | 53 |
| Unavailable coverage | 127 |
| Total | 203 |

**20 previously incomplete hosts now passed all five cases**: eight formerly
partial and twelve formerly unavailable. All three controls passed again. Four
originally partial hosts became unavailable. Live pages, timing and server responses
vary; a repeat pass does not establish that one particular fix caused recovery.

The [CSV](sites.csv) retains every original and repair outcome. The
[summary](summary.json) contains nonexclusive issue categories, per-case statuses,
phase identities, wheel hashes and audit totals. [The repair guide](../../../public-site-failures.md)
describes the reproducible fixes and the independent network boundary.

## Two recorded phases

The first phase retained **125 completed records**. One worker finished; the other
was stopped after an unresponsive baseline renderer stalled its next unrecorded
host. A continuation with native scan timeouts and a case watchdog recorded the
**remaining 78**. Their host sets do not overlap and together exactly match the
203-host repair catalog. Preliminary diagnostic runs are excluded.

Each phase used a different harness snapshot and a built wheel installed outside
the checkout. Both used Python 3.12.14, Playwright 1.60.0 and Chromium
148.0.7778.96 on macOS arm64, with at most two live-site workers. The summary
identifies each phase separately; it is not one identical-runtime campaign.

The wheel difference is limited to `acira/journal.py`: the final wheel retains
only known screenshot diagnostic enums through redaction. Enforcement is identical.
The earlier phase preserved human-readable reasons but redacted those enums.
The final installed runtime matches all **84 runtime Python files** in source.

The final runner at code revision
`b68807fc842b59860d37d187bab9b8f29dc8a8e4` additionally stops if a timed-out
context cannot be confirmed retired. This two-line fail-closed check was added
after the continuation snapshot. Its real-browser fixture passed both in source
and against the installed wheel; the snapshot hashes remain unchanged.

## Browser effects and durable audit

An independent read-only journal check examined **1,750 events** and **56 reviews**:

- Zero duplicate dispatches.
- Zero blocked-profile click dispatches.
- Zero rejected-review dispatches.
- 57 allowed click dispatches; one uncertain outcome remains recorded as unknown.

All cases start with a new context and zero cookies. A full pass requires a matching
baseline, ordinary protection, explicit block, rejected review and approved review.
Blocked/rejected cases must produce no click dispatch or new document request.
No transactions, account actions, form entry, consent interaction or forced clicks
were performed. Uncertain attempts were not replayed automatically.

42 review images were available and 14 were omitted. The final phase recorded
three unstable-page omissions and two omissions for unbounded sensitive visual
effects. The earlier phase's nine reason enums were not retained; this omission
is visible rather than reconstructed. A link-action pass is not screenshot coverage.

## Remaining baseline-pass differences

The seven hosts whose baseline passed but whose five-case sequence remained partial
were investigated separately:

| Host | Retained evidence |
| --- | --- |
| Cambridge | The selected target was not actionable in the blocked/rejected sessions. Normal and approved sessions passed. |
| IKEA | Normal/approved destinations encountered intercepted transport failures. A new independent five-case run passed. |
| KAIST | The approved session could not load the initial page because its document fetch failed; no click dispatched. |
| Greenpeace | Protected destinations failed. A fresh baseline witness showed that navigation added a query string rejected by the unchanged independent test fence. |
| Edinburgh | The selected target was unavailable in normal/rejected sessions; approved navigation passed. |
| University of Chicago | The normal session rejected an unverifiable target with zero dispatch. A new independent five-case run passed; the original target-resolution gap is retained. |
| Wayfair | Blocked/approved sessions received HTTP 429 before an action. Other sessions passed. Rate limits were respected. |

The three additional diagnostic repeats are in [supplemental.json](supplemental.json).
They do not replace the 203 records or increase any distinct-host count. Observers
logged fixed diagnoses only and did not change route parameters or SDK return values.
IKEA/Chicago recovery does not prove the original timing or transport problem was fixed.

Other gaps include robots/access restrictions, restricted server redirects,
certificate/transport failures, unavailable targets and missing eligible links.
Category totals can overlap. They are not all SDK defects. Do not disable the guard,
accept consent or bypass an access challenge to turn them into passes.

## Regression verification

The complete 600-test suite passed before the final cleanup-failure fixture was added.
The complete final **601-test suite passed in 489.95 seconds** before push.

Against the final wheel outside the checkout, **26 screenshot/privacy fixtures**
and **33 sweep fixtures** passed across focused runs. The two new watchdog checks
also passed against real inert local pages. Negative controls reproduce the old
absolute-fragment selection, missing failure audit evidence and unsafe online
teardown behavior. Native trial expiry produces no click or abandoned browser
future. The continuation's completed worker logs contain no unhandled browser-future
warnings; the earlier interrupted phase's warnings are retained locally.

This evidence covers signed-out public navigation through asynchronous
Playwright/Chromium. It does not establish every action on every site, authenticated
workflows, live browser-use coverage or other browser families. Private journals,
masked images, registries, raw errors and local machine paths remain outside Git.
