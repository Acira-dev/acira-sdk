# Optional: try Acira on real public documentation

This test drive is optional after integrating Acira into your own project. In your
Python 3.11/3.12 environment, install Playwright separately if it is missing, then
add Acira from PyPI:

```sh
python -m pip install playwright==1.63.0
# Only if the matching Chromium build is missing:
python -m playwright install chromium
python -m pip install acira
acira demo --public-sites
```

If the compatible Playwright package and browser are already present, keep them.
The base Acira package provides this optional journey outside the source checkout;
it does not replace your application's entry point or require a cloned SDK repository.

Keep the terminal open. The command opens a test-drive page and the shared local
dashboard. Select Python documentation, MDN's JavaScript guide, or Playwright's
Python documentation. The default demo creates separate projects in Acira's private
user-state directory; it leaves your existing application project alone. Use
`--directory /absolute/separate-folder` for another location.

1. **Browse normally:** Chromium opens the public website and follows a documentation
   link. The result verifies the expected final URL and visible page heading,
   including sites that navigate within the page without a new document request.
2. **Try a review:** Chromium opens the same page but waits before the link click.
   Select **Approvals → Review operation** in Acira for the matching **Public docs**
   project and **review** agent. Inspect the screenshot and highlighted link.
3. **Approve once:** the SDK rechecks and clicks, then verifies the destination.
   **Reject:** the SDK blocks and verifies that Chromium stayed on the initial URL.
4. **Try a blocked click:** the **blocked** agent's policy stops the same link without
   asking. This intentionally demonstrates enforcement on a harmless operation.
5. Inspect the result, then **Agents** and **Activity** in the dashboard. The window
   closes five seconds after the outcome, and the run ends automatically. No manual
   refresh or “finish” step is required. **Cancel run** invalidates a pending review.

The three configurations (**browse**, **review**, **blocked**) have separate policies
within each website project. Existing policy edits are preserved when restarting.
If you change a demo policy, the observed behavior follows the new configuration.
The read-only test-drive network restrictions remain in place independently.

The runner uses isolated Chromium contexts, no saved accounts or cookies, no granted
browser permissions, no accepted downloads, and blocked service workers/WebSockets.
It permits only GET/HEAD HTTPS requests to the chosen documentation host, without
account/payment paths; document navigation stays inside its documentation path and
has no query parameters. Sphinx static CSS/JavaScript may carry one hexadecimal
version parameter so Python documentation renders normally. Other query parameters
and external resources are blocked. It cannot
test purchases, authenticated workflows or arbitrary sites.

These are actual public pages and actual guarded Playwright calls, using no model
or API key. The local-model browser-use journey remains `acira demo --adapter browser-use`.
Public sites can change, time out or restrict automated browsers. Missing links,
unexpected destinations or connectivity failures produce an error, not a success
claim or fallback to an unguarded click. Internet access is required only for this
mode; the existing local demo remains usable offline.

Manual clicks in Chromium, unrelated browser connections and independent HTTP
clients remain outside SDK enforcement. Use the test-drive and dashboard buttons
to test guarded automation. This demonstration establishes behavior on these
workflows; it does not establish universal website coverage.
