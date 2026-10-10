# Project configuration and migration

The configuration location is resolved once: explicit project/config path, otherwise
the nearest acira.json walking upward from the current working directory. All relative
policy and secret-reference paths are anchored there. No configuration means setup
guidance, not an inferred project.

## Example

```json
{
  "schema": "acira.project.v1",
  "name": "Orders",
  "dashboard": {"connect": true},
  "agents": {
    "checkout": {
      "adapter": "playwright",
      "policies": {
        "general": "policies/checkout/general",
        "specific": "policies/checkout/specific"
      },
      "disabled_policies": [],
      "decisions": {}
    }
  },
  "retention": {
    "audit_days": 30,
    "image_days": 7,
    "audit_bytes": 1073741824,
    "image_bytes": 262144000
  }
}
```

Use `acira init --agent checkout` to create it. Named agents use separate folders;
deliberately identical paths share files. Agent-specific `decisions` map general policy
IDs to risk-name decisions. `disabled_policies` references installed policy IDs.
Unknown policies/risks and malformed files fail validation. Managed fields cannot
silently disable the project guard.

External policy folders are supported through CLI initialization. Dashboard edits can
reuse registered external locations or add folders inside the project, but cannot
silently add unrelated external write roots.

## General policy extensions

Existing general and specific skill schemas remain importable. The public interface
calls them policies. General policies may add:

```json
{
  "acira_decisions": {"example.risk": "ask"},
  "acira_review": {
    "example.risk": {
      "required_fields": ["recipient", "amount", "currency", "account"],
      "request_binding": true
    }
  }
}
```

These fields augment a valid general policy with declared risks. They are not a
standalone policy. Required fields must be established by a supported operation
contract. Specific captures remain declarative and schema validated.

A catalog manifest has schema `acira.catalog.v1` and a `policies` object keyed by name,
with HTTPS `url`, content `sha256` and optional `version`. Downloads are size/time
bounded and redirects are rejected. Catalog checksums establish correspondence to
the selected manifest; they are not a publisher signature.

## Saves and revisions

All UI/CLI saves use optimistic hashes and atomic replacement. The SDK checks the
current valid snapshot at each action boundary, with background polling for status.
Polling is the current implementation; platform-native file watchers are still a
responsiveness enhancement, not an authorization requirement.

Unsaved → Saved → Applying → Active describes a selected run. Several runs can
acknowledge at different times. An invalidated pending operation is revalidated and
evaluated again, with a fresh review when required. This is bounded to three policy
evaluations within the original five-minute window; it never retries after dispatch. Invalid configuration, conflicts, waiting and failed
application have separate states. Adapter changes require restarting that run.

Revisions contain configuration references and declarative policies, never resolved
secret values. Rollback creates a new revision and uses a recovery marker for multi-file
writes. Interrupted transactions block mutations; `acira doctor --recover` restores
the previous complete snapshot. Policy rollback can affect all agents sharing a file.

## Local identity and access

Project identities map canonical roots in a private OS-user registry; agent names
identify configurations; every run gets a new identity. These IDs are routing metadata,
not credentials. The local service authenticates discovery and negotiates protocol
version 1. Environments sharing that protocol reuse the service. Incompatible versions
produce repair guidance; Acira never silently upgrades an environment.

The default registry location is Application Support/Acira on macOS, LOCALAPPDATA/Acira
on Windows and XDG_STATE_HOME/acira (or ~/.local/state/acira) on Linux. `ACIRA_HOME`
overrides it, useful for tests. Keep descriptors and credentials private.

## Migrating existing configurations

1. Keep your existing configuration and policy files.
2. Initialize the project using their existing general/specific folders.
3. Copy supported guard settings into the selected agent's `guard` object; the project
   manages adapter identity, policy folders and dashboard discovery itself.
4. Point `protect(project=..., agent=...)` at the project, or pass its acira.json to
   `config=`. Legacy config schemas still use the old SDK path.
5. Validate and test your representative workflows before replacing a running setup.

Security-related behavior changes:
- Mutating actions no longer reuse approval merely because the policy decision ID or
  target looks similar. Explicit bounded grants replace that behavior.
- Pending approval is invalidated by policy/target/value changes.
- Unsupported mutation paths fail closed instead of silently relying on selected Page
  wrappers.
- Project-mode audit history is in the durable journal. The legacy event stream is
  also indexed there; a separate legacy persistent archive is an explicit advanced
  configuration with its own storage accounting.
- Existing secret providers remain readable/configurable through their files. The new
  reference editor handles project .env references without accepting secret values.
- Legacy graph/configuration/backtest interfaces remain in the compatibility dashboard.
  The shared workspace has project-aware assignments, raw JSON, revision comparisons
  and diagnostics. Its interactive graph supports layout, inspection, policy copying,
  assignments, shared folders and secret references, with an accessible table for
  the same assignments.

No file migration, browser installation, credential exposure or remote publication is
performed automatically.

Policy and project JSON files are limited to 1 MiB each after formatting. An agent’s
combined policy snapshot is limited to 32 MiB; revision and recovery readers allow
64 MiB for that aggregate plus metadata. Oversized saves are rejected before replacing
files. Revision history and comparison selectors can load older pages.
