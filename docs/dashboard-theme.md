# Acira dashboard theme

The SDK's shared local workspace and retained advanced dashboard use the Acira
visual language: Google Sans, Geist Mono for technical details, white cards,
soft borders, dotted page backgrounds, black primary buttons, and solid dark
page headings. Gradient color stays within original Acira brand assets.

The shared tokens and font declarations live in
`acira_dashboard/static/theme.css`. View layouts remain in `workspace.css` and
`styles.css`. Both interfaces load their assets from the installed package;
there are no external font or script requests needed to render the dashboard.

Status has a written label as well as color. Saved, ended, disconnected, and
rejected states are neutral; pending review and application use amber; active
and approved states use green; configuration failures use red. Approval still
means permission for an attempt, not a verified business outcome.

Approval destinations, reviewed values, request contracts, and verified facts
stay visible. Run and revision identities use a disclosure, alongside existing
operation and policy explanations. Screenshots retain the scaled target outline.
Dialogs initially focus Close, and Enter does not approve a newly opened review.

Layouts support narrow screens, keyboard focus, and reduced-motion preferences.
Navigation requested during a view load is queued, so a background refresh cannot
discard the user's latest view selection.
The appearance changes do not alter authorization, reusable-grant scope, policy
activation, runtime controls, or audit behavior.

## Font notices

Unmodified Google Sans and Geist Mono fonts are bundled under SIL OFL 1.1, with
the copyright and full license notices in the package's static directory and
wheel license metadata:

- Google Sans: `GoogleSans-OFL.txt`, from
  <https://github.com/google/fonts/tree/main/ofl/googlesans>.
- Geist Mono: `GeistMono-OFL.txt`, from
  <https://github.com/vercel/geist-font>.

The Acira SDK source remains MIT licensed. The font files retain their own licenses;
the distribution metadata lists both MIT and OFL-1.1.
