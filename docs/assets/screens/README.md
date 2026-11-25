# Product screenshots

## Retained professional examples

These assets show actual xPerfect work with public synthetic material. They are used by the
README, quickstart, workshop and visual guide; keep those pages on the same image files.

| Asset | What it shows | Capture scope |
| --- | --- | --- |
| `expert-team.jpg` | The five professional starting workers and the Create card with current profession artwork. | Actual expert gallery, 25 October 2026, public main runtime. Kept for the 25 October changelog entry. |
| `expert-gallery.jpg` | The same gallery in Run Project, with the Experts tab in the bar. | Actual expert gallery, 25 November 2026, main `2081409`, light appearance, 1280 pixels wide at twice the density. |
| `finance-delivery.jpg` | The Finance Director's delivered report and file controls in Watch. | Actual native Claude Code work with the bundled synthetic Finance inputs, 24 October 2026. |
| `finance-report.png` | Page one of the actual downloaded Finance report. | Rendered from that synthetic report; not a UI mock or a claim that every generated report is correct. |

The gallery was refreshed when its artwork and Create control changed. On 25 November 2026, when
the Experts tab joined the bar, it was taken again as `expert-gallery.jpg`; the October file stays
with its own changelog entry. The Finance captures
retain their actual 24 October scope. Do not retake an unchanged result merely to change its date.

`docs/user-guide/assets/` holds byte-identical publication mirrors of the files the guide uses
(`expert-gallery.jpg`, `finance-delivery.jpg`, `finance-report.png`, `expert-versions.jpg`,
`experts-page.jpg` and `activity-filters.jpg`; its copy of `expert-team.jpg` is no longer shown and is
kept for history) so the portable
guide and its existing same-origin website publication work. These files remain the
canonical image source; update mirrors by exact copy, not by separate editing.

## Saved expert versions

`expert-versions.jpg` shows the actual Versions dialog after creating a second revision of a
synthetic Finance Director setup, 25 October 2026. Each version keeps its own name and
instructions. Choosing an earlier version keeps that exact version after reload; it does not
silently select the latest setup. Archive and Remove apply to the whole saved expert; Restore
makes its retained versions available again. The matching guide asset is byte-identical.

## Experts, channels and Activity

These show a staged synthetic example on a local install of main `2081409`, captured on
25 November 2026 in a browser window 1280 pixels wide at twice the pixel density. The experts (Finance Director,
Growth Marketer, Project Coordinator), the people (Maya Chen, Daniel Brooks, Priya Nair) and their
words are made up; two experts use the starter artwork as their picture. Their Teams links ran
against a local stand-in for Microsoft's service, so no Microsoft connection is shown or claimed.
Three lines are real runs on Claude: Maya Chen's two messages (a question back, then the
checklist job) and Priya Nair's request held by Ask me first. The other four lines were written as
stored rows, without an AI run, to show each status.

| Asset | What it shows | Used by |
| --- | --- | --- |
| `experts-page.jpg` | The Experts page: the row of experts with their pictures, and what the Finance Director does. | README, guide, `docs/10`, changelog |
| `experts-ai-team.jpg` | The AI team table: who talks, who coordinates, the Workers and the Preferred one. | `docs/10` |
| `teams-channel-card.jpg` | The Teams card in an expert's Channels window, with the people who may message it; its Connected badge reflects the stand-in. | `docs/10`, changelog |
| `needs-you-request.jpg` | A request the expert raised itself under Needs you: who asked, where, their words, its own reason and the reply that waits. | `docs/10`, changelog |
| `activity-filters.jpg` | Activity's filters and lines in several statuses. | README, guide, `docs/07`, changelog |
| `activity-line-opened.jpg` | One line opened with its recorded steps: the message, the job and what was sent back, with the engine steps folded under Details. | `docs/07`, changelog |
| `run-project-my-experts.jpg` | Run Project's My experts with each expert's own picture. | changelog |

## Retired walkthrough captures

Obsolete connectivity and QA screenshots were removed from the current publication when their
controls no longer matched the product. Git history retains their dated context. The guide does
not publish unused test captures or error-state leftovers.

## Capture and privacy rules

Use a fresh synthetic workspace at a readable desktop width. Show the selected professional,
actual work and openable result; do not present setup as completion. Check pixels, metadata and
filenames for credentials, account addresses, local paths, real company material and private tabs.
Update a screenshot when its depicted controls or result change. Screenshots are supporting
illustrations, not universal provider or deployment acceptance.

## Brand and comparison artwork

The README's light/dark wordmarks reuse the exact paths from the runtime's
[canonical brand symbol](../../../frontends/glass-drive-ui/src/glass_drive_ui/static/brand-symbol.svg).
Only fill, placement and the typeset project name differ; no replacement logo was invented.
The guide uses the same geometry with its current text color.

The [comparison SVG](../capability-comparison.svg), [accessible comparison](../../comparison.md)
and portable guide page are generated from [comparison.json](../../comparison.json) using
`scripts/render-capability-comparison.py`. The SVG is a compact landscape preview; the full
page contains every product, short capability descriptions, real-life examples and primary
source links. It is generated artwork, not a product screenshot or a speed benchmark.
