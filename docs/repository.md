# Project layout

The [Acira SDK repository](https://github.com/Acira-dev/acira-sdk) contains the SDK,
optional local dashboard, capture extension, policies, documentation, and tests.
The SDK uses the [MIT license](../LICENSE); bundled font notices are retained with
the dashboard assets.

| Path | Contents |
| --- | --- |
| `acira/` | SDK, CLI, policies, integrations, and packaged capture resources |
| `acira_dashboard/` | Local dashboard and static assets |
| `acira_cdp_proxy/` | Lower-level CDP integration and semantic profiles |
| `tools/` | Capture extension sources and development utilities |
| `tests/` | SDK, browser integration, extension, and lifecycle tests |
| `docs/` | Setup, operation contracts, policies, and maintainer guides |

Use [the setup guide](setup.md) for an existing agent project and
[the build guide](release.md) for a development checkout.

Keep credentials, user configurations, virtual environments, browser profiles,
model weights, runtime databases, screenshots, and local reports outside version
control. Initialized projects keep private runtime/history under `.acira/`.
