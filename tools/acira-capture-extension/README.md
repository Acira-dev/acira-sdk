# Acira Capture WebExtension

This is the first user-facing capture surface for Acira specific skills. It targets Chrome and Edge with a WebExtensions-style Manifest V3 layout so Firefox support can follow later.

## Use

1. Open `chrome://extensions` or `edge://extensions`.
2. Enable developer mode.
3. Run `acira extension install`, then load the printed folder as an unpacked extension. Developers working in this repository can load this source folder instead.
4. Open the website page you want the agent to use later. Start testing with a signed-out public link or an inert local fixture.
5. Click the Acira Capture extension icon to open the capture panel on that tab.
6. Select a button/link/input/select, then mark it as `Block` or `Ask`.
7. Copy or download the exported JSON policy file, then run `acira policy import capture.json` inside your initialized project.
8. Run `acira policy configure` to choose the policy and Ask/Block decision in the terminal. Use `--agent NAME` when configuring another agent.

The panel and overlays are injected only after you click the extension icon. Clicking `Close` or clicking the extension icon again removes the panel and overlays from that tab.

The exported file is declarative data only. It contains URL scope, target fingerprints, decisions, reasons, and capture metadata; it does not contain executable page code. The panel captures a selection without activating the website control. Repeated exports and saved drafts retain their policy ID. Identical imports are idempotent; changed captures require explicit replacement. No dashboard is needed for the download/import workflow.

## Optional dashboard connection

Run `acira open`, then open Settings → Pair capture extension. Open this
extension's connection settings, enter the displayed local address and pairing
code, and allow access to the loopback address when Chrome/Edge asks. Choose the
destination project and agent. The capture panel displays that destination before
saving directly to its policy folder. Offline copy/download remains available.

Disconnect in the extension or revoke the paired credential in dashboard Settings.
The extension can list destinations and import captures; it cannot approve agent
actions or retrieve secrets. An identical capture imports only once.

## Capture privacy

Input values are excluded. URL credentials, query strings and fragments are removed
from exported scope and target attributes; quoted inline-handler values are excluded
from intent tokens. Review the resulting policy before sharing it: ordinary page
labels, IDs and paths can still contain application-specific information. Acira does
not claim to recognize every secret that a page embeds in arbitrary text.
