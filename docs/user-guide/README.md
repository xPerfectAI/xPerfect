# Visual user guide

Read the published guide at **[xperfect.ai/docs/](https://www.xperfect.ai/docs/)**.

This folder is the canonical source. Keep instructions, screenshots and known limits beside the
product they describe. Do not create a second edited copy in the marketing website or a separate
wiki repository.

## Edit and preview

- `index.html`: six user journeys, commands, screenshots and dated verified limits.
- `styles.css` and `guide.js`: website-matched graphite/pearl/taupe colors; system light/dark by default, with an optional system → light → dark cycle. The system choice follows OS changes during the visit.
- `assets/`: reviewed product screenshots, canonical brand/comparison artwork and downloadable synthetic sample files.
- `compare.html`, `comparison.css`, `comparison.js`: focused landscape comparison. The renderer generates the HTML from `docs/comparison.json`; CSS and the small theme/filter script are maintained here.

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

The guide is maintained with the product source. The current manifest selects matched
`linux/arm64` or `linux/amd64` service/native pairs for the Docker daemon and records the runtime
code source separately. Do not pin this guide to an obsolete package commit or infer that a guide
publication rebuilt the runtime. The live guide records its documentation revision in `/docs/source.json`.

Native package checks and anonymous startup have their recorded scopes. Hosted TLS/XFS/OIDC
member isolation and storage recovery, and one native Codex API-key setup-to-result path,
have separate scoped proof. Internet/cloud account onboarding, other native API-key providers,
GPT-6.1 API access and Windows host startup remain unverified.

The recommended five-profession example uses the supported host Claude Code Skill route.
Docker remains an alternative for its offered native worker routes; the guide does not claim
that every professional starter or Skill works on every deployment.

The expert/Finance illustrations are byte-identical publication mirrors of the canonical files
under `docs/assets/screens/`. The local copies under this guide's `assets/` make its portable
preview and existing same-origin website publication work without changing the site CSP. Update
these mirrors only by copying the reviewed canonical files; do not edit a second image source.
The gallery shows the current 25 October UI; the Finance report/delivery retain their real
24 October synthetic-work scope. Dated result illustrations are not a new provider acceptance run. Unused older UI/QA captures are removed from publication. The stock/Skill fixtures remain
clearly marked connectivity examples, separate from the professional demonstration.

No raw sessions, prompts, private accounts, customer material, logs or local runtime state are
published. The small Skill, CSV and ZIP are synthetic fixtures under the repository's license;
their demonstration phrase is not a credential. See the [asset owner](../assets/screens/README.md).

## Why the guide lives here

This follows the [Write the Docs principles](https://www.writethedocs.org/guide/writing/docs-principles/)
of keeping documentation near its code, avoiding parallel maintenance, and linking users to one
publication. A separate repo would add coordination without giving this small guide a useful new
boundary. The existing website hosts static files using its current Vercel deployment; no new site,
CMS, service or runtime dependency is needed.

## Public updates and comparison copy

Lead with what a user can accomplish, the benefit and a concrete example. Explain unfamiliar
terms for a reader with no project background. Organize related changes around expertise,
knowledge, work, results, automation and ownership. Show current, reviewed screenshots when
the interface or output makes the benefit clearer; retain their actual date and synthetic scope.
Keep engineering details available in a disclosure instead of forcing everyone to read them.

`CHANGELOG.md` is the dated source for the online changelog. Its small Markdown contract uses
`> **Summary:**`, `> **Example:**`, `> **Topics:**`, up to three `- **Benefit:**` lines,
ordinary reviewed screenshot links and `> **Image caption:**`, followed by technical change
bullets. The website parser and saved edition share that contract. Sync new screenshots before
relying on the live feed to show them; the page only renders its mirrored asset list.

The comparison should make strengths easy to see. Use clear examples and positive, specific
capability descriptions. A configured feature is a feature; do not label it Build or Partial
merely because setup is required. Compare current, correctly named product layers against
primary sources. Do not invent missing competitor capabilities, exclusivity or benchmark
results. Keep product operation and qualification details in the capability guide, outside the
main feature matrix. Update the JSON once, then regenerate the HTML, Markdown and SVG with
`python3 scripts/render-capability-comparison.py`; use `--check` to detect drift.
