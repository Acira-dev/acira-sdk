# Acira Capture WebExtension

This is the first user-facing capture surface for Acira specific skills. It targets Chrome and Edge with a WebExtensions-style Manifest V3 layout so Firefox support can follow later.

## Use

1. Open `chrome://extensions` or `edge://extensions`.
2. Enable developer mode.
3. Load this folder as an unpacked extension.
4. Open the real logged-in app page you want the agent to use later.
5. Click the Acira Capture extension icon to open the capture panel on that tab.
6. Select a button/link/input/select, then mark it as `Block` or `Ask`.
7. Copy or download the exported JSON skill file.

The panel and overlays are injected only after you click the extension icon. Clicking `Close` or clicking the extension icon again removes the panel and overlays from that tab.

The exported file is declarative data only. It contains URL scope, target fingerprints, decisions, reasons, and capture metadata; it does not contain executable page code.

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
