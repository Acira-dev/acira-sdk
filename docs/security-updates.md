# Protection and configuration updates

These changes strengthen the existing SDK integration. Use the normal setup
instructions in [setup.md](setup.md); the dashboard and demo remain optional.

## Browser operations

Playwright JavaScript handles and handles obtained through their properties keep
the protected page's ownership. Their script methods use the same guard as Page,
Frame and ElementHandle methods. Creating a read-only handle does not authorize
arbitrary JavaScript through that handle.

Requested dropdown selections are copied before review. The review shows the
requested values, labels or indices for ordinary controls. Sensitive controls are
masked and require an operation-specific review. Changes to the observed option
definitions invalidate the review.

A submission approval covers one dispatch attempt from the reviewed page and
frame. Concurrent matching requests cannot share that attempt. Browser-use binds
the operation to a positively verified document and network frame identity.

Playwright cannot establish dedicated-worker request provenance through its public
request API. Critical `guard.submit(...)` operations therefore require a context
without observed dedicated workers, including workers that have already closed.
Service workers remain blocked in guard-created contexts. A worker appearing during
the operation causes refusal. Ordinary supported browsing remains available; use a
fresh worker-free context for a critical submission. See
[form-submissions.md](form-submissions.md) for the supported request formats.

## Terminal and captured policies

Terminal reviews within a process take turns. The active prompt owns its context,
operation evidence and input until it completes or is canceled. Queued reviews
cannot consume its answer. EOF, including Ctrl-D, blocks even when an older
configuration sets `default_review_decision` to `allow`.

Captured targets have stable unique IDs that survive editing the capture list.
Import rejects duplicate normalized target IDs before writing policy files.
Capture the controls again if an old export contains duplicate IDs; do not choose
a target arbitrarily from an ambiguous file.

Previously approved target mappings are checked against current identity,
destination and safety state. Changes can require a fresh mapping review. Identical
labels on separately verified controls do not merge their policy assignments.
Generic or overlapping Block policies retain precedence.

## Privacy and configuration activation

Recorded URLs mask userinfo and recognized sensitive query/fragment fields. Both
the normal journal and persistent audit use this sanitization. Raw values used for
private runtime comparisons do not become display or export values.

Journal metadata cannot undo masking merely by using names such as `review_id`.
Only declared routing fields and registered generated screenshot artifacts retain
their identifiers. Existing event and review records remain readable.

Secret-provider configuration changes produce a new revision. A replacement broker
must be prepared before the new revision activates. Invalid configuration or a
provider setup failure blocks affected actions until repaired; it cannot silently
leave an old provider behind a newly active revision. Pending reviews and reusable
grants invalidate when the repaired revision activates.

## Legacy dashboard migration

The recommended optional dashboard command remains `acira open`.

The compatibility command `python -m acira_dashboard` now displays a local access
form. Use the access key supplied with `--api-key`, or the generated key printed in
that command's terminal. The unauthenticated page contains no write credential or
project data. Authenticated browser sessions expire after twelve hours. API clients
must include `X-Acira-Dashboard-Token` for reads as well as writes.

Secret environment files written through the compatibility dashboard use private
directories and owner-only files. Symbolic links and multiline values are refused.
Existing files receive these permissions when next saved. Do not commit secret
files or local runtime directories.
