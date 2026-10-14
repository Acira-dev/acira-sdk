# Maintainer build and release guide

Verification and publication are separate steps. Build from the intended source,
check the complete distributions, and obtain maintainer approval before uploading.
Do not describe incomplete or skipped platform checks as successful verification.

## Development setup

Use a fresh virtual environment. Install Node.js 22 or newer for the extension tests,
then install the Python dependencies and Chromium explicitly:

```sh
python -m pip install -c .github/constraints-release.txt ".[dev,playwright,browser-use,vertex]" build twine
python -m playwright install chromium
python -m pip check
python -m pytest -q
```

Run the full suite on the Python, platform, and framework combinations you intend
to support. Also check an installed wheel from outside the source checkout, with
both browser integrations and all published extras installed. Use the normal pytest
plugin loading and retain failures; do not skip unavailable required checks.
Review the complete dependency environment with a current advisory audit.

## Build and inspect

```sh
python -m build
python -m twine check dist/*
python tools/verify_wheel.py dist/acira-0.1.1-py3-none-any.whl
```

The wheel check creates clean environments and verifies installed assets, CLI setup,
local agents, and dashboard restart behavior. It does not open a browser.
Use the filename matching the version being built. Inspect both the wheel and source
archive for the intended SDK, dashboard/capture assets, policies, tests, documentation,
and license notices. Exclude credentials, runtime state, and private local reports.

Check the dashboard and extension scripts:

```sh
node --check acira_dashboard/static/workspace.js
node --check tools/acira-capture-extension/background.js
node --check tools/acira-capture-extension/content.js
node --check tools/acira-capture-extension/connection.js
node --check tools/acira-capture-extension/selection.js
```

Record the source revision, distribution hashes, dependency versions, and actual test
results for the maintainer reviewing the release. A source test does not replace an
installed-wheel test, and earlier results do not qualify changed artifacts.

## Publish only approved artifacts

After review, upload the same distributions that were checked. Configure publishing
credentials or trusted publishing outside the repository; never commit tokens or
passwords. Follow the official [Python packaging upload guide](https://packaging.python.org/en/latest/tutorials/packaging-projects/#uploading-the-distribution-archives)
and [PyPI trusted publishing documentation](https://docs.pypi.org/trusted-publishers/).

Uploading is an explicit maintainer action, not a side effect of installation,
building, or running tests. If a required check fails, preserve its result and resolve
it before claiming that release scope is complete.
