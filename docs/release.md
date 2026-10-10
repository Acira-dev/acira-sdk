# Building and publishing the SDK release

The SDK distribution is `acira`, with optional `playwright`, `browser-use`, and
`vertex` extras. The packaged extension has its own manifest version.

## Reproducible local artifacts

```sh
python -m pip install build twine
python -m build --outdir dist
python -m twine check dist/*.whl dist/*.tar.gz
python tools/verify_wheel.py dist/acira-0.1.0-py3-none-any.whl
python tools/prepare_release.py dist
```

The default build creates a source archive, then builds the wheel from that archive.
The assembly tool also produces the unpacked extension ZIP, a SHA-256 checksum file,
and a manifest. It neither uploads files nor invents verification results.
Install the verified wheel into an existing project environment, then use `acira init`.

## GitHub release workflow

`SDK release` is a manually triggered workflow. Its default `publish=none` builds
artifacts and verifies the full test suite and clean-wheel journey on macOS, Windows
and Linux, with Python 3.11 and 3.12. The full suite imports the downloaded wheel
outside the source checkout, and refuses source-package imports. A failed matrix
job prevents publication. Selecting either publishing target also runs a dependency
advisory audit against the installed SDK and both browser integrations; any listed
vulnerability or failed audit prevents upload. Build-only local verification remains
available while dependency fixes are under evaluation.

The 2026-10-10 dependency review upgraded core aiohttp/cryptography/Pillow and the
development tools, and evaluates browser-use 0.13.11. That upstream release still
pins AnyIO 4.12.1 and pypdf 6.16.2, which have current advisory records. Do not bypass
their declared requirements or ignore advisories to claim a clean release. The local
fake-data demo does not exercise PDF parsing or remote model TLS, but this does not
establish the safety of arbitrary downstream workflows. Resolving the upstream pins
and rechecking the actual dependency graph remains a publication gate.

Publishing also checks GitHub's actual environment configuration with
`tools/verify_release_environment.py`. It requires the selected repository-owner
user as the release reviewer, permits that owner's self-review, disables
administrator bypass, and accepts only the exact `main` branch. Missing, unreadable,
or weaker provider controls block both publishing jobs. Build-only verification
does not require publishing environments or PyPI access.

Configure PyPI/TestPyPI trusted publishing for this repository, workflow filename
`release.yml`, and the corresponding `pypi` / `testpypi` GitHub environment. Configure
required reviewers on those environments. Publication access and the package name
must be verified by the account owner; GitHub repository access alone does not
establish PyPI access.

The selected owner is the **Acira PyPI account**. For the first publication, that
account's [Publishing settings](https://pypi.org/manage/account/publishing/) should
contain a pending GitHub publisher with these exact fields:

| Field | Value |
| --- | --- |
| PyPI project name | `acira` |
| GitHub repository owner | `Acira-dev` |
| GitHub repository name | `acira-sdk` |
| Workflow filename | `release.yml` |
| GitHub environment | `pypi` |

A pending publisher does not reserve the package name. If `acira` already belongs
to that account, add the same publisher in the existing project's settings instead.
The Acira GitHub account has administration access to this repository. Configure
the `pypi` environment and its release approval/branch controls before publication;
the reviewed release workflow must be available on `main`. On 2026-10-10, the
authenticated Acira-Dev PyPI account registered this exact pending publisher,
and the superseded `FouadDim/Acira-Browser-Security` publisher was removed after
the new connection was verified. No distribution has been uploaded. GitHub and
PyPI ownership remain separate.
Publishing configuration is not complete until both services acknowledge it.
Account passwords and long-lived upload tokens are not needed in the repository.
TestPyPI is a separate account/project setup using the `testpypi` environment.
See [PyPI's first-publication instructions](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).

During the Acira-owned repository setup on 2026-10-10, GitHub accepted the `pypi`
environment, exact `main` branch rule, and disabled administrator bypass, but
rejected required reviewers with an HTTP 422 plan-availability error. The
[GitHub environment documentation](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)
states that required reviewers on Free, Pro and Team plans are available only for
public repositories. The repository remains private. The release check correctly
blocks publishing in this state; an environment existing by name is not proof of
configured approval. No billing change, public-visibility change, or upload is
performed to work around this limitation.

Select `testpypi` or `pypi` only for an approved release. Publication jobs receive
OIDC permission, download already-tested artifacts, and do not run checkout/build
code. This follows the [Python Packaging User Guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/).

Keep the release artifact checksums and the matching CI run together. Use the
[implementation ledger](implementation-status.md) for outstanding manual platform,
extension, accessibility and pilot evidence. Do not describe a candidate build as a
fully verified release until its required gates have passed.

Chrome/Edge extension store submission and GitHub release publication are separate
account-owner operations. The ZIP works for unpacked installation with developer mode;
it is not a signed/store-published CRX. No signing key is generated or committed.
