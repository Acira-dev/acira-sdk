# Captured-policy repair checks — 13 October 2026 UTC

These are focused checks of the **0.1.1 candidate**, not release qualification.
The published 0.1.0 package is not credited with these fixes. Publication still
requires the fresh source/installed-wheel platform matrix, supplemental framework
suites, dependency audit and Acira-owner approval described in
[compatibility](../../compatibility.md).

## Reported live bypass

The SDK recheck report described a captured Archive control causing an unrelated
Delete control on the same page to bypass general Ask or Block. A negative control
using the original guard reproduced the mapping-allow branch and one dispatch in
both cases. The current guard already combines the authoritative general decision
after captured-target mapping; this repair was introduced before these new tests.

The new tests use the actual packaged Chrome extension, terminal import and target
configuration, and both Playwright and browser-use. They cover captured Ask/Block,
exact/broad page scope, general Block/Ask, rejection and fresh bounded approval.
Every browser case exercises Delete first and Archive first. Capturing or mapping
Archive never approves Delete or overrides a general Block.

## Additional reproduced defects repaired

- **Replay attribution:** actual recorded general-policy reviews could replay as a
  missing-target review attributed to captured Archive. Selected-action facts do
  not recreate page mapping. Mapping-dependent candidate decisions now report
  insufficient evidence; independently proven core/general Blocks remain Block.
- **Legacy identity:** a name-only capture passed validation and import, then
  listing and saved-revision reconstruction crashed on a missing `id`. A padded
  general ID also broke source lookup. Project listing, source lookup and replay
  now use the existing validated parser identity. Original JSON is preserved;
  duplicate normalized identities remain rejected.
- **Runtime review:** follow-up testing found that approval lookup still used raw
  IDs. Name-only captures could import successfully and then fail before review;
  padded general IDs also missed the runtime's evidence-requirement lookup.
  Runtime review now uses the same validated identity. Eight new checks cover
  approval/rejection and required-field/request-binding enforcement.
- **Catalog/update previews:** name-only and padded identities also failed catalog
  previews or update checks. These paths now compare validated policy kind and ID.
  A specific capture sharing a bundled general policy's ID is never proposed as
  that general policy; a catalog replacement changing policy kind is rejected.
  Checksums, explicit update acceptance and original JSON remain unchanged.

## Focused results and limits

The earlier affected-policy suite passed 113 tests without skips. After the
runtime follow-up repair, the complete affected-policy suite passed **121 tests,
zero skips**, in 194.25 seconds on native macOS, CPython 3.12, Playwright 1.63.0 and
browser-use 0.11.13. Its original 44 regression cases remain, with eight additional
runtime cases. The real browser checks include 24 extension captures and 48
protected adapter runs. The local browser fixtures observed:

- 48 denied Delete operations and 48 denied Archive operations with zero DOM or
  HTTP effects.
- 16 fresh, explicitly reviewed, bounded local POST operations, each reaching its
  fake endpoint exactly once.
- 64 recorded review requests and responses.

The eight additional runtime cases observed six review pairs, two missing-evidence
blocks before review, and two approved fixture callbacks. These use the public
terminal import/configure commands and `protect()` gateway with a controlled
review backend; they do not claim native browser effects or a manual terminal
prompt demonstration. Terminal input behavior has separate tests.

The complete policy-management module passed **22 tests, zero skips**, including
18 new catalog and dashboard-preview cases. Preserved negative controls reproduced
four runtime failures and twelve management failures before their respective fixes.
These management checks use fake catalog responses and authenticated loopback
requests, without contacting remote catalogs or applying previewed updates.

Only inert localhost fixtures were used: no accounts, payments or public form
submissions. These results establish browser-call and HTTP effects, not business
outcomes. Historical failure and comparison evidence remains preserved. Exact
code/test and log hashes are in [evidence.json](evidence.json).

A separate intermittent native selection-input timeout caused an earlier release
candidate to be withdrawn. Its cause remains unproven. The campaign driver now
stops after one selection attempt, and the shared test driver awaits one native
move/down/up gesture with target revalidation. This is a stricter test contract,
not proof of that historical failure's cause. Fresh browser and complete release
checks remain required; neither retries nor longer selection deadlines qualify
this candidate.

## Repeat locally

Install the development dependencies with the release constraints, then run:

```sh
python -m pytest -q tests/test_captured_general_boundary.py tests/test_policy_decision_precedence.py tests/test_runtime_contracts.py tests/test_terminal_policies.py tests/test_project_workflow.py
python -m pytest -q tests/test_management_contracts.py
```

The complete source and installed-wheel suites remain mandatory release checks.
