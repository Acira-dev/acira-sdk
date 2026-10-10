# Specific Skills

This folder is intentionally empty in the production handoff.

Specific skills are captured per customer or application and should not be
committed from local demos. Use the Acira Capture extension or the dashboard
import flow to create real declarative JSON skills for the target application.

After adding a specific skill JSON file, reference it from a config with:

```json
{
  "specific_skills": [
    "../skills/specific/example-skill.json"
  ]
}
```
