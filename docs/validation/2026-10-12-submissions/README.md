# Submit, Send and Post validation — 2026-10-12

Focused submission checks have passed on the source snapshots recorded below.
An earlier complete SDK diagnostic run recorded **1,156 passed, 2 failed, zero
skipped**. A later source snapshot passed **1,184 cases, zero skipped**, in
783.67 seconds, but its release qualification was withheld after an independent
check exposed a captured-only reusable-grant defect. Both snapshots and the
subsequent checks are described below.
**These earlier and focused results do not replace a successful complete run of the final
frozen source and its installed-wheel matrix.** Check the matching source commit
in [Acira SDK release verification](https://github.com/Acira-dev/acira-sdk/actions)
for its final release status. These results do not establish
coverage of every website or workflow.

For setup and supported request contracts, read the
[form submission guide](../../form-submissions.md). The capture and terminal
workflow is explained in [specific policies](../../specific-policies.md).

## Local verification

| Check | Actual environment | Result |
|---|---|---|
| Complete local browser matrix | macOS arm64, CPython 3.12.14, Playwright 1.63.0, browser-use 0.11.12, cdp-use 1.4.5 | 51 passed, zero skipped |
| Earlier integrated submission snapshot | Native macOS CPython 3.12, Playwright 1.63.0, browser-use 0.11.13, exact release dependency constraints | 94 passed, zero skipped; historical focused evidence |

That earlier 94-case snapshot preceded the final page-review, dialog-generation
and dialog-redaction changes. Its cases include 51 real-browser cases and 43
decision/lifecycle cases in
[submission boundaries](../../../tests/test_submission_boundaries.py),
[submission decisions](../../../tests/test_submission_decisions.py), and
[dialog lifecycle](../../../tests/test_submission_dialog_lifecycle.py).
The two rows overlap; they are independent checks of the same feature on two
supported browser-use versions, not 145 distinct scenarios.


The later complete source run collected 1,158 cases and had two failures:

- Built-in adapter discovery included internal dialog helper modules. The narrow
  registration fix subsequently passed the factory-contract check.
- The specific-only extension capture → terminal import → bound-review case
  timed out during capture. The unchanged six-case capture group, a nine-case
  sequence including the preceding dialogs, and the complete 51-case submission
  sequence subsequently passed without increasing timeouts or adding retries.
  The original full-run failure remains preserved and its cause is unconfirmed.

A separate chained-confirmation fixture also exposed an agent wait: a second
physical modal could open before the first dismissal response completed.
No confirmation or submission was accepted. After the generation-token fix,
four real-browser variants passed: confirm, prompt, ordinary chained confirm and
chained confirm with the first dismissal return held until the second dismissal
entered. Both chained variants recorded two false outcomes and no received POST;
the held variant demonstrated the problematic ordering. These cases overlap the
earlier local matrix. The 19 new pure generation-ordering cases also passed with
zero skips. Passing focused regressions does not erase an earlier failed run or
replace the complete final-source and installed-wheel release qualification.

The later 1,184-case source pass included the dialog-generation and capture
failure-diagnostic changes. It does not erase the two failures in the earlier
1,158-case run, and it does not qualify the subsequent source changes.

Independent review then found that captured-only outbound actions could be
classified as routine for an icon's `aria-label`, an underscore-separated
`send_email` ID, or an input button's `value` label. The policy matched the captured
target, but the separate approval-scope snapshot omitted or normalized that
identity differently. A real-runtime negative control demonstrated creation and
reuse of a routine grant; no browser operation was dispatched by that control.
The fix preserves bounded control identity, shares token normalization and keeps
outbound controls outside routine grants without inferring request payloads.

Six local real-browser cases passed after that fix: Playwright and browser-use,
each with the icon, underscore ID and input-button label. Every case used the
actual Chrome extension capture, JSON download, terminal import/validation and
captured-only policy configuration. Two successive attempts received fresh
reviews; requesting a reusable grant was rejected while the review remained
pending, then the reviewer explicitly rejected the attempt. Each fixed case
recorded zero local POSTs, zero handler attempts, zero affected dispatch records
and no created/used grants.

The same six cases failed against the hash-verified old scope module at the
reviewer's grant-eligibility assertion, before any approval or dispatch. Their
post-failure HTTP/DOM assertions were not reached, so those negative controls are
not represented as complete old-version effect proofs. The passing six-case
receipt and old-module failures remain separate evidence. After the final bounded
cleanup-note refinement, all six cases also passed against the actual integrated
SDK without private module replacements: zero skips, 21.26 seconds.

The private scope regressions recorded 21 passed, zero skipped. A separate
API-key privacy candidate recorded 31 passed, zero skipped, including actual
SQLite/CDP-log persistence and exact string/byte browser-wire preservation for
API-key aliases. The integrated focused pure set recorded 112 passed, zero
skipped. These sets overlap and must not be added together as distinct scenarios.
A subsequent 1,242-case full run was deliberately stopped after 281 passes
when another privacy probe exposed a known API-key form field in review evidence.
That partial run is diagnostic, not release qualification. The fix recognizes
bounded API-key aliases in real field identities, masks their display values and
keeps those operations outside routine grants. Original private values remain
unchanged for live revalidation; ordinary `key` fields remain visible.

The field-evidence regressions recorded 15 passes, zero skips. Two additional
real-browser cases, one per integration, passed using actual extension capture,
terminal import and a specific-only Preview policy. Each form contained five
API-key aliases and ordinary controls. The fixed cases masked every API value,
preserved the trusted raw DOM/CDP values, rejected two fresh reviews and reusable
grants, and recorded no affected dispatch, local handler attempt or POST.
Hash-verified old-module controls failed at the mask assertion before approval;
independent SQL inspection confirmed the old exposure, and teardown observed no
local POST or affected dispatch. These negative controls are retained separately
from the passing repaired cases.

A separate reviewer-projection regression set recorded 34 passes, zero skips.
Custom review callbacks and terminal output receive copied, redacted evidence;
full raw page observations remain private to the runtime. Selected targets and
operation evidence remain available. Reusable-grant records mask secret values
after exact private scope hashing, so redaction does not broaden authorization.
Actual callback, terminal and SQLite checks preserve the original private request
and distinguish different raw scopes even when their redacted displays match.
The final integrated pure set recorded 101 passes, zero skips; it overlaps the
34 reviewer, 15 field, 31 API-key and 21 scope cases above.

Collection and focused passes do not establish completion of the final full
source suite or its native macOS/Linux installed-wheel matrix. The matching
commit and artifact must pass those fresh release gates before publication.

All submitted POST requests went only to test-owned `127.0.0.1` fixtures with
invented recipient and message data. Rejected or unbound operations were checked
against actual received requests and browser effects, rather than only a policy
decision string. Verified journeys include:

- Native form controls, external `form=` associations, direct outbound buttons,
  frames, Page/Locator/Element interactions, keyboard Enter and relevant script/CDP paths.
- Exact request-bound approval dispatching one local POST; rejection, changed
  values/destination/target/method and mismatched payloads dispatching zero.
- Actual Chrome extension capture → downloaded JSON → terminal import and
  validation → rejected and approved request-bound execution. Repeated import
  retained one captured policy. General and captured protections were also
  enabled together.
- Separate core click-review rules preserving general submission requirements.
- Actual browser-use native and non-form Send controls, with known native form
  facts, zero unbound requests, one approved request, and ordinary Preview allowed.
- Native confirm/prompt dismissal without a submission. Multiple CDP attachments
  to the same physical dialog generated one dismissal/audit record, while distinct
  targets and subsequent dialogs remained separate.
- Routine grants failing to authorize critical submissions, target changes
  invalidating authorization, and exact session/page ownership during shutdown.

## Signed-out public documentation checks

Each case used a fresh browser context with initially empty cookies and storage.
Robots checks and a separate GET/HEAD documentation fence limited loading and
navigation. After initial loading, an independent emergency fence aborted every
new request before the attempted guarded submission. A submission-policy pass
required the exact policy/risk evidence, an Acira block, zero dispatch records
and zero new requests; unrelated resolution failures did not count as passes.

| Public page | Read-only navigation control | Native GET submission result |
|---|---|---|
| [Python tutorial](https://docs.python.org/3/tutorial/) | Allowed navigation to the documentation index | Search-input Enter blocked by `acira.general.form_submission` / `submission.outbound_attempt`; zero dispatch records, zero new requests, zero emergency aborts |
| [MDN JavaScript guide](https://developer.mozilla.org/en-US/docs/Web/JavaScript/Guide) | Allowed navigation to MDN Web documentation | No observable native GET control; unavailable for this check |
| [Playwright Python introduction](https://playwright.dev/python/docs/intro) | Allowed navigation to the Playwright API documentation | No observable native GET control; unavailable for this check |

All three pages loaded successfully and permitted ordinary documentation
navigation. Only Python supplied the native GET submission evidence. The other
two are not counted as submission-policy passes. No public form was submitted,
no field was filled, and no account, purchase, payment or model/API workflow was
used.

## Additional signed-out documentation coverage

A separate 18-page probe used the current integrated source with the exact
macOS arm64 CPython 3.12.14 environment: Playwright 1.63.0, browser-use 0.11.13,
cdp-use 1.4.5, Pydantic 2.14.1 and aiohttp 3.14.5. The same reviewed fence and
policy-evidence requirements applied. The earlier three-page receipt was retained
separately.

Eight pages supplied an actual native GET search input whose Enter operation
was blocked with the exact submission policy/risk evidence. Each of those eight
recorded zero dispatched actions, zero new requests and zero emergency aborts.
Four checks remained unknown because background requests reached the emergency
fence before the guarded operation was attempted. Those requests were aborted;
no submission attempt was made in those cases. Five pages had no observable
native GET control, and SQLAlchemy was unavailable because its robots redirect
was outside the agreed loading scope.

| Public page | Read-only navigation control | Native GET check |
|---|---|---|
| [Django](https://docs.djangoproject.com/en/6.1/) | Unknown; selected link did not establish a distinct navigation | Passed: exact-policy block, zero dispatch and requests |
| [Flask](https://flask.palletsprojects.com/en/stable/) | Allowed | Unknown: three background requests aborted before the attempt |
| [Sphinx](https://www.sphinx-doc.org/en/master/) | Allowed | Unknown: one background request aborted before the attempt |
| [pytest](https://docs.pytest.org/en/stable/) | Allowed | Passed: exact-policy block, zero dispatch and requests |
| [Requests](https://requests.readthedocs.io/en/latest/) | Allowed | Unknown: one background request aborted before the attempt |
| [pandas](https://pandas.pydata.org/docs/) | Allowed | Unavailable: no observable native GET control |
| [NumPy](https://numpy.org/doc/stable/) | Allowed | Unavailable: no observable native GET control |
| [SciPy](https://docs.scipy.org/doc/scipy/) | Allowed | Unavailable: no observable native GET control |
| [Jinja](https://jinja.palletsprojects.com/en/stable/) | Allowed | Passed: exact-policy block, zero dispatch and requests |
| [SQLAlchemy](https://docs.sqlalchemy.org/en/20/) | Not attempted | Unavailable: robots redirect outside the agreed scope |
| [Read the Docs](https://docs.readthedocs.com/platform/stable/) | Allowed | Passed: exact-policy block, zero dispatch and requests |
| [Rust book](https://doc.rust-lang.org/book/) | Allowed | Unavailable: no observable native GET control |
| [Click](https://click.palletsprojects.com/en/stable/) | Allowed | Unknown: three background requests aborted before the attempt |
| [Matplotlib](https://matplotlib.org/stable/) | Allowed | Unavailable: no observable native GET control |
| [attrs](https://www.attrs.org/en/stable/) | Allowed | Passed: exact-policy block, zero dispatch and requests |
| [urllib3](https://urllib3.readthedocs.io/en/stable/) | Allowed | Passed: exact-policy block, zero dispatch and requests |
| [SymPy](https://docs.sympy.org/latest/) | Allowed | Passed: exact-policy block, zero dispatch and requests |
| [Pillow](https://pillow.readthedocs.io/en/stable/) | Allowed | Passed: exact-policy block, zero dispatch and requests |

The totals are **8 confirmed submission-policy blocks, 4 unknown and 6
unavailable**, not 18 successful submission tests. Seventeen pages loaded with
HTTP 200; sixteen ordinary documentation navigation controls were confirmed
allowed. All fresh contexts started without cookies. No public field was filled,
no form submission was dispatched, and no account or transaction was used.
These public checks exercise blocking of observed GET search inputs; successful
Send/Post execution and approval remain covered by local test-owned POST
fixtures, not public website submissions.

Across the retained three-page and separate eighteen-page receipts, the total
is **21 public-page checks: 9 confirmed blocks, 4 unknown and 8 unavailable**.
These are signed-out documentation/search checks with independent request fences,
not 21 successful submissions or proof of compatibility with every website.

After integrating all captured-grant, API-field and reviewer-projection privacy
fixes, the same three initial documentation pages were checked again with Playwright 1.63.0 and
browser-use 0.11.13 installed. All three allowed ordinary documentation navigation;
Python again recorded the exact-policy search block with zero requests, dispatches
and emergency aborts. MDN and Playwright again had no observable native GET form.
This repeat is not three additional distinct websites. No public field or form
was submitted.

## Protection boundaries

Native form detection uses actual browser form association, including controls
outside a form associated through `form=`. Direct custom control detection uses
the English identity tokens Submit, Send, Post, Publish, Apply and Finalize,
including their own IDs/labels. It does not establish detection of every custom
or localized outbound control. Unknown native button association is gated
conservatively; an outside-form Preview control with a neutral identity remains
an ordinary action.

All native forms are gated, including GET/search. Verified GET request approval
is unsupported and blocks while `submissions` is enabled. Enter/Return/NumpadEnter
are conservatively gated even outside forms because a key handler can commit an
outbound operation. No key or HTTP fallback turns a rejected action into an
approved one. Native confirm/prompt/beforeunload acceptance is unsupported and
must not inherit approval from a preceding click. A screenshot supplements the
reviewed request contract; it does not prove the request payload or business
outcome.

These are supported-integration action boundaries, not an operating-system
sandbox or a guarantee against application code using unrelated browser
connections. A website can perform JavaScript effects before opening a dialog or
in its cancellation branch; dismissing that dialog cannot undo those effects or
contain arbitrary page-authored scripts. Windows qualification and the complete
fresh source/wheel release matrix are separate gates and are not claimed by this
focused report.
