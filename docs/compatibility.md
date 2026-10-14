# Compatibility and dependency choices

These instructions follow the [0.1.1 version scope](../README.md#version-scope).

Install the official browser framework separately, then install `acira` in the same
project environment. Optional extras are convenience dependency setup; base Acira
does not install a browser framework or model.

## Python and platform requirements

The package requires Python 3.11 or newer. Use standard CPython and consult your
browser framework's platform prerequisites. Examples for Windows do not imply that
a different browser API, interpreter, or platform has the same protection boundary.
Free-threaded and non-CPython interpreters are outside the declared verification scope.

Both integrations use asynchronous Chromium. Firefox, WebKit, and synchronous
Playwright are not supported. Chromium must be installed explicitly, and its sandbox
must remain enabled. See [protection and platform boundaries](protection.md).

## Framework versions and operations

The Acira 0.1.1 adapter allowlist contains these exact releases:

| Framework | Declared versions |
| --- | --- |
| Playwright | 1.60.0, 1.61.0, 1.62.0, 1.63.0 |
| browser-use | 0.11.12, 0.11.13 |

`acira doctor` reports the installed framework and its verified/missing/unverified
allowlist status. This is an adapter version check, not an audit of the entire
environment or a guarantee of every action's coverage. Attachment rejects an
unverified framework version before enabling protection. Acira does not silently
upgrade or downgrade an existing installation.

Read the [operation contracts](protection.md) before relying on an operation.
External drops, arbitrary scripts/CDP mutations, storage mutations, clock
instrumentation, virtual credential APIs, and browser-use PDF actions have no
supported operation contract and are rejected in protected mode.

## Dependency resolution

Dependencies use supported ranges, while adapter integration dependencies and the
development constraints select specific versions. Run `python -m pip check` after
installing. Review proposed changes before updating an existing agent environment:

```sh
python -m pip install --dry-run "acira[browser-use]"
```

The browser-use extra supplies its settings dependency and the declared integration
stack, including minimum AnyIO and PyPDF versions. `acira doctor` checks recorded
minimums, which do not replace a current whole-environment advisory audit. Do not
bypass the resolver or modify upstream package metadata to force an unsupported
framework version into the project.

For Python 3.15, the declared aiohttp 3.14.5 and Pydantic 2.14.1 minimums avoid
incompatible older binary dependencies. Use current pip in an ordinary virtual
environment; diagnose conflicts without rewriting the user's application.

## Optional providers

Base Acira does not install Google Cloud or require cloud credentials. The optional
`vertex` extra is for projects that need that integration. Installing or checking
provider modules does not establish account access or authorize a cloud/model
request. Existing browser-use agents keep their own model configuration.
