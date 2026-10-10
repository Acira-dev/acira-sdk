# Migration: `monitor` removal (PR1)

PR1 collapses the decision model from four values to three. If you have config or skill files
from before this change, Acira will **refuse to load them** with a precise error rather than
silently misbehave. This is intentional — it is a security product and stale policy is a risk.

## What changed

### `monitor` is gone

The decision values are now exactly:

| value | meaning |
|---|---|
| `allow` | permitted **and recorded** (every action is logged by default) |
| `ask`   | pause for a human (HITL) |
| `block` | denied |

`monitor` previously meant "permit but observe". In practice it was indistinguishable from
`allow`, and the audit pipeline (PR2) makes `allow` mean "permitted and recorded" for every
action — so `monitor` is redundant. **Replace every `monitor` with `allow`.**

### Per-skill policy (already in effect)

Decisions live under per-skill blocks keyed by skill id. The old flat `risk_policy` /
`page_risk_policy` maps and the list form of `general_skills` are not supported.

```jsonc
// before (rejected)
"general_skills": ["../skills/general/checkout.json"],
"risk_policy":      { "checkout.card_data_entry": "monitor" },
"page_risk_policy": { "checkout.page_detected": "monitor" }

// after
"general_skills": {
  "acira.general.checkout": {
    "path": "../skills/general/checkout.json",
    "risks": {
      "checkout.page_detected":   "allow",
      "checkout.card_data_entry": "ask"
    }
  }
}
```

### Skill files

Skill JSON stays at schema `acira.general_skill.v3`. Only the per-risk `decisions` doc array
changes — drop `"monitor"`:

```jsonc
"decisions": ["allow", "ask", "block"]   // was ["allow", "monitor", "ask", "block"]
```

## The errors you may see

The migration validator (`acira_cdp_proxy/migration_validator.py`) runs on every config and
skill load and names the offending file:

- `<file>: '<risk>' uses 'monitor', which is no longer a valid decision. Replace with 'allow'.`
- `<file>: 'risk_policy' is no longer supported. Configure decisions per skill under general_skills.<skill-id>.risks instead.`
- `<file>: 'general_skills' must be an object keyed by skill id ... not a list.`
- `<file>: skill schema 'acira.general_skill.v2' is no longer supported. Use 'acira.general_skill.v3'.`

The validator is a launch safety net; it is kept through v1 and removed in v0.2.

## Note on schema versioning

Three independent version namespaces, do not conflate them:

- **Config schema** — `acira.config.v1` (unchanged).
- **Skill schema** — `acira.general_skill.v3` (unchanged; removing a decision *value* does not
  change the file *structure*).
- **Audit event schema** — `schema_version: "1.0"` (new in PR1, pinned;
  see `docs/audit/schema.md`, added in a later PR).
