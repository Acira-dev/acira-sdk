# Building and publishing Acira

Acira's public Python distribution is named `acira`, with optional
Playwright, browser-use, and Vertex extras. GitHub stays private during release
preparation. PyPI publication makes the distributed SDK source public; making the
GitHub repository public later does not change the installation command.

## Two separate actions: verify, then approve

1. Run **SDK release verification** on the exact main commit.
2. After every check passes, the **Acira-dev owner** runs **SDK release** and
   enters the verification run ID, source commit, wheel SHA-256, and source archive
   SHA-256 shown in the verification summary.

The owner explicitly approved this free workflow approval on 2026-10-11 instead
of GitHub's Enterprise-only reviewer feature for this private repository. This is
an owner-controlled release request, not an independent reviewer service. Protect
the Acira account and changes to the release workflows.

The publisher verifies the actual GitHub actor and rerun actor by account ID
**286463772**, the private Acira-owned repository, both workflow identities, the
exact commit, and that approval was submitted after verification finished. The
main commit must still match the approved commit. A moved main branch requires
fresh verification.

All six combinations of **Linux, macOS, Windows × Python 3.11, 3.12** must execute
and pass. Each runs source and installed-wheel suites, dependency consistency,
Chromium tests, the clean installation journey, and dashboard/extension script
checks. Skipped tests are rejected in release jobs. The dependency advisory audit
runs during verification, including a verification that is never published.
Incomplete job evidence, failed/skipped required steps, unavailable artifacts,
wrong checksums, and approvals from collaborators block publication.

After a failed or interrupted verification, submit a new verification request.
The publisher requires every job from the same run attempt; a partial rerun cannot
mix earlier passing jobs with a later attempt's evidence.

The publishing environment still disables administrator bypass and allows only
the exact main branch. Owner approval is enforced by the workflow, replacing the
unavailable paid environment-reviewer checkpoint.

Publication downloads the already tested artifact ID and checks the two approved
hashes again immediately before upload. It does not rebuild, import SDK code, or
check out the repository in the upload job. Only that job receives OIDC upload
permission. The official uv publisher supports macOS self-hosted runners and
requires trusted publishing; no stored PyPI upload token is used.

## Free execution while hosted minutes are exhausted

