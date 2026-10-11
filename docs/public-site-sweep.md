# Signed-out public-site compatibility sweep

This opt-in tool tests the installed SDK against a curated catalog of hundreds of
real public websites. It is separate from ordinary CI and the interactive
`acira demo --public-sites` test drive. It uses no model API or paid browser service.

## What one complete result means

Each website uses fresh, empty Chromium contexts for five independent cases:

| Case | Expected evidence |
| --- | --- |
| Baseline without Acira | A visible public content link reaches its expected page. |
| Acira with all four bundled protections | The same link works; exactly one guarded click is dispatched. |
| A policy explicitly blocks clicks | The click is blocked; no dispatch or document request follows. |
| A review is rejected | One rejected review, no dispatch and no document request follows. |
| A review is approved | One approved review, exactly one dispatch and the expected destination. |

The test reviewer approves only the exact selected harmless public link. This is
test instrumentation, not a change to Acira's unattended approval behavior.
Review images use the SDK's existing masking/capture pipeline. Missing images and
their reasons remain in the results; a link-action pass does not prove that an
image was available.

Selection pins the observed DOM element rather than its index in a menu. Menu
reordering cannot silently change the chosen link. Replaced, hidden or changed
targets remain failures; the driver never forces a click or retargets it.

A complete result requires all five cases. A loaded page without an eligible
link is partial coverage. Robots restrictions, bot checks, timeouts, server errors
and login requirements are retained as unavailable results, never counted as
protection passes. A baseline pass followed by an Acira failure needs investigation;
a baseline failure cannot establish an SDK defect. Protection failures stop the
worker for investigation.

Bot challenges and blank loading pages are excluded from the observed-public-page
coverage metric, even when a server returns HTTP 200. They remain visible in the
per-site evidence.

## Safety and limitations

- No imported browser profile, account credentials, cookies or local storage.
- No purchases, transactions, form entry, uploads, downloads, account actions or
  CAPTCHA bypasses. Tests stay on public content links on the selected website.
- An independent network fence permits HTTPS GET/HEAD only, checks URLs, blocks
  service workers and WebSockets, and bounds each context's request count.
- HTTP redirects are fetched without automatic continuation. Permitted document
  redirects become new guarded navigations; unsafe and resource redirects stop.
  This fence changes network behavior and is also present in the baseline.
- Contexts go offline before interception is removed during teardown, so late
  background requests cannot escape the read-only boundary.
- Robots rules are checked before page and selected-link navigation. Document
  requests are paced, declared slower rates are honored or skipped, and concurrency
  is limited to two workers for the recorded campaign.
- A unique catalog entry is a hostname, including documentation subdomains. Do not
  describe several subdomains as independent organizations.

This campaign tests signed-out public-page navigation through asynchronous
Playwright/Chromium. It does not establish authenticated, payment, destructive,
browser-use, every-browser or every-workflow compatibility. Critical side effects
remain covered by controlled, offline real-browser fixtures in the main test suite.

## Run against an installed wheel

Use a separate environment outside the source checkout, so the run cannot
accidentally import source in place of the package under test:

```sh
python -m venv /absolute/path/acira-site-test-env
/absolute/path/acira-site-test-env/bin/python -m pip install "/absolute/path/acira-0.1.0-py3-none-any.whl[playwright]"
/absolute/path/acira-site-test-env/bin/python -m playwright install chromium
```

On Windows, use the environment's `Scripts/python.exe` instead of `bin/python`.
Reuse an installed compatible Chromium by setting `PLAYWRIGHT_BROWSERS_PATH`.

From a separate working directory, first run a small sample:

```sh
/absolute/path/acira-site-test-env/bin/python /absolute/path/acira-sdk/tools/public_site_sweep.py --output ./site-check-smoke --limit 8
```

For the full catalog, run these in two terminals, using the same output location:

```sh
/absolute/path/acira-site-test-env/bin/python /absolute/path/acira-sdk/tools/public_site_sweep.py --output ./site-check --shards 2 --shard 0
/absolute/path/acira-site-test-env/bin/python /absolute/path/acira-sdk/tools/public_site_sweep.py --output ./site-check --shards 2 --shard 1
```

`--headed` shows Chromium. `--resume` skips completed site records, provided the
harness, catalog, versions, timeout and sharding match. Changed test code requires
a new output location. Ordinary Acira policy defaults remain enabled.

Generate the report after completion:

```sh
/absolute/path/acira-site-test-env/bin/python /absolute/path/acira-sdk/tools/public_site_sweep.py --output ./site-check --summary
```

The output includes a filterable `report.html`, `results.csv`, `summary.json`,
per-site JSONL evidence, exact harness/catalog snapshots and environment metadata.
Private project journals, masked review artifacts and temporary user registries
stay beneath the output directory. Do not publish the whole output directory or
serve it as a website; share the report/CSV after inspecting them.

The runner registers only in its isolated test-user directory and does not add
hundreds of test projects to your normal local dashboard.

## Repeatable safety verification

```sh
python -m pytest -q tests/test_public_site_sweep.py
```

Offline real-browser fixtures verify empty initial sessions, ordinary navigation,
no-effect blocking/rejection, durable approvals, masked evidence, permitted
redirects and an unsafe redirect whose destination receives no request.
The ordinary test suite does not contact hundreds of public sites.
