# Compatibility and dependency choices

Acira integrates into an existing project. Install the official browser framework
separately, then install `acira` in the same environment. The optional extras are
convenience dependency setup; base Acira does not install a framework or model.

## Release verification scope

The expanded candidate requires native macOS/Linux source, installed-wheel and
clean-setup tests on standard CPython 3.11, 3.12, 3.13, 3.14 and 3.15. Each selected
slot must pass without skipped tests before publication. Windows stays in CI and
is not a supported release platform until its native checks pass. Current local
platform evidence uses ARM64 machines; do not infer native x86 test results from it.
Free-threaded, prerelease and non-CPython interpreters have not been validated.
See the [release status](implementation-status.md) for version-specific evidence.

## Framework versions and operations

`acira doctor` reports the installed framework version, exact tested versions and
verified/missing/unverified status. Adapter attachment rejects an unverified version
before enabling protection. Installation success does not establish action coverage.
Acira never silently upgrades or downgrades an existing browser framework.
Adapter verification describes API coverage; it is not a dependency advisory audit.

The candidate verifies Playwright 1.60.0, 1.61.0, 1.62.0 and 1.63.0, and browser-use 0.11.12 and 0.11.13.
The **0.1.1 release gate** requires the following fresh source and installed-wheel
checks against the same frozen commit and GitHub-built wheel:

| Verification scope | Python and native platform | Framework pair |
|---|---|---|
| Release platform matrix | Standard CPython 3.11–3.15, macOS ARM64 and Linux ARM64 | Playwright 1.63.0 / browser-use 0.11.13 |
| Supplemental complete suites | Standard CPython 3.12, macOS ARM64 | Playwright 1.61.0 / browser-use 0.11.13 |
| Supplemental complete suites | Standard CPython 3.12, macOS ARM64 | Playwright 1.62.0 / browser-use 0.11.13 |
| Retained framework versions, complete suites | Standard CPython 3.12, macOS ARM64 | Playwright 1.60.0 / browser-use 0.11.12 |

These are distinct test scopes. The supplemental rows do not establish a complete
framework-version × Python-version × operating-system test matrix. Earlier results
for withdrawn source revisions do not qualify this candidate. Before publication,
every selected check above must pass without skipped tests; final evidence belongs
to the matching frozen commit and artifact hashes in the
[release status](implementation-status.md).

Both integrations use asynchronous Chromium. See the [operation boundaries](protection.md)
for supported actions. External drops, arbitrary scripts/CDP mutations, storage
mutations, clock instrumentation and virtual credential APIs have no verified operation contract and are
rejected. New browser-use PDF actions remain unsupported and are blocked before
creating a file.

## Safe dependency resolution

Public library dependencies use conservative supported ranges, with security-fixed
lower bounds. Private adapter integration libraries retain exact verified versions.
The release constraints reproduce the actual tested stack independently from normal
project installation. Run `python -m pip check` after installing.

For browser-use, `acira doctor` separately checks the recorded AnyIO and PyPDF
security fix floors and the supported release minimum versions. These offline
checks are dated **2026-10-12** and do not certify the whole environment. The
command shows missing, older and unverified versions with a manual `--dry-run`
repair path. Run a current whole-environment dependency audit before deployment.

As of **2026-10-12**, the checked stable browser-use 0.12/0.13 releases,
including [0.13.11](https://pypi.org/pypi/browser-use/0.13.11/json), pin affected
AnyIO/PyPDF versions in upstream metadata. Those pins also conflict with the fixed stack. Acira does not bypass the
resolver, ignore advisories or change upstream metadata to support them. Use a verified
0.11.12/0.11.13 project environment while upstream corrections remain pending.

Python 3.15 requires the native-wheel-capable aiohttp 3.14.5 and Pydantic 2.14.1 stack.
These minimum versions avoid forcing source builds of incompatible older binaries.
User projects should use ordinary virtual environments and current pip. A different
project's dependency conflict is diagnosed rather than resolved by silently rewriting
its application or environment.


### Optional providers

The base installation does not install Google Cloud or require cloud credentials.
The optional `vertex` provider remains available. The release constraints include
its normally resolved dependencies, including protobuf 6.33.6, so testing and
advisory coverage include every published extra. Offline compatibility checks
import provider modules and round-trip generated messages without creating service
clients or making cloud/model requests. A successful dependency check does not
prove account access or that a paid cloud operation will succeed.
