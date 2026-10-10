# Acira General Skills v3

General skills are no-code detection recipes over normalized Acira Core facts.

They are intentionally not plugins. A general skill cannot run JavaScript, Python, imports, shell commands, webhooks, callbacks, network requests, or arbitrary expressions. Core observes the browser, redacts and normalizes facts, evaluates skill rules, and emits risk ids. Project config maps those risks to `allow`, `ask`, or `block` (per-skill, under `general_skills.<skill-id>.risks`). `allow` means permitted and recorded.

## Shape

A skill declares its risks up top (the self-documenting contract), with detection wiring
nested under each risk. Rules and lexicons are the internals below.

```json
{
  "schema": "acira.general_skill.v3",
  "id": "acira.general.example",
  "name": "Example General Skill",
  "version": 2,
  "requires": ["page", "target", "action"],
  "risks": [
    {
      "id": "example.risky_action",
      "context": "action",
      "severity": "high",
      "description": "The agent attempted a risky action such as delete, revoke, or disable.",
      "decisions": ["allow", "ask", "block"],
      "detection": {
        "when": ["risky_action"],
        "reason": "Acira detected a risky action."
      }
    }
  ],
  "lexicons": {
    "dangerous_actions": ["delete", "revoke", "disable"]
  },
  "rules": {
    "risky_action": {
      "token_match": {
        "paths": ["target.identity_tokens", "target.nearby_tokens"],
        "lexicon": "dangerous_actions",
        "min_matches": 1
      }
    }
  }
}
```

Each risk's `context` is `"page"` (fires during page preflight) or `"action"` (fires on a
specific agent action). It controls the fail-secure default: an unconfigured **action** risk
defaults to `ask`; page preflight is opt-in.

## Operators

- Boolean: `all`, `any`, `not`, `rule`
- Existence/equality: `exists`, `missing`, `equals`, `not_equals`, `in`, `not_in`
- Tokens/strings: `contains_any`, `contains_all`, `token_match`, `starts_with`, `ends_with`
- Numbers: `number_gt`, `number_gte`, `number_lt`, `number_lte`, `number_between`
- Collections: `any_where`, `none_where`, `count_where`
- Confidence/session: `confidence_at_least`, `within_recent_actions`

Collection rules operate over Core-owned arrays such as `page.controls`:

```json
{
  "any_where": {
    "path": "page.controls",
    "where": {
      "all": [
        { "equals": { "path": "is_button", "value": true } },
        {
          "token_match": {
            "paths": ["identity_tokens", "nearby_tokens"],
            "lexicon": "dangerous_actions"
          }
        }
      ]
    }
  }
}
```

## Fact Paths

Root-level useful paths include:

- `action.method`, `action.action`, `action.confidence`, `action.read_only`, `action.meaningful`, `action.tokens`
- `target.tag`, `target.type`, `target.role`, `target.kind`, `target.is_button`, `target.is_editable`, `target.is_form_control`, `target.is_submit`
- `target.identity_tokens`, `target.nearby_tokens`, `target.section_tokens`, `target.form_tokens`, `target.attributes.autocomplete`
- `page.url`, `page.hostname`, `page.url_tokens`, `page.title_tokens`, `page.control_tokens`, `page.target_count`, `page.controls`
- `form.tokens`
- `value.present`, `value.length`, `value.classes`
- `session.recent_actions`
- `network.tokens` is reserved for future Core-provided network summaries

Inside `page.controls` collection filters, paths are relative to each control:

- `tag`, `type`, `role`, `kind`, `label`, `id`, `name`, `form_id`, `section`
- `is_button`, `is_editable`, `is_form_control`, `is_submit`, `is_disabled`, `is_hidden`
- `tokens`, `identity_tokens`, `nearby_tokens`, `section_tokens`, `form_tokens`
- `attributes.autocomplete`, `attributes.placeholder`, `attributes.type`, `attributes.role`

## Security Defaults

- Skill files must use `acira.general_skill.v3`.
- Executable-looking keys fail validation recursively.
- Fact paths are allowlisted; arbitrary dotted access is rejected.
- Raw secret-like values are not required in skill files and are redacted from decision metadata.
- Skills emit risk ids only. Project config owns final policy decisions.
- Dangerous JavaScript bypass protection is enforced by Acira Core, not by general skills.

## Authoring Guidance

- Put domain vocabulary in `lexicons`; do not expect Core to know domain-specific risk words.
- Prefer combining a target/action rule with a page/form context rule to reduce false positives.
- Use `suppress_when` for safe previews, documentation, examples, search, filters, and non-mutating helpers.
- Use `any_where` or `count_where` for page-wide controls instead of broad page text alone.
- Use `value.classes` and `value.length` instead of raw input values.
