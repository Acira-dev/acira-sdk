# Start here: add Acira to your own project

Acira is available on [PyPI](https://pypi.org/project/acira/). The supported release
platforms are macOS and Linux with standard CPython 3.11–3.15. Windows examples are provided
for contributors; Windows support remains pending its release tests.

The normal journey is **use your browser framework → install Acira → initialize →
integrate → run your own agent**. The dashboard and demo are optional.

Playwright stays a separate package. Acira provides the adapter that checks your
application's supported Playwright actions. If you already have a compatible
Playwright project, install Acira alone; no SDK checkout or supplied wheel is required.

## 1. Open your existing agent project

Use the Python environment that runs your application. Activate it before installing
Acira or running its commands. In macOS/Linux:

~~~sh
cd /path/to/your-agent-project
source .venv/bin/activate
~~~

If this project has no environment yet, create one first with your installed Python
3.11 through 3.15, then activate it:

~~~sh
python3.13 -m venv .venv
source .venv/bin/activate
~~~

Existing Conda, uv and other environments also work. Installation and execution must
use the same interpreter. Node.js is needed for SDK development tests, not normal use.

Windows contributor equivalent:

~~~powershell
cd C:\path\to\your-agent-project
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
~~~

Create the environment only when one does not already exist. If PowerShell blocks
activation, use its Python directly, such as
`.\.venv\Scripts\python.exe -m acira --help`, without changing system execution policies.

## 2. Install Playwright only if your project needs it

**Already using Playwright?** Keep your compatible installation and existing browser
configuration, and continue to step 3. This release is tested with Playwright 1.60.0, 1.61.0, 1.62.0 and 1.63.0. `acira doctor` reports whether
your installed framework version was verified. Unverified versions receive an explicit
error before attachment; Acira never silently changes your framework. See
[compatibility](compatibility.md) before changing an existing environment.

**Starting a Playwright project?** Install the official package separately:

~~~sh
python -m pip install playwright==1.63.0
~~~

Install the matching Chromium build only if it is missing:

~~~sh
python -m playwright install chromium
~~~

For a supported Linux system that reports missing browser libraries:

~~~sh
python -m playwright install --with-deps chromium
~~~

The Linux dependency installer may request administrator access. Keep the browser
sandbox enabled. Reuse a compatible existing browser when available.

Keep your own script, URLs and launch settings. Playwright's
[official getting-started guide](https://playwright.dev/python/docs/intro) explains its
independent setup. Playwright automation requires no model, paid API or cloud account.

For an existing browser-use project, keep its framework and model configuration
instead. Follow the [browser-use integration guide](browser-use-quickstart.md) for
the tested versions and optional dependency setup.

## 3. Install Acira in that same environment

~~~sh
python -m pip install acira
python -m pip check
~~~

The base package includes the CLI, policy engine, starter policies, local dashboard
and Chrome capture extension. It does not install Playwright, Chromium or a model.
No PyPI account or access to the private GitHub repository is needed.

Acira does not automatically change your application code. The next step adds its
protection to the workflow you already run.

## 4. Initialize and integrate your actual entry point

Stay in your agent project's folder:

~~~sh
acira init
acira doctor
acira policy validate
~~~

The wizard asks for the integration, policy folders, starter protections and optional
dashboard connection. It prints the integration snippet. Add it to your existing entry
point; initialization alone does not protect application actions.

The wizard explains its default `submissions` offer before policy selection:
it gates every native form and direct Submit/Send/Post/Publish/Apply/Finalize
control. GET/search forms are blocked because verified GET approval is not
supported yet. Other matched controls require a supported `guard.submit(...)`
request contract. Omit `submissions` if this is broader than your intended
policy; existing initialized projects are preserved. See the
[form submission guide](form-submissions.md) before enabling it.

With `submissions` enabled, Enter/Return/NumpadEnter on resolved controls is
also treated as a possible submission, even outside native forms. Browser-use
key activation has the same conservative limit. The current `guard.submit(...)`
contract verifies a control activation and exact JSON/form request; it cannot
approve an unknown keyboard-only transport. See the
[keyboard activation boundary](form-submissions.md#keyboard-activation).

For Playwright, enter `protect()` and patch before launching or connecting:

~~~python
import asyncio
from acira import protect
from playwright.async_api import async_playwright

async def main():
    async with protect() as guard:
        async with async_playwright() as p:
            guard.patch_playwright(p)
            browser = await p.chromium.launch(headless=False)
            try:
                await run_existing_workflow(browser)
            finally:
                await browser.close()

asyncio.run(main())
~~~

`run_existing_workflow(browser)` stands for your application's existing function.
Keep its actual browser actions, URLs and compatible launch settings, or place your
existing browser code inside the protection context. It is not an Acira demo function.
Use `headless=False` if you want to see the browser.

For browser-use, create and run your existing session/agent inside `protect()`.
Complete examples are in the [existing-project quickstart](quickstart.md),
[Playwright guide](playwright-quickstart.md) and
[browser-use guide](browser-use-quickstart.md).

Acira guards supported SDK actions; manual human clicks and unrelated browser
connections are outside that boundary. Read the [protection contracts](protection.md)
before relying on a sensitive operation.

## 5. Choose general policies and capture specific ones

These commands work entirely in the terminal:

~~~sh
acira policy add
acira policy list
acira policy configure
acira extension install
~~~

The first command offers general starter protections, including `submissions` for
native forms and Submit/Send/Post/Publish/Apply/Finalize controls. Add it explicitly
with `acira policy add submissions`. This conservative policy includes GET/search
forms and requires verified request evidence; a plain final click is blocked when
that evidence is unavailable. Read the [form submission guide](form-submissions.md)
for the supported `guard.submit(...)` integration and its limits.

The last command exports the bundled
capture extension and prints its folder. In `chrome://extensions`, enable Developer
mode and choose **Load unpacked** with that folder.

On the intended website, open **Acira Capture**, select a control, choose **Ask first**
or **Block target**, then export JSON. Import it in your agent project:

~~~sh
acira policy import /path/to/acira-capture.json
acira policy configure
acira policy validate
~~~

Use the actual JSON path, quoted when it contains spaces. If the website prohibits
Download, choose **Copy JSON**, save that text as a JSON file, and import it the same
way. Close capture before letting an agent use that page.

The [terminal-first policy guide](specific-policies.md) covers target selection,
idempotent imports, mapping reviews, and configuration without manually editing
fingerprints.

## 6. Run your application and review its actions

Use your application's usual launch command in this environment. Protection begins
when execution enters `protect()`. An Ask policy pauses the guarded operation for
review; inspect its target and actual operation details before responding.

Rejection blocks the operation. Approval allows one reviewed attempt. A target
mapping review only identifies the control and is separate from permission to act.

For file-only use, select no dashboard during initialization or use
`protect(dashboard=False)`. Terminal review works in an interactive terminal.
Without an available reviewer, review-required actions block; an unattended terminal
never approves them.

## 7. Open the optional dashboard whenever you need it

Keep your application running. Open a second terminal in the same project, activate
its environment, then run:

~~~sh
acira status
acira open
~~~

The dashboard connects to opted-in agents that started earlier. One dashboard serves
different projects and virtual environments on the same computer and OS account.
Opening it does not launch your application; a run ends when its protection context exits.

For another project, initialize in that project's own folder and environment.
For another agent configuration in one project, use `acira init --agent research`
and `protect(agent="research")`.

Policy saves write to the configured project files. **Saved** means persisted;
**Active** means acknowledged by the selected run at its next guarded boundary.
See [project configuration](project-configuration.md).

## Update and troubleshoot

For a PyPI installation:

~~~sh
python -m pip install --upgrade acira
python -m pip check
acira extension install --replace
~~~

Reload the existing extension card in Chrome/Edge after replacing its exported folder.
Updating the SDK does not automatically replace a previously exported extension.

| Problem | Check |
| --- | --- |
| `acira` not found | Activate the installation environment, or use its `python -m acira`. |
| No project found | Run in the initialized project, or pass `--project PATH` and `protect(project=PATH)`. |
| Missing integration/browser | Run `acira doctor`; install Playwright separately or its matching Chromium build in the same environment. |
| No dashboard run | Ensure your application enters `protect()`, is still running, and has dashboard connection enabled. |
| No review request | Inspect the decision; policy may allow or block it. No request is not proof of protection. |
| Capture cannot connect | Reload/reopen the extension and check permissions. Offline JSON works without pairing. |
| Mapping rejected or outcome unknown | Investigate in a new run; do not force or automatically replay an uncertain action. |

The [validation report](validation/2026-10-12-specific-policies/README.md) separates
complete, partial and unavailable website results. Package installation does not
establish protection for every possible browser path.

## Alternative installation methods

The normal setup above installs Playwright and Acira separately. The existing extra
is only a convenience option for users who prefer to install them together:

~~~sh
python -m pip install "acira[playwright]"
~~~

This installs Acira and the official tested Playwright package; it does not install
a custom Playwright build or Chromium. Browser-use and Vertex extras remain available
for their documented integrations.

Invited collaborators can install from the private SDK checkout instead of PyPI:

~~~sh
git clone https://github.com/Acira-dev/acira-sdk.git
# In your agent project's environment, use the actual checkout path:
python -m pip install "/path/to/acira-sdk"
~~~

If cloning fails, confirm that your own GitHub account has accepted its invitation.
If needed, use `gh auth login --hostname github.com` and `gh auth setup-git` locally.
Once GitHub becomes public, the same source-installation method still works.

A supplied wheel also works without copying it into your project:

~~~sh
python -m pip install "/path/to/acira-0.1.1-py3-none-any.whl"
~~~

For source updates, pull the intended SDK revision and reinstall its base path with
`--force-reinstall`, then run `python -m pip check`. Keep the browser framework installed
separately in your agent environment.

## Optional: try the demo after integration

~~~sh
acira demo
~~~

This is a separate, optional test drive using local fake-data pages. It does not
replace your application or become a prerequisite for setup. It requires the supported
Playwright package and its matching Chromium build.

The [Playwright guide](playwright-quickstart.md#optional-run-the-bundled-journey) explains
its review journey. The browser-use guide describes a separate optional local-model
demo; no model download is needed for normal Playwright integration.
