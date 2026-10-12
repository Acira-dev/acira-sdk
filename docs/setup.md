# Start here: set up Acira in your own project

> The first public release supports macOS and Linux on Python 3.11/3.12.
> Windows instructions are available for contributors, but Windows support
> remains pending its release tests.

This guide is for Fouad and other developers installing the SDK on a new computer.
The normal journey is **get the SDK → install in your agent's environment → initialize
→ integrate → run**. The dashboard and demo are optional.

For a published release, install directly from PyPI in your existing agent
environment, then continue at step 3:

```sh
python -m pip install "acira[playwright]"
python -m pip check
```

Use plain `acira` if you already have the supported browser integration installed.
Check the [Acira PyPI page](https://pypi.org/project/acira/) for available versions;
a pending publisher is not yet an installable release. No PyPI account is needed
to install a published SDK.

The repository stays private during release preparation. Invited collaborators
can follow the source installation below using their own invited GitHub account.
Once the repository becomes public, this same clone and install path remains usable.

## 1. Get the SDK

Have Git and Python 3.11 or newer installed. The current test environments use
Python 3.11 and 3.12. For capture, also have Chrome or Edge available. Playwright's
Chromium installation is a separate step below. Node.js is needed for development
tests, not ordinary SDK use.

In a folder where you keep source checkouts:

```sh
git clone https://github.com/Acira-dev/acira-sdk.git
```

If GitHub says the repository is unavailable, check that your own account has
accepted its invitation and that Git is authenticated as that account. The
repository is private; the error does not mean you need a paid subscription.
If you use GitHub CLI and Git is not yet authenticated, sign in locally and
configure Git's credential helper before retrying the clone:

```sh
gh auth login --hostname github.com
gh auth setup-git
```

Keep this checkout separate from the project containing your browser agent.
Remember its full location, such as `/path/to/acira-sdk` or `C:\src\acira-sdk`.

If you already have a supplied preview wheel, you can instead use the
[wheel installation instructions](quickstart.md#2-install-the-sdk-there) and
continue at step 3. Build output is not committed to Git; cloning does not give
you a `dist` folder or download a published release automatically.

## 2. Install into the environment that runs your agent

Open a terminal in **your existing agent project**, not the SDK checkout. Activate
its existing environment. If you need a new one, these examples use Python 3.12;
use your installed Python 3.11 instead if appropriate.

macOS/Linux:

```sh
cd /path/to/your-agent-project
# Only if this project does not already have an environment:
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install "/path/to/acira-sdk[playwright]"
python -m pip check
```

Windows PowerShell:

```powershell
cd C:\path\to\your-agent-project
# Only if this project does not already have an environment:
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install "C:\src\acira-sdk[playwright]"
python -m pip check
```

Replace both paths with your actual folders. This installs the SDK and its tested
Playwright version from the checkout; it does not modify your application code.
If Playwright is already installed, the base path without `[playwright]` installs
Acira alone. For an existing browser-use agent, choose `[browser-use]` instead and
keep its existing model configuration. Extras can change framework dependencies;
check pip's output and the [supported versions](quickstart.md#2-install-the-sdk-there).

If PowerShell blocks activation, use the environment's Python directly instead
of changing system execution policies:

```powershell
.\.venv\Scripts\python.exe -m pip install "C:\src\acira-sdk[playwright]"
.\.venv\Scripts\python.exe -m acira --help
```

The same `python -m acira` form works for every command below. Install your
application's own dependencies using its existing setup instructions as well.

If the intended Playwright browser build is missing, install it explicitly:

```sh
python -m playwright install chromium
```

On a supported Linux system, if Playwright reports missing browser libraries:

```sh
python -m playwright install --with-deps chromium
```

This may request administrator access to install operating-system dependencies.
Keep the browser sandbox enabled. Reuse a compatible existing browser setup when
available. Playwright requires no model
or paid API; Acira does not download a model during installation.

## 3. Initialize and integrate your existing workflow

Stay in your agent project's folder and run:

```sh
acira init
acira doctor
acira policy validate
```

The wizard asks for the integration, policy folders, starter protections and
optional dashboard connection. It prints the integration snippet. **Add that
snippet to your actual entry point**: initialization by itself does not protect
your application, and Acira does not rewrite it for you.

For Playwright, enter `protect()` and call `guard.patch_playwright(p)` before
launching or connecting to the browser. For browser-use, construct and run the
existing session/agent inside `protect()`. Use the complete examples in the
[existing-project quickstart](quickstart.md#4-add-acira-to-your-existing-entry-point),
[Playwright guide](playwright-quickstart.md), or
[browser-use guide](browser-use-quickstart.md).

Run your application's usual command from this same environment. Use
`headless=False` in your Playwright launch if you want to see Chromium. Acira
protects supported agent actions, not manual human clicks.

## 4. Choose general policies and capture specific ones

These commands work entirely in the terminal:

```sh
acira policy add
acira policy list
acira policy configure
acira extension install
```

The first command offers the general starter protections. The last exports the
bundled capture extension and prints its folder. In `chrome://extensions`, enable
Developer mode and choose **Load unpacked** with that folder. On the intended
website, open **Acira Capture**, select a control, mark it **Ask first** or
**Block target**, and export JSON.

Then, in your agent project:

```sh
acira policy import /path/to/acira-capture.json
acira policy configure
acira policy validate
```

Use the actual JSON path, quoted when it contains spaces. If the website prohibits
Download, choose **Copy JSON**, save the copied text in a JSON file, and import it
the same way. Close capture before letting an agent use that page. The
[terminal-first policy guide](specific-policies.md) covers target selection,
idempotent imports, updates, mapping reviews and configuration without editing
fingerprint JSON manually.

## 5. Run and review, with or without the dashboard

Run your integrated application normally in an interactive terminal. An Ask
policy pauses the guarded operation for review. Check the actual target and
operation details before answering. Rejection blocks the operation; approval
allows one reviewed attempt. If a target mapping review appears first, it only
identifies the control and is separate from approval to execute the action.

For file-only use, select no dashboard during initialization or use
`protect(dashboard=False)`. Without an available reviewer, review-required
operations block; leaving the terminal unattended never approves them.

To use the optional dashboard, open a **second terminal in the same project**,
activate its environment, and run:

```sh
acira status
acira open
```

Keep the first terminal running your agent. The browser dashboard can connect to
opted-in agents that started earlier. One dashboard serves projects and virtual
environments on the same computer and OS account. A run ends when its protection
context exits; opening the dashboard does not run your application for you.

For additional projects, repeat initialization in each project's own folder and
environment. For an additional named agent in one project, use
`acira init --agent research` and `protect(agent="research")`. Policy edits apply
at the next guarded boundary; **Saved** is persisted and **Active** is acknowledged
by the selected run. See [project configuration](project-configuration.md).

## Update and troubleshoot

To update a repository installation, pull the intended source revision in the
SDK checkout, then reinstall from that path in your **agent's environment**:

```sh
python -m pip install --force-reinstall "/path/to/acira-sdk[playwright]"
python -m pip check
acira extension install --replace
```

Reload the existing extension card in Chrome/Edge. Replacing the SDK package does
not automatically replace an extension folder already exported by an older
installation. Current previews share version `0.1.0`, so a version label alone
does not prove you installed the latest build.

| Problem | Check |
| --- | --- |
| `acira` not found | Activate the environment used for installation, or use that environment's `python -m acira`. |
| No project found | Run inside the initialized project, or pass `--project PATH` to the CLI and `protect(project=PATH)` in code. |
| Missing integration/browser | Run `acira doctor`; install the chosen extra or the missing Chromium build in the same environment. |
| No dashboard run | Ensure the application enters `protect()`, is still running, and has dashboard connection enabled. |
| No review request | The action may be allowed or blocked by policy; inspect its decision. A missing request is not proof of protection. |
| Capture cannot connect | Close and reopen capture; check extension reload and permission status. Use offline JSON when dashboard pairing is unavailable. |
| Mapping rejected or outcome unknown | Investigate and repair the capture/workflow in a new run; do not force the same action or automatically replay an uncertain operation. |

Read the [protection boundary](protection.md) before relying on a sensitive
operation. The [validation report](validation/2026-10-12-specific-policies/README.md)
distinguishes complete, partial and unavailable site results. Current Windows,
native toolbar/consent rechecks and formal publication gates remain separate;
setup commands do not establish every-site or transaction protection.

SDK maintainers can use [the release guide](release.md) to build and verify a
distributable wheel. That owner-approved process is separate from installing or
using this preview. Do not run publication workflows merely to try the SDK.