Keep paid usage disabled. GitHub's included hosted allowance is exhausted for
Acira's account; deleting artifacts does not restore used minutes. Self-hosted
runners on existing computers are free according to
[GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

Both release workflows offer a runner choice:

- **hosted** uses GitHub's free allowance when it is available.
- **self-hosted** requires existing computers or virtual machines running all
  three operating systems. A Mac cannot stand in for the Windows/Linux jobs.

The helper `tools/start_release_runner.py` downloads the official GitHub runner,
verifies its published checksum, and registers one-job ephemeral runners for one
specific run ID and source commit. It installs no service and deletes the
registration and temporary files after completion, including interruption.
A pre-job hook rejects other workflows, commits, run IDs, accounts, and pull
request jobs before job steps execute. Runner labels include the selected run ID.

Requirements on each runner computer: Python 3.12+, Git, GitHub CLI authenticated
as Acira-dev, and Bash (Git for Windows provides it). Linux also needs browser
system dependencies; use a disposable Linux VM with passwordless sudo for
Playwright's official dependency installer.

Use Ubuntu 24.04 or a supported Debian release for the pinned Playwright 1.60.0
Linux tests. Its installer rejects Ubuntu 26.04 ARM64. A newer operating system
does not establish browser compatibility; choose a supported image rather than
overriding Playwright's platform detection or skipping browser tests.

Mac runners need native Python 3.11 and 3.12 already installed. The helper verifies
their actual versions and architecture and prepares private, temporary virtual
environments that setup-python can discover. It avoids the official Mac installer's
privileged `/Users/runner` location; it does not create or modify system folders.

Run verification from the Mac:
```sh
gh workflow run verify-release.yml --repo Acira-dev/acira-sdk --ref main -f runners=self-hosted
gh run list --repo Acira-dev/acira-sdk --workflow verify-release.yml --limit 1
```

Record the resulting run ID and full source commit. Then run the helper from
the SDK checkout on each computer:
```sh
# Mac: authorization, build, two Python versions, audit, final summary
python3 tools/start_release_runner.py --run-id RUN_ID --commit SOURCE_COMMIT --jobs 6

# Linux: both Python versions
python3 tools/start_release_runner.py --run-id RUN_ID --commit SOURCE_COMMIT --jobs 2
```

On Windows 11, use PowerShell:
```powershell
# Install missing prerequisites using their official installers or winget.
# Git for Windows supplies the Bash used by the workflow.
gh auth login --hostname github.com
gh repo clone Acira-dev/acira-sdk
Set-Location acira-sdk
git checkout SOURCE_COMMIT
py -3.12 tools/start_release_runner.py --run-id RUN_ID --commit SOURCE_COMMIT --jobs 2
```

Keep the Windows terminal open until both jobs finish. Account login happens on
that computer; do not copy passwords or tokens into chat or the repository.
The Mac/Linux jobs can proceed concurrently. Do not represent local macOS results
as Windows or Linux verification.

After the successful verification run has completed, publish the exact artifacts:
```sh
gh workflow run release.yml --repo Acira-dev/acira-sdk --ref main \
  -f publish=pypi -f runners=self-hosted \
  -f verification_run=RUN_ID -f source_sha=SOURCE_COMMIT \
  -f wheel_sha256=WHEEL_SHA256 -f sdist_sha256=SOURCE_ARCHIVE_SHA256
```

This is the separate Acira-controlled approval. Get the new publishing run ID
and run the temporary Mac helper for that ID with `--jobs 2`.
A failed verification cannot be approved by this command. No upload occurs until
GitHub and PyPI accept the exact trusted identity and artifact pair.

## Package contents and dependency gates

Local build:
```sh
python -m pip install build twine
python -m build --outdir dist
python -m twine check dist/*.whl dist/*.tar.gz
python tools/verify_wheel.py dist/acira-0.1.0-py3-none-any.whl
python tools/prepare_release.py dist
```

The build produces a source archive, then a wheel from it. Assembly adds the
unpacked extension ZIP, SHA-256 checksums, and a manifest. It does not upload files.

The browser-use extra selects the compatible upstream **0.11.12** distribution,
which permits patched AnyIO **4.15.1** and pypdf **6.20.0**. Its missing
pydantic-settings dependency and tested integration libraries are explicit.
Browser-use 0.13.11 pins affected versions; no upstream metadata override or
advisory exclusion is used. The adapter supports the compatible recovery methods
and a finite, review-aware action timeout.

## PyPI account connection and installation verification

The Acira-Dev PyPI account registered this pending publisher on 2026-10-10:

| Field | Value |
| --- | --- |
| PyPI project | acira |
| GitHub owner | Acira-dev |
| Repository | acira-sdk |
| Workflow | release.yml |
| Environment | pypi |

The workflow filename and environment remain unchanged. A pending publisher does
not reserve a project name and does not create an installable package. See
[PyPI first publication](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).
TestPyPI requires its own publisher and environment.

After an actual successful PyPI upload, verify in a new environment outside the
checkout:
```sh
python -m venv /tmp/acira-public-install
/tmp/acira-public-install/bin/python -m pip install --upgrade pip
/tmp/acira-public-install/bin/python -m pip install acira
/tmp/acira-public-install/bin/python -m pip check
/tmp/acira-public-install/bin/acira --help
# In an existing agent project, install its integration and initialize normally:
/tmp/acira-public-install/bin/python -m pip install "acira[playwright]"
/tmp/acira-public-install/bin/acira init
```

Use equivalent Scripts paths on Windows. Optional integrations require the
matching extra and the developer's existing browser environment.

Keep checksums, verification run, source commit, audit, and publication outcome
together. Until PyPI confirms an upload, use a tested local wheel and describe it
as a candidate. See the [implementation ledger](implementation-status.md).

The capture ZIP remains an unpacked extension, not a store-published CRX.
Extension-store submission and GitHub release publication are separate steps.
