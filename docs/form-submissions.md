# Review form submissions, Send and Post actions

Acira's bundled `submissions` policy matches native form submissions and button
controls whose own identity contains Submit, Send, Post, Publish, Apply or
Finalize. The default is **ask**, with a verified single-request contract required
before approval can permit an attempt. Explicit outbound controls are covered
outside forms, including button-role links. Page text and ordinary navigation
links alone do not trigger this policy.

Native submission status comes from actual form association, including controls
associated through HTML's `form` attribute. A known non-submit Preview, Search,
Continue or Save button outside a form does not trigger this policy merely
because the older submit heuristic matches it. When native status is unknown,
mutating button actions are conservatively gated. The starter's explicit outbound
words are English; do not treat it as proof that every custom/localized commit
label or arbitrary page-authored request is recognized.

This protection is conservative: **every native form submission is gated,
including GET/search forms and controls labeled Search or Continue**. Verified
GET approval is not supported, so these GET/search submissions block while the
policy is enabled. Other matched controls need a supported request contract.
Select only the starter protections appropriate to your workflow; do not rewrite
a method or mislabel an operation to bypass review.

## Enable or configure from your terminal

In an initialized project:

```sh
acira policy add submissions
acira policy configure submissions --risk submission.outbound_attempt --decision ask
acira policy validate
```

`acira policy add` also offers it as a numbered choice. In the optional dashboard,
open **Policies**, select the bundled **Submit, Send And Post Protection**, and
save it to the intended project/agent. Both interfaces write the same project
policy files. No dashboard is required for terminal review or enforcement.

New initialization offers this policy with a clear description. Existing policy
files and agent choices are preserved when you upgrade or rerun `acira init`.
Existing projects opt in explicitly with `acira policy add submissions`; adding
the template to Acira's catalog does not automatically add it to their folders.

## Declare the actual request in your existing application

An ordinary `locator.click()` on a matched control lacks the verified request
contract and is blocked with `guard.submit(...)` guidance. A screenshot and button
highlight help locate the control; they cannot prove what data will be sent.

For a supported Playwright request, replace just the final activation in your own
workflow with an explicit contract, after patching Playwright and creating a
context owned by this Acira run:

```python
await guard.submit(
    page.get_by_role("button", name="Send", exact=True),
    destination="https://app.example.test/api/messages",
    method="POST",
    encoding="json",
    values={"recipient_id": "recipient-42", "message": "A test draft"},
    facts={"recipient": "recipient_id"},
)
```

The reserved example URL is a placeholder. Use your application's exact endpoint,
HTTP method, encoding and **complete actual request body**. The `facts` mapping
names fields that really exist in that body; it does not invent an account or
recipient from a website's domain. The policy does not require payment-specific
amount, currency or account fields for an unrelated message/form.

Browser-use uses the same request contract with
`guard.submit(browser_session, node, destination=..., values=..., ...)`.
Keep the existing browser/model setup and obtain the intended current DOM node
through your application's supported integration. See the
[operation contract and ownership requirements](protection.md#operation-contracts).

The current contract supports POST, PUT, PATCH and DELETE with bounded, nonempty
flat JSON or URL-encoded form bodies. GET forms are detected by this conservative
policy but cannot be approved through that current request implementation.
Nested JSON, multipart uploads and unsupported operation/request transports also
lack a verified execution contract. Acira blocks missing or unsupported evidence;
do not change the HTTP method or omit fields just to satisfy an example.

## What approval means

The reviewer sees the operation's destination, method and relevant actual values,
with sensitive data redacted. Approval permits one matching attempt. A changed
payload, destination, target or policy revision invalidates approval. Rejection
produces no guarded browser side effect. Routine reusable grants cannot authorize
these critical operations, including Send/Post controls outside a native form.

When a configured page policy also requires review, Acira first asks for a separately labelled page acknowledgement, then reevaluates the original operation and asks for its fresh request-bound approval. A page acknowledgement never authorizes a later Send or Submit operation, and a page-level denial cannot be overridden by an action review.

A timeout, redirect or ambiguous result never authorizes an automatic retry.
Recorded HTTP/browser completion does not prove that a message was delivered or a
business operation succeeded. Acira protects supported integration calls; it is
not an operating-system sandbox or a promise about arbitrary website JavaScript.

## Keyboard activation

`Enter`, `Return` and `NumpadEnter` on resolved controls are conservatively
treated as potential submissions, including controls outside native forms.
Website JavaScript can use these keys to send or commit data. Browser-use
`SendKeysEvent` activation has the same conservative boundary. With the
`submissions` policy enabled, such attempts are gated even when the precise
native-submit flag is false.

The supported `guard.submit(...)` contract activates a resolved control; it
does not verify an arbitrary keyboard transport. Do not wrap an unknown
keyboard action and assume a button-click contract authorizes it. When your
application has a final control that produces a supported JSON/form request,
use that actual control and complete request contract. Workflows requiring an
unsupported keyboard-only transport remain blocked. Known ordinary button
clicks such as Preview are separate from these keyboard attempts.

## Website confirmation dialogs

Acira's human-review prompt does not authorize a website's native JavaScript
`confirm()`, `prompt()` or before-unload acceptance. Those acceptance operations
have no verified contract and are blocked. Acira attempts to dismiss the exact
owned modal when its detecting page/session is available; it does not accept a
focused tab's unrelated dialog as a fallback. A Playwright `alert()` can be
acknowledged as a notification, not as submission approval.

A page may perform its own JavaScript effects before opening a confirmation or
in its cancellation branch. Dismissal cannot undo an earlier effect or contain
arbitrary page-authored scripts. The request contract applies to the supported
bounded operation, and independent HTTP clients, browser extensions and
unrelated browser connections remain outside Acira's protection boundary.

For testing, use inert local pages and counters first. Inspect signed-out public
forms without sending, posting, buying, creating accounts or accepting a real
confirmation. Capturing a target in the extension selects it for a policy; that
selection is not permission to execute it.
