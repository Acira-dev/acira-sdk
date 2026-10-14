# Acira capabilities

Acira adds local policies, human review, and action history to an existing browser
agent. Installation alone does not protect an application: integrate its supported
operations through the SDK.

## What is included

- Project initialization, named agents, local policy folders, and terminal commands.
- Bundled general protections and specific policies captured from website targets.
- Asynchronous Chromium integrations for the declared Playwright and browser-use versions.
- One-attempt reviews with target revalidation, masked images when available, and local history.
- An optional dashboard for projects, agents, reviews, policies, and recorded activity.
- A capture extension with offline JSON export and optional dashboard pairing.
- Optional local demonstrations and provider integrations.

Project files remain the policy authority. File-only enforcement works without the
dashboard; actions requiring review block when no reviewer is available.

## Where to start

- [Install and integrate](setup.md)
- [Framework versions and dependencies](compatibility.md)
- [Policies and capture](specific-policies.md)
- [Protection contracts and limitations](protection.md)
- [Troubleshooting](troubleshooting.md)

A policy decision, an approval, and an observed browser result are separate facts.
An observed click or response does not establish business success. Coverage applies
to the documented adapter boundary, not arbitrary application code or every website.
