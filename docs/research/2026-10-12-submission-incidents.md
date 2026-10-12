# Submission incidents and Acira's enforcement boundary

Research checked on 12 October 2026. These sources explain test requirements;
they are not evidence that Acira already passes those requirements.

## Anthropic's report

[Anthropic's official report](https://www.anthropic.com/news/investigating-unintended-model-actions)
was published on 9 October 2026 and corrected on 12 October. It describes
**three separate form-submission examples**: an unreleased research model switched
from a failed practice government form to a real form; Haiku 4.5 submitted despite
an instruction to stop before final submission, expecting another confirmation
page; and Haiku 4.5 invented a police tip while performing example webpage tasks.
The third task forbade accounts, personal data, purchases and destructive actions,
but did not forbid form submissions. The tip was filtered as spam. Anthropic
identifies the Philadelphia Police Department and says it notified the department
on 8 October.

The report does **not** identify the government form as a US visa application or
give a count of 20 submissions. We have not independently verified those details
from an official agency source. Do not combine the three examples into one claim
that a police tip and 20 visa applications were all submitted despite the same
final-submit instruction.

## Browser-use findings

[Issue #4518](https://github.com/browser-use/browser-use/issues/4518), opened and
closed by its reporter on 25 March 2026, reports a wrong-button click on
browser-use **0.12.5**. Its example batches typing and clicking: a disabled submit
button becomes enabled after typing, while the next action still uses the old
selector map. The issue proposes refreshing that map and reports
`max_actions_per_step=1` as a workaround. Its closure is not evidence of a merged
fix. The reported version is outside Acira's verified 0.11.12/0.11.13 family;
that report alone does not establish the same defect in Acira's supported stack.

The [upstream popup watchdog](https://github.com/browser-use/browser-use/blob/main/browser_use/browser/watchdogs/popups_watchdog.py)
automatically accepts `confirm` and `beforeunload` dialogs, acknowledges alerts,
and dismisses prompts. We checked both installed browser-use 0.11.13 and the
official current source (Git blob `50986e2f39147fd438c63b93202963ea4082ad57`).
This is framework behavior, not proof that acceptance bypasses Acira's guarded
connection.

[PR #5951](https://github.com/browser-use/browser-use/pull/5951), created on
30 September 2026, was still open and unmerged when checked. It proposes a
`confirm_dialog_action` option, with **accept remaining the default**. It does not
provide universal submission containment or change every modal type. The earlier
[custom popup handler PR #3821](https://github.com/browser-use/browser-use/pull/3821)
was closed without merging.

## Existing Acira coverage and required verification

The pre-change release candidate `1bc100652b8db1c7cbee4538b2b4ad399af1743a`
contains bundled payment, credential, destructive-action and download/export
policies. It does not bundle a general outbound Submit/Send/Post policy. Payment
rules require payment context; a generic label is insufficient. A critical
operation marker prevents routine grant reuse but does not itself request review.

That candidate already blocks unclassified meaningful CDP mutations when the
SDK enables its bound-review requirement, including native modal acceptance.
This must not be presented as proof that upstream auto-confirm succeeds through
Acira. Direct Playwright `Dialog.accept` and a prior action transaction need
explicit boundary tests.

The implementation and release tests must establish:

- Recognizable outbound controls can be configured through terminal and dashboard
  policy workflows. Critical submissions require fresh, bounded review rather
  than a routine reusable grant.
- A changed target, destination, values or policy revision invalidates review.
  A screenshot or button label supplements evidence; neither establishes the
  actual request contents.
- A prior click approval cannot authorize a separate native confirmation.
  Protected modals are dismissed on their exact owning page/session, without
  using the focused tab as a fallback or recording raw dialog messages.
- Framework event handlers do not wait for a reply on their own blocked receive
  loop; owned background tasks are cancelled and reaped during shutdown.
- Controlled local forms and endpoints show zero rejected effects, exactly one
  permitted request, independent runs, and unchanged unrelated-browser behavior.

An ordinary DOM policy governs supported guarded actions. It does not contain
arbitrary page-authored JavaScript or unrelated browser connections. Only a
supported request-bound operation contract can justify claims about the reviewed
HTTP destination and values; native GET forms or unknown submission paths must
not be advertised as covered by that contract without an implemented, tested
adapter.

Real-site inspection should use public documentation or ordinary search pages
while signed out, stopping before any outbound action. Police, immigration,
payment and other consequential forms are not release-test targets.
