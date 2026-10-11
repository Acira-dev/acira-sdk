# Specific-policy validation — 11 October 2026 UTC

The signed-out campaign exercised the packaged Chrome capture extension, terminal
import/configuration, and the installed Playwright SDK. It ran for about **71
minutes of recorded site cases**, over several bounded campaigns, with **93
attempts across 56 distinct hosts**. **22 distinct hosts completed the full
specific-policy workflow** at least once. Repeated hosts are not new coverage.

This supplements the earlier general-policy campaign; it does not establish
protection on every website, authenticated workflows, or actual transactions.
See [sites.csv](sites.csv) for every attempt and [summary.json](summary.json) for
counts, timestamps, harness/catalog hashes, and wheel identities.

## What a complete case establishes

1. A fresh context with zero initial cookies opens public content and follows a
   harmless link successfully as a baseline.
2. The real packaged extension captures that same control without activating it.
3. Terminal import of the exported capture succeeds; importing it again does not
   duplicate the policy.
4. `acira policy configure` changes that captured target to Block. Its guarded
   click causes no dispatch or navigation.
5. Configuration changes to Ask. Rejection causes no dispatch or navigation.
6. Approval permits one guarded attempt at that same public link, with a recorded
   browser outcome. Policy changes apply to the same running SDK instance.

These projects also installed the four general starter protections. The specific
cases did not substitute broad generic click rules for the captured policy.
The test reviewer approved only the chosen public link and, where necessary,
explicit mappings with matching concrete destination/label evidence. Uncertain
mappings were rejected, never bypassed to obtain a pass.

## Results, including original failures

| Campaign | Extension | Attempts | Complete | Partial | Unavailable |
|---|---|---:|---:|---:|---:|
| Initial campaign | 0.1.3 | 52 | 18 | 10 | 24 |
| Label follow-up | 0.1.4 | 6 | 1 | 1 | 4 |
| Expanded label follow-up | 0.1.4 | 12 | 3 | 1 | 8 |
| SPA capture follow-up | 0.1.5 | 14 | 2 | 2 | 10 |
| Vite diagnostic | 0.1.5 | 1 | 0 | 1 | 0 |
| Readiness-driver follow-up | 0.1.5 | 8 | 5 | 2 | 1 |

The final follow-up completed Python.org, Python documentation, Flask, Go, and
Sphinx. Bulma and Webpack retained ambiguous mapping failures and dispatched no
guarded clicks. Vite's baseline destination timed out in that follow-up; its
complete live workflow remains unverified.

Unavailable includes robots restrictions, transport timeouts, challenges, and
content/navigation that cannot be tested under this campaign's read-only fence.
A baseline success followed by an unavailable or uncertain protected step is
partial, not a protection pass.

### Repairs found through this work

- **Specific-only page mapping:** preflight no longer depends on a general
  page-level policy being installed. The shared authorization boundary receives
  fresh trusted page observations.
- **Audit size:** full-page candidate arrays remain runtime-only. Durable events
  retain candidate counts and selected mapping evidence without exceeding the
  existing record-size bound.
- **Labels:** link fingerprints use their own text/accessible label rather than
  borrowing a preceding control's label.
- **Capture side effects and SPA scope:** window capture listeners intercept
  selection before ordinary document listeners. The source scope is frozen at
  the first target; route changes cannot silently rewrite an exported draft.
  Changed-page selection/addition is rejected.
- **Test isolation:** extension panel controls are selected within that panel,
  avoiding collisions with website buttons called Download.
- **Readiness checks:** trial scrolling can trigger a site's route change before
  a click. The test driver now verifies the source after trial readiness and
  declines the action if it changed. A real-browser fixture reproduces the route
  change with zero clicks.

The initial Vite case exposed **one dispatched click in a test expecting Block**:
the old capture had incorrectly moved its scope to the destination page, leaving
the original source outside the policy. The initial campaign stopped immediately.
This failure remains in the CSV. The SPA fixes address the capture, and offline
tests reproduce the event ordering and verify unchanged scope with no selection
side effect.

