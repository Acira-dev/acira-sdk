# Acira SDK repository

The canonical repository is https://github.com/Acira-dev/acira-sdk. It is owned by
the Acira GitHub account and is private while the release is prepared. Changing
visibility to public is a separate deliberate release step.

This repository contains the complete tracked project snapshot from
https://github.com/FouadDim/Acira-Browser-Security at commit
`0008fd730d1e8b5309fcc07717d313dd09a32c69`, including SDK integrations, policy
engine, dashboard, capture extension, sample policies, tests and release tooling.
The MIT license and Acira contributor attribution are preserved. The source
repository retains its original Git history and review record; this repository
starts with the reviewed project snapshot. Historical CI links in the implementation
ledger refer to that source repository and are not passes for this repository.

Only tracked portable project files were copied. Per-machine settings, credentials,
virtual environments, model weights, runtime databases, screenshots, local reports
and build outputs are not part of this repository.

GitHub repository ownership and PyPI package ownership are separate. Automatic
publishing requires an explicit PyPI trusted publisher authorizing these fields:

| Field | Value |
| --- | --- |
| PyPI project | `acira` |
| GitHub owner | `Acira-dev` |
| GitHub repository | `acira-sdk` |
| Workflow filename | `release.yml` |
| GitHub environment | `pypi` |

The old publisher for `FouadDim/Acira-Browser-Security` does not authorize this
repository. The release workflow remains manual, defaults to build-only, and
requires passing browser/platform checks, a dependency audit, and verification of
the actual GitHub release approval and branch controls before any upload.
Publishing setup status is recorded in [the release guide](release.md). No passwords
or long-lived package-upload tokens belong in Git.
