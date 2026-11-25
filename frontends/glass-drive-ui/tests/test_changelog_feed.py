"""CHANGELOG.md feeds the website's changelog page, whose reader rejects the whole file on one bad line.

These checks hold the file to the rules written at its top, so a change that the website could not read
fails here first.
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path

import pytest

CHANGELOG = Path(__file__).resolve().parents[3] / "CHANGELOG.md"
TOPICS = {"Experts", "Library", "Work control", "Results", "Scheduling", "Connections", "Integrations",
          "Deployment", "Reliability", "Website"}
CHANGE_LABELS = {"Added", "Improved", "Fixed", "Preserved", "Established"}
HEADING = re.compile(r"^## (\d{4}-\d{2}-\d{2}) — (.+)$")
QUOTED = re.compile(r"^> \*\*(Summary|Example|Topics|Image caption):\*\* (.+)$")
PICTURE = re.compile(r"^!\[([^\]]+)\]\(([^)]+)\)$")
ITEM = re.compile(r"^- \*\*([A-Za-z ]+):\*\* (.+)$")
PICTURE_PATH = re.compile(r"^docs/assets/screens/[a-z0-9-]+\.(jpg|png|webp)$")


def problems(text: str) -> list[str]:
    """Every rule the text breaks, one line each; empty when the website can read it."""
    found: list[str] = []
    if len(text) > 100_000 or not text.startswith("# Changelog\n"):
        found.append("the file must start with '# Changelog' and stay under 100,000 characters")
    text = re.sub(r"<!--[\s\S]*?-->", "", text)
    entries: list[dict] = []
    entry: dict | None = None
    for number, raw in enumerate(text.split("\n"), 1):
        line = raw.strip()
        heading = HEADING.match(line)
        if heading:
            date, title = heading.groups()
            where = f"line {number} [{date}]"
            try:
                datetime.date.fromisoformat(date)
            except ValueError:
                found.append(f"{where} is not a real date")
            if len(title) > 160:
                found.append(f"{where} title is longer than 160 characters")
            if entry and date >= entry["date"]:
                found.append(f"{where} is not newer-first with one entry per date (previous {entry['date']})")
            entry = {"date": date, "line": number, "Summary": 0, "Example": 0, "Topics": 0, "benefits": 0,
                     "changes": 0, "pictures": 0, "captioned": True}
            entries.append(entry)
            continue
        if line.startswith("## "):
            found.append(f"line {number} is a '## ' line that is not a dated entry")
            continue
        if entry is None or not line or line == ">":
            continue
        where = f"line {number} [{entry['date']}]"
        quoted, picture, item = QUOTED.match(line), PICTURE.match(line), ITEM.match(line)
        if quoted:
            key, value = quoted.groups()
            if len(value) > 700:
                found.append(f"{where} {key} is longer than 700 characters")
            if key == "Image caption":
                if entry["captioned"]:
                    found.append(f"{where} a caption must follow its one picture")
                entry["captioned"] = True
                continue
            entry[key] += 1
            if entry[key] > 1:
                found.append(f"{where} a second {key} line")
            if key == "Topics":
                topics = [topic.strip() for topic in value.split(",")]
                unknown = [topic for topic in topics if topic not in TOPICS]
                if unknown or len(set(topics)) != len(topics):
                    found.append(f"{where} topics must come from the list, without repeats: {unknown or topics}")
        elif picture:
            alt, path = picture.groups()
            entry["pictures"] += 1
            entry["captioned"] = False
            if entry["pictures"] > 2:
                found.append(f"{where} more than two pictures")
            if len(alt) > 240:
                found.append(f"{where} alt text is longer than 240 characters")
            if not PICTURE_PATH.match(path):
                found.append(f"{where} a picture must be docs/assets/screens/<name>.jpg, .png or .webp")
        elif item and item.group(1) == "Benefit":
            entry["benefits"] += 1
            if entry["benefits"] > 3:
                found.append(f"{where} more than three benefits")
            if len(item.group(2)) > 400:
                found.append(f"{where} a benefit is longer than 400 characters")
        elif item and item.group(1) in CHANGE_LABELS:
            entry["changes"] += 1
            if len(item.group(2)) > 1400:
                found.append(f"{where} an item is longer than 1,400 characters")
        else:
            found.append(f"{where} is not an accepted line: {line[:60]}")
    for each in entries:
        if not each["changes"]:
            found.append(f"line {each['line']} [{each['date']}] has no Added, Improved, Fixed, Preserved or Established item")
    if not entries:
        found.append("the file has no dated entry")
    if len(entries) > 200:
        found.append("more than 200 entries")
    return found


def test_the_changelog_follows_the_rules_the_website_reads() -> None:
    assert problems(CHANGELOG.read_text(encoding="utf-8")) == []


VALID = """# Changelog

## 2026-11-25 — A newer entry

> **Summary:** One line about it.
>
> **Topics:** Experts, Work control

![The Experts page.](docs/assets/screens/experts-page.jpg)
> **Image caption:** The Experts page.

- **Benefit:** One benefit.

- **Added:** One change.

## 2026-11-24 — An older entry

- **Fixed:** One fix.
"""


def test_a_well_formed_changelog_has_no_problems() -> None:
    assert problems(VALID) == []


@pytest.mark.parametrize(("change", "expected"), [
    (("## 2026-11-24 — An older entry", "## 2026-11-25 — A second entry on the same date"), "one entry per date"),
    (("> **Topics:** Experts, Work control", "> **Topics:** Experts, Activity"), "topics must come from the list"),
    (("- **Fixed:** One fix.", "- **Changed:** One change."), "not an accepted line"),
    (("- **Benefit:** One benefit.", "- **Benefit:** One.\n- **Benefit:** Two.\n- **Benefit:** Three.\n- **Benefit:** Four."),
     "more than three benefits"),
    (("> **Image caption:** The Experts page.",
      "> **Image caption:** The Experts page.\n![Two.](docs/assets/screens/a.jpg)\n![Three.](docs/assets/screens/b.jpg)"),
     "more than two pictures"),
])
def test_what_the_website_would_reject_is_caught(change: tuple[str, str], expected: str) -> None:
    before, after = change
    assert before in VALID
    found = problems(VALID.replace(before, after, 1))
    assert any(expected in problem for problem in found), found


def test_a_changelog_with_no_entry_is_caught() -> None:
    assert "the file has no dated entry" in problems("# Changelog\n\nNothing dated yet.\n")
