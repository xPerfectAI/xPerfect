# Visual user guide

Read the published guide at **[xperfect.ai/docs/](https://www.xperfect.ai/docs/)**.

This folder is the canonical source. Keep instructions, screenshots and known limits beside the
product they describe. Do not create a second edited copy in the marketing website or a separate
wiki repository.

## Edit and preview

- `index.html`: six user journeys, commands, screenshots and dated verified limits.
- `styles.css` and `guide.js`: website-matched graphite/pearl/taupe colors; system light/dark by default, with an optional system → light → dark cycle. The system choice follows OS changes during the visit.
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

The guide is maintained with the product source. Its dated release evidence records the
published 25 October 2026 release at
`a6e0825b267d9fa4d88244bc0427d16bb7ab6638`. Its default manifest selects the audited
`linux/arm64` service/native pair. Actual anonymous package startup, native Codex/Claude/Grok
work, same-worker reuse, exact file delivery, API/MCP and saved-state recovery have recorded
proof at their stated routes. Hosted TLS/XFS/OIDC with a second member and native API-key
onboarding remain separate prerequisites, not inherited passes.

The recommended five-profession example uses the supported host Claude Code Skill route.
Docker remains an alternative for its offered native worker routes; the guide does not claim
that every professional starter or Skill works on every deployment.

The expert/Finance illustrations are byte-identical publication mirrors of the canonical files
under `docs/assets/screens/`. The local copies under this guide's `assets/` make its portable
preview and existing same-origin website publication work without changing the site CSP. Update
these mirrors only by copying the reviewed canonical files; do not edit a second image source.
They show real synthetic work from 24 October, with dated captions rather than a claim that
later portrait/preview revisions have identical pixels. Earlier local guide screenshots and
stock/Skill fixtures remain marked as connectivity examples, not the professional demonstration.

No raw sessions, prompts, private accounts, customer material, logs or local runtime state are
published. The small Skill, CSV and ZIP are synthetic fixtures under the repository's license;
their demonstration phrase is not a credential. See the [asset owner](../assets/screens/README.md).

## Why the guide lives here

This follows the [Write the Docs principles](https://www.writethedocs.org/guide/writing/docs-principles/)
of keeping documentation near its code, avoiding parallel maintenance, and linking users to one
publication. A separate repo would add coordination without giving this small guide a useful new
boundary. The existing website hosts static files using its current Vercel deployment; no new site,
CMS, service or runtime dependency is needed.
