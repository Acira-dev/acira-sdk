# Signed-out public-site validation — 2026-10-11

The completed campaign attempted **393 distinct website hostnames** using the
installed Acira wheel and real asynchronous Playwright/Chromium. It observed public
HTML on **279 hosts**, loaded public pages under Acira on **242**, and obtained
**193 complete five-case passes**. There were **57 partial** and **143 unavailable**
outcomes. Several hostnames can belong to the same organization.

The original outcomes are retained in [sites.csv](sites.csv). Version information,
source/catalog hashes, category counts and audit totals are in
[summary.json](summary.json). The data does not contain private journals, screenshots,
credentials, browser profiles or machine-specific paths.

| Category | Hosts attempted | Complete | Partial | Unavailable |
| --- | ---: | ---: | ---: | ---: |
| Developer | 133 | 90 | 13 | 30 |
| Science | 57 | 35 | 5 | 17 |
| Education | 70 | 29 | 12 | 29 |
| Culture | 52 | 17 | 11 | 24 |
| News | 30 | 10 | 2 | 18 |
| Product | 51 | 12 | 14 | 25 |
| **Total** | **393** | **193** | **57** | **143** |

## What was tested

A complete pass requires five fresh, empty browser contexts: an unprotected
baseline, the same public link with all four bundled protections, an explicit
click block, a rejected review, and an approved review. Successful guarded clicks
must reach the expected destination with exactly one dispatch. Blocks and
rejections require zero dispatches, no new document request and an unchanged URL.
The test reviewer permits only the selected harmless public link; it does not
alter the SDK's unattended approval default.

A separate read-only cross-check of all **10,215 durable journal events** and
**408 review records** found **zero duplicate dispatches, zero blocked-profile
click dispatches, and zero dispatches associated with rejected reviews**.
Six attempted browser calls had unconfirmed outcomes and remain outside complete
passes. They were not retried automatically.

The durable review records contain **286 available masked captures** and
**122 unavailable captures**. Per-case exports contain slightly fewer image records
because some browser-call errors occur before case enrichment. A navigation pass
does not imply screenshot availability. Privacy checks were not weakened to obtain
an image.

## Environment and boundaries

- SDK 0.1.0; Python 3.12.14; Playwright 1.60.0; Chromium 148.0.7778.96; macOS arm64.
- Two live workers; 20-second action/navigation timeout; fresh empty storage for
  every case; no account credentials, profile imports or form entry.
- An independent public-HTTPS GET/HEAD fence, blocked service workers/WebSockets,
  bounded request counts, explicit redirect handling and offline-before-teardown
  ordering. The same fence is present in the baseline and changes some website
  behavior. Robots checks and document pacing remain enabled.
- No real payment, destructive action, upload, download, account login, CAPTCHA
  bypass, forced click, model API or paid browser service.

This establishes finite signed-out public-content navigation coverage. It does
not establish every operation on every website, authenticated workflow coverage,
browser-use live-site coverage, other browser families or other operating systems.
Critical side effects remain exercised against controlled real-browser fixtures,
not against real accounts or transactions.

## Findings and retained failures

Failures include robots restrictions, certificate/transport errors, HTTP errors,
bot checks, unsuitable public links, consent overlays, detached or changing
targets, and mismatched destinations. They are not converted to successes.

The campaign exposed an ordinal-selector defect in the test driver: a changing
menu could move another link into the selected position. A local real-browser
regression reproduced the wrong destination without Acira and rejection of the
unexpected target during review. The driver now holds the observed DOM element;
it never forces or substitutes a click. The original campaign predates that fix,
and its outcomes remain unchanged. Separate follow-up evidence records the
improved driver rather than replacing these rows.

The completed [follow-up dataset](followup/sites.csv) repeats all 14 partial
workflows with successful baselines, plus Python, MDN and Jest controls. It has
**7 complete, 6 partial and 4 unavailable** outcomes across 17 hosts already in
the original catalog. Podman, BBC, Samsung and USGS passed all five cases in this
separate run; the three controls also passed. These repeats are not additional
distinct hosts and do not replace the primary counts. Remaining results include
hidden/replaced targets, consent overlays, missing selected links, destination
mismatches and transport failures. Live websites vary; repeated success alone
does not establish which timing or server condition changed.

The [follow-up summary](followup/summary.json) records another 625 journal events
and 23 reviews, again with zero duplicate, blocked-profile or rejected-review
dispatches. All product runtime files and policy defaults were unchanged; the
test selector and coverage accounting changed.

An earlier preliminary run is excluded because its cleanup removed interception
while a page was still network-enabled. The corrected campaign began only after
a local negative control reproduced that gap and offline-before-teardown tests
passed. Private diagnostic records remain local.

## Regression and release evidence

All **590 local regression tests passed**, including the new safety/selector/report
fixtures. The **25 sweep fixtures also passed against the installed SDK outside
the checkout**. Product runtime files remain unchanged from revision
`e6c987ad7bed10c4857db652f6a462a68df628e1`. Primary campaign harness revision:
`773910984834ef18db8b9a14fe827795467e0ee0`; improved driver revision:
`92ef76eaa118b8bbf0db6f404bcde4be14dbe0fa`.

GitHub prevented the source revision's CI jobs from starting because of the
account's billing/spending restriction; no steps executed in
[the final code revision's inspected CI run](https://github.com/Acira-dev/acira-sdk/actions/runs/38098135583).
Local results are not presented as a new Windows/Linux CI pass or PyPI publication.

See [the repeatable method](../../public-site-sweep.md) for commands and safety limits.

## Failure investigation and repair rerun

The [separate repair record](repairs/README.md) repeats all 200 originally incomplete
hosts and three controls. It retains 23 complete, 53 partial and 127 unavailable
outcomes, with 20 new complete workflows among those incomplete hosts. The initial
393-host and 17-host follow-up records above remain unchanged. Per-phase wheel and
harness identities, residual limitations and audit checks are included.
