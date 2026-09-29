# Visual user guide

Read the published guide at **[xperfect.ai/docs/](https://www.xperfect.ai/docs/)**.

This folder is the canonical source. Keep instructions, screenshots and known limits beside the
product they describe. Do not create a second edited copy in the marketing website or a separate
wiki repository.

## Edit and preview

- `index.html`: six user journeys, commands, screenshots and dated verified limits.
- `styles.css` and `guide.js`: responsive light/dark presentation and small browser enhancements.
- `assets/`: only reviewed product screenshots with synthetic tasks and downloadable sample files.

From this folder, run `python3 -m http.server 19990 --bind 127.0.0.1`, then open
`http://127.0.0.1:19990`. Use an unused port if needed. No docs framework or package install is required.

When behavior changes, update the affected instructions and evidence date in the same product
change. A code fix alone does not establish that a known user-path limit is resolved.

## Publish

The existing xPerfect website serves a generated snapshot at `/docs/`. After committing this
folder, run the website repository's `npm run sync:guide -- /path/to/xPerfect`, review its scoped
diff, and deploy the website normally. The sync records the product commit and refuses uncommitted
guide changes. Do not hand-edit the generated `public/docs/` snapshot.

Check the live footer link, chapter links, screenshots, downloads, theme switch and client tabs on
desktop and mobile. This documentation change does not require a product runtime rebuild.

## Provenance and privacy

Instructions now match accepted published package `91f3120583df7cb549e7f81924a4f751089c1c31`
and its immutable `linux/arm64` service/native pair. One-command public startup, the corrected
manual MCP address and all repaired host expert/file/schedule paths have dated actual evidence.
Mac package and real Arm Linux/hosted results are retained on their recorded candidates;
Windows/x86-64 and automatic container client setup remain explicit limits. No new Grok run
is claimed. The earlier hosted diagnostic expert was preserved, not overwritten.

The five PNGs were captured from the xPerfect app on 29 September 2026 at public source `5d9a851`.
They show synthetic onboarding tasks, a generic local-owner account and sample results. They contain
no sign-in credentials, personal account addresses or customer material. The sample skill, CSV and
ZIP are synthetic fixtures covered by this repository's Apache-2.0 license. The demonstration
phrase is not a secret credential. Raw sessions, private reports, state and logs are not published.

## Why the guide lives here

This follows the [Write the Docs principles](https://www.writethedocs.org/guide/writing/docs-principles/)
of keeping documentation near its code, avoiding parallel maintenance, and linking users to one
publication. A separate repo would add coordination without giving this small guide a useful new
boundary. The existing website hosts static files using its current Vercel deployment; no new site,
CMS, service or runtime dependency is needed.
