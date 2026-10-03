from __future__ import annotations


def normalize_launch_surface(value: str | None, default: str = "desktop") -> str:
    # The initial Watch surface is an explicit choice or the configured default; the task text
    # never selects it.
    normalized = str(value or "").strip().lower()
    if normalized in {"desktop", "terminal"}:
        return normalized
    return default


def build_operator_brief(description: str, success_criteria: str, context: str | None = None) -> str:
    # The run carries only what the user entered. Authority comes from the workspace's typed access
    # mode, and safety and completion rules from the runtime worker contract.
    sections = [description.strip()]
    criteria = success_criteria.strip()
    background = (context or "").strip()
    if criteria:
        sections.append(f"Success criteria:\n{criteria}")
    if background:
        sections.append(f"Background:\n{background}")
    return "\n\n".join(sections)


def build_project_title(description: str) -> str:
    words = [part for part in description.replace("\n", " ").split() if part]
    title = " ".join(words[:6]).strip()
    if not title:
        return "xPerfect Project"
    if len(words) > 6:
        title += "…"
    return title
