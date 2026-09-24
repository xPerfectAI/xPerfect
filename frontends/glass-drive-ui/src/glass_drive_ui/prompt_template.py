from __future__ import annotations

import re


def normalize_launch_surface(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"desktop", "terminal", "auto"}:
        return normalized
    return "auto"


def build_operator_brief(description: str, success_criteria: str, context: str | None = None) -> str:
    body = (context or "").strip()
    criteria = success_criteria.strip()
    optional_sections = (f"Success Criteria:\n{criteria}\n\n" if criteria else "")
    optional_sections += f"Context:\n{body}\n\n" if body else ""
    return (
        "You are the main worker for this project.\n\n"
        "Project Definition\n"
        "Describe your project:\n"
        f"{description.strip()}\n\n"
        f"{optional_sections}"
        "Execution Rules:\n"
        "- Treat the success criteria as mandatory acceptance gates.\n"
        "- Keep working, researching, and verifying until every success criterion is satisfied or a hard blocker requires operator intervention.\n"
        "- Repeatedly ask yourself: have I achieved the success criteria fully and correctly?\n"
        "- If the answer is no, continue working.\n"
        "- You are already fully authorized inside this sandbox. Do not pause for routine permission prompts, confirmation prompts, or sandbox prompts.\n"
        "- Use the non-interactive, no-approval, bypass-permissions, or dangerous mode flags that are appropriate for the tool you are using inside the sandbox.\n"
        "- If you need to run a local server, watcher, browser session, or any long-lived process, start it in the background, capture its PID or port, verify the result, and only then report success.\n"
        "- Never block the whole run on a foreground dev server or a foreground watcher unless the operator explicitly asked for a held-open checkpoint.\n"
        "- For temporary local verification flows, prefer one self-contained script or process that starts the server, performs the HTTP or browser check, and shuts the server down cleanly in the same run.\n"
        "- Do not rely on a background server surviving across separate tool calls unless you have verified that it stays alive.\n"
        "- When a success criterion requires visual or HTTP verification, perform the verification explicitly and include the concrete result in the final output.\n"
        "- If the deliverable is a webpage, landing page, app, or browser-visible result, open the final result inside the sandbox browser before declaring success and leave it visible for the operator.\n"
        "- For static webpage deliverables, verify through a localhost HTTP preview server when the browser/tooling does not support file:// URLs; shut down temporary servers unless the final result needs to remain visible.\n"
        "- If a local preview server is required for the final browser result, keep that preview server running in the sandbox at completion and report the URL you verified.\n"
        "- If you need irreversible external actions, risky writes, missing credentials, or human judgment, pause and surface the exact blocker.\n"
        "- Stay concise in progress reporting, but do the work thoroughly.\n"
    )


def build_project_title(description: str) -> str:
    words = [part for part in description.replace("\n", " ").split() if part]
    title = " ".join(words[:6]).strip()
    if not title:
        return "xPerfect Project"
    if len(words) > 6:
        title += "…"
    return title


def desktop_action_for_launch(profile: str, description: str) -> str:
    lowered = description.lower()
    external_browser_signals = (
        "http://",
        "https://",
        "website",
        "site",
        " url",
        "navigate",
        "playwright",
        "form",
        "submit",
    )
    deliverable_browser_signals = (
        "landing page",
        "web page",
        "webpage",
        "page",
        "frontend",
        "ui",
        "browser",
        "render",
        "html",
        "hero section",
        "microsite",
        "open the page",
    )
    browser_verification_only = "browser" in lowered or "chrome" in lowered or "chromium" in lowered
    mentions_external_domain = re.search(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)+\b", lowered) is not None
    if any(signal in lowered for signal in external_browser_signals) or mentions_external_domain:
        return "browser"
    if any(signal in lowered for signal in deliverable_browser_signals):
        return "browser"
    if profile.startswith("openclaw") and browser_verification_only:
        return "browser"
    mapping = {
        "codex-cli": "terminal",
        "claude-code": "terminal",
        "openclaw-general": "openclaw",
        "openclaw-codex": "openclaw",
        "openclaw-claude": "openclaw",
    }
    return mapping.get(profile, "terminal")


def watch_surface_for_launch(profile: str, description: str) -> str:
    action = desktop_action_for_launch(profile, description)
    if action == "browser":
        return "desktop"
    return "terminal"


def initial_watch_surface_for_launch(
    profile: str,
    description: str,
    *,
    launch_surface: str | None = None,
) -> str:
    normalized = normalize_launch_surface(launch_surface)
    if normalized in {"desktop", "terminal"}:
        return normalized
    return watch_surface_for_launch(profile, description)