The later Vite diagnostic verified both Block and rejected Ask, then stopped when
its approval case dispatched without the expected review. Recorded authorization
facts showed that the source had already changed to `/guide/`, outside the captured
homepage scope. The readiness-driver fix prevents that test from claiming it is
still exercising the original page. This diagnostic remains a failed evidence
check; it is not recast as a complete live pass.

## Durable evidence checks

Read-only inspection of the private journals found:

- 2,202 events and 95 reviews.
- Zero duplicate dispatches for a recorded operation.
- Zero dispatches for an operation whose recorded SDK decision was Block.
- Zero dispatches associated with a rejected click review in these serial workers.
- Seven dispatch attempts without a recorded browser-call completion. These remain
  unconfirmed; permission to dispatch is not proof of a completed outcome.
- 81 reviews with a ready screenshot artifact and 14 with unavailable evidence.

The zero Block-decision count does **not** erase the initial out-of-scope capture
failure described above. Journals, images, profiles, credentials, and full captures
stay local; only bounded aggregate results and public URLs are committed.

## Offline, packaging, and platform checks

Real-browser fixtures cover five control shapes, selection side effects, SPA
capture, stable export identities, duplicate import/save, terminal Ask/Block and
assignments, dashboard configuration, review rejection/approval, and independent
general/specific page mapping. Tests observe browser effects, not only decisions.

The full source and installed suites passed on macOS and native Linux with Python
3.11 and 3.12 before the final SPA capture refinement: 703 tests per run, no skips.
The final extension's focused 11 checks also passed on both Linux environments and
macOS. Final package source testing on macOS 3.12 and installed testing on macOS
3.11 passed 703 tests; the additional readiness fixture brings the current suite
to 704 tests. The final macOS Python 3.12 source suite passed **704 tests in
598.08 seconds**; the final installed-wheel suite passed **704 tests in 617.87
seconds**, with no skips. The final macOS Python 3.11 installed suite passed
**703 tests in 596.06 seconds** before that diagnostic fixture was added.

The final preview wheel was built through its source archive, installed outside
the checkout, and checked with the clean-wheel journey. Checks passed for imports,
file-only enforcement, packaged extension assets, late dashboard connection, one
shared service, independent policy revisions, restart/reconnection, and stable
event identities. Dependency consistency and distribution validation passed.
An external advisory audit found zero known vulnerabilities among 115 third-party
packages; this is not an audit of Acira's own security.

Final runtime artifact, built from `afebbc0` (later commits change diagnostic tools
and evidence, not the SDK runtime):

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `acira-0.1.0-py3-none-any.whl` | 844,511 | `8ee58f8910f9dfb6056988605463e9a06cb35c9405b5e4c41273aa47fa043a41` |
| `acira-0.1.0.tar.gz` | 1,009,748 | `3fd07ad797fc40f1f24d86204acbc63224d4911eca870b4fb8d5135b1eef7539` |

## Boundaries and remaining release checks

- Automated extension tests grant only their selected fixture/site origins. The
  production manifest remains activeTab-based, with optional loopback access and
  no debugger permission. Native Chrome toolbar/permission setup could not be
  completed while the Mac was locked; that manual check remains pending.
- Capturing a control is not a sandbox against arbitrary website JavaScript. Real
  campaigns use an independent network fence: no accounts, form submissions,
  purchases, uploads, or downloaded website content.
- Ambiguous targets require an explicit mapping review or a repaired capture.
  No mapping thresholds or fail-closed rules were lowered for these results.
- Windows checks are still pending. Formal source/installed release gates on all
  three operating systems and both Python versions, plus the Acira-owner publish
  approval, remain required. Local results do not replace those gates.
- This is an installable preview wheel, not a published PyPI release. Public
  `pip install acira` must not be advertised until publication and a clean public
  installation have succeeded. No paid services or subscriptions were used.

Follow the [terminal policy guide](../../specific-policies.md) and
[existing-project setup](../../quickstart.md) to try the supplied wheel.
