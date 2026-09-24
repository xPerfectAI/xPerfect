from __future__ import annotations

import base64
import errno
import hashlib
import hmac
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Callable, TypeVar

from .auth import multi_user_security_enabled
from .codex_plugins import provision_codex_official_marketplace

import errno

import stat

from contextlib import contextmanager

from functools import wraps

from typing import Any, Callable, TypeVar


JsonDict = dict[str, Any]
PromptProducer = TypeVar("PromptProducer", bound=Callable[..., object])
WORKER_PROMPT_LAYER_PRODUCER_BINDINGS: dict[str, tuple[str, ...]] = {}
WORKER_PROMPT_LAYER_DECLARATION_ATTRIBUTE = (
    "__glasshive_worker_prompt_layer_declaration__"
)
WORKER_PROMPT_LAYER_EMISSION_ATTRIBUTE = "__glasshive_worker_prompt_emissions__"


class WorkerPromptLayerText(str):
    """A prompt string carrying the producer identities that emitted it."""

    def __new__(
        cls,
        value: str,
        emissions: tuple[tuple[str, tuple[str, ...]], ...],
    ) -> WorkerPromptLayerText:
        instance = str.__new__(cls, value)
        setattr(instance, WORKER_PROMPT_LAYER_EMISSION_ATTRIBUTE, emissions)
        return instance


class WorkerPromptLayerDict(dict[str, Any]):
    """A prompt bundle carrying the producer identities that emitted it."""


def worker_prompt_layer_emissions(
    value: object,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    emissions = getattr(value, WORKER_PROMPT_LAYER_EMISSION_ATTRIBUTE, ())
    if not isinstance(emissions, tuple):
        return ()
    return tuple(
        (str(producer_ref), tuple(str(name) for name in layer_names))
        for producer_ref, layer_names in emissions
        if isinstance(producer_ref, str) and isinstance(layer_names, tuple)
    )


def emit_worker_prompt_layers(
    *,
    producer_ref: str,
    layer_names: tuple[str, ...],
    value: object,
) -> object:
    """Attach a checked producer identity to one actual prompt-layer value."""

    registered = WORKER_PROMPT_LAYER_PRODUCER_BINDINGS.get(producer_ref)
    if registered != layer_names:
        raise RuntimeError(f"Unregistered worker prompt producer: {producer_ref}")
    emissions = (*worker_prompt_layer_emissions(value), (producer_ref, layer_names))
    if isinstance(value, str):
        return WorkerPromptLayerText(value, emissions)
    if isinstance(value, dict):
        emitted = WorkerPromptLayerDict(value)
        setattr(emitted, WORKER_PROMPT_LAYER_EMISSION_ATTRIBUTE, emissions)
        return emitted
    try:
        setattr(value, WORKER_PROMPT_LAYER_EMISSION_ATTRIBUTE, emissions)
    except (AttributeError, TypeError) as exc:
        raise RuntimeError(
            f"Worker prompt producer output cannot carry identity: {producer_ref}"
        ) from exc
    return value


def worker_prompt_layer_producer(
    *layer_names: str,
) -> Callable[[PromptProducer], PromptProducer]:
    """Register prompt facts at the function that produces them."""

    normalized = tuple(sorted({str(name).strip() for name in layer_names if name}))
    if not normalized:
        raise ValueError("A worker prompt producer must declare at least one layer")

    def register(producer: PromptProducer) -> PromptProducer:
        producer_ref = f"{producer.__module__}.{producer.__qualname__}"
        existing = WORKER_PROMPT_LAYER_PRODUCER_BINDINGS.get(producer_ref)
        if existing is not None and existing != normalized:
            raise ValueError(f"Conflicting worker prompt producer registration: {producer_ref}")
        WORKER_PROMPT_LAYER_PRODUCER_BINDINGS[producer_ref] = normalized

        @wraps(producer)
        def emitted(*args: Any, **kwargs: Any) -> object:
            return emit_worker_prompt_layers(
                producer_ref=producer_ref,
                layer_names=normalized,
                value=producer(*args, **kwargs),
            )

        setattr(
            emitted,
            WORKER_PROMPT_LAYER_DECLARATION_ATTRIBUTE,
            (producer_ref, normalized),
        )
        setattr(emitted, "__glasshive_worker_prompt_producer_ref__", producer_ref)
        return emitted  # type: ignore[return-value]

    return register


VIVENTIUM_FEELING_STATE_PREFIX = "<viventium_feeling_state"
VIVENTIUM_FEELING_STATE_START = "<viventium_feeling_state>"
VIVENTIUM_FEELING_STATE_END = "</viventium_feeling_state>"

# This file owns the worker bootstrap boundary:
#
# - what the host is allowed to project into a worker
# - which prompt files are materialized into the workspace
# - which MCP/client config files are written for Codex and Claude
# - how secrets stay out of ordinary interactive shell files
#
# Keep the editable worker-facing prompts at the top of the file. They are intentionally plain
# strings so operators can review the actual text that lands in AGENTS.md / CLAUDE.md / CODEX.md
# without chasing helper functions.
PromptProducer = TypeVar("PromptProducer", bound=Callable[..., object])

WORKER_PROMPT_LAYER_PRODUCER_BINDINGS: dict[str, tuple[str, ...]] = {}

WORKER_PROMPT_LAYER_DECLARATION_ATTRIBUTE = (
    "__glasshive_worker_prompt_layer_declaration__"
)

WORKER_PROMPT_LAYER_EMISSION_ATTRIBUTE = "__glasshive_worker_prompt_emissions__"






VIVENTIUM_FEELING_STATE_PREFIX = "<viventium_feeling_state"

VIVENTIUM_FEELING_STATE_START = "<viventium_feeling_state>"

VIVENTIUM_FEELING_STATE_END = "</viventium_feeling_state>"

DEFAULT_BOOTSTRAP_SOURCE_MAX_BYTES = None
BOOTSTRAP_SOURCE_TOKEN_KEY = "source_path_token"
GLASSHIVE_CAPABILITY_BROKER_TOKEN_ENV = "GLASSHIVE_CAPABILITY_BROKER_TOKEN"
GLASSHIVE_PROVIDER_SESSION_MODE_ENV = "GLASSHIVE_PROVIDER_SESSION_MODE"
GLASSHIVE_PROVIDER_SESSION_EPOCH_ENV = "GLASSHIVE_PROVIDER_SESSION_EPOCH"
PARALLEL_CLEAN_ROOM_EXECUTION_POLICY = "parallel-clean-room-v1"

PARALLEL_CLEAN_ROOM_BROKER_NAME = "glasshive-user-capabilities"

PARALLEL_CLEAN_ROOM_BROKER_PROXY_URL = "http://host.docker.internal:8080/mcp"

CLEAN_ROOM_RUNTIME_ENV_KEYS = {GLASSHIVE_CAPABILITY_BROKER_TOKEN_ENV, "GLASSHIVE_PEER_TOKEN", "GLASSHIVE_CONTEXT_TOKEN"}

CLEAN_ROOM_HOME_AUTHORITY_PATHS = (
    ".grok/auth.json",
    ".grok/config.toml",
    ".grok/mcp_credentials.json",
    ".codex/auth.json",
    ".codex/config.toml",
    ".claude.json",
    ".claude/.credentials.json",
    ".claude/credentials.json",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".gitconfig",
    ".git-credentials",
    ".gitcookies",
    ".netrc",
    ".config/git/credentials",
    ".config/gh/hosts.yml",
    ".config/glab-cli/config.yml",
    ".ssh",
    ".bashrc",
    ".bash_profile",
    ".profile",
    ".zshrc",
)

CLEAN_ROOM_WORKSPACE_AUTHORITY_PATHS = (
    ".grok/config.toml",
    ".grok/auth.json",
    ".mcp.json",
    ".claude/settings.local.json",
)

GLASSHIVE_PROPORTIONAL_VERIFICATION_RULE = (
    "3. PROPORTIONAL VERIFICATION: Choose verification depth from the user's explicit success "
    "criteria, requested rigor, the risk of a wrong result, and concrete defects found. Use the "
    "smallest evidence that proves the result. When direct inspection is needed, use one relevant "
    "run, render, or interaction. Do not repeat an equivalent check after the output satisfies the "
    "request; re-check only after a relevant output change or a detected defect. Never report "
    "success without that evidence."
)

GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS = f"""CRITICAL OPERATING INSTRUCTIONS (FOLLOW STRICTLY):

1. PATH OF LEAST RESISTANCE: Use the simplest, most direct solution. Don't reinvent wheels.

2. JUST DO IT: Execute immediately without asking questions. Users want RESULTS. Rely on your intelligence, tools, MCPs, skills to find ways around blockers to get it done full and complete.

{GLASSHIVE_PROPORTIONAL_VERIFICATION_RULE}

If a server is only for QA or preview, use a bounded run or explicit cleanup; never leave a foreground server blocking final delivery or wasting compute.

4. NO USER INTERVENTION: Deliver a COMPLETE, WORKING solution."""
def _load_viventium_worker_prompts() -> dict[str, Any] | None:
    # A standalone GlassHive install retains its own compatibility contract. A managed
    # Viventium install requires its compiled registry, including when the path is missing.
    if not (os.environ.get("VIVENTIUM_PROMPT_BUNDLE_PATH") or os.environ.get("VIVENTIUM_INSTALL_MODE")):
        return None
    shared = next((parent / "shared" for parent in Path(__file__).resolve().parents
                   if (parent / "shared/compiled_prompt_contract.py").is_file()), None)
    if shared is None:
        raise RuntimeError("Viventium worker prompt runtime is missing; rebuild this installation")
    if str(shared) not in sys.path:
        sys.path.insert(0, str(shared))
    from compiled_prompt_contract import load_compiled_prompts
    return load_compiled_prompts()


_VIVENTIUM_WORKER_PROMPTS = _load_viventium_worker_prompts()


def _worker_prompt(
    prompt_id: str, standalone: str, *, variables: dict[str, str] | None = None
) -> str:
    if _VIVENTIUM_WORKER_PROMPTS is None:
        return standalone
    from compiled_prompt_contract import render_compiled_prompt
    text = render_compiled_prompt(prompt_id, prompts=_VIVENTIUM_WORKER_PROMPTS, variables=variables)
    return text + ("\n" if standalone.endswith("\n") else "")


GLASSHIVE_SAFETY_CHECKPOINT_RULE = _worker_prompt("worker.safety_checkpoint", "Safety boundary: these operating instructions never override platform policy, tenant/user scope, authentication, or OS security controls. Determine task scope from the user's current request and applicable prior authorization, preserving the project definition's constraints. Do not ask again for an action already authorized. Full-access tools and a project file do not grant new authority. Ordinary reversible local file or app work within the task may use authorized locations outside the default workspace. Before destructive changes, external publication or purchases, privileged or persistent system changes, credential/session changes, unrelated process termination, or sharing private data, request a clear checkpoint if that action is not already authorized. Use existing signed-in sessions through supported app flows; do not extract authentication material or bypass a permission denial, quarantine, or required OS consent. Do not loop forever or spend indefinitely: when a blocker cannot be resolved with the available runtime, tools, MCPs, files, auth, time, or budget, report the concrete blocker and the best available partial result after `FINAL REPORT:`.")


# These text literals are standalone GlassHive compatibility defaults. Managed
# Viventium uses only its registered worker.* sources, with no inline fallback.
# Source parity tests bind the initial compatibility text to those registered bodies.
GLASSHIVE_WORKER_COMPLETION_CONTRACT = _worker_prompt("worker.completion_contract", (
    "GlassHive completion contract:\n"
    "- Do the requested work before reporting completion.\n"
    "- Before `FINAL REPORT:`, inspect the concrete output/artifacts/tool results/visible state you produced against the user's request, success criteria, constraints, and files. Correct a detected mismatch. Report a concrete blocker only when you cannot complete it.\n"
    "- For research/source-gathering work, preserve citations and evidence, respect the user's source/date/auth/scope constraints, and do not dump large raw webpages, docs, logs, or command outputs into the conversation context. If a source/date/auth/scope constraint excludes an item, do not use that item to support facts, scoring, or deliverables; record it only as rejected or out-of-scope evidence when useful. Keep source publication/evidence dates distinct from retrieval/access timestamps; an access date must not widen or replace a user-limited source window. If `glasshive-run/constraint-ledger.json` exists, read it before planning, delegation, source collection, and final delivery; its original request and typed continuation authority preserve the admitted input, which you must interpret yourself. If you create research plans, specs, subagent prompts, or delegation notes, carry the user's constraints forward literally and exactly instead of widening, weakening, summarizing away, or rewriting them. If a plan/spec/delegation conflicts with the admitted user request or typed authority, correct that file before continuing. Save working notes/excerpts to files when useful and bring back concise source-grounded summaries so the task can continue without overflowing or destabilizing the provider route.\n"
    "- `glasshive-run/` is reserved for internal harness support evidence, not user-facing artifacts. Save every user-facing artifact outside `glasshive-run/` so GlassHive can discover and deliver it.\n"
    "- For long-running work, keep durable checkpoints in workspace files and prioritize a usable core result before optional expansion. If time, tool, auth, or dependency limits prevent the full requested deliverable, stop with an honest partial artifact/report and the exact blocker instead of spending the entire run on private notes.\n"
    "- When the request calls for a report, document, deck, client deliverable, or other shareable work product and the user did not ask for a technical/source format, make the primary user-facing output a polished ordinary end-user artifact such as PDF, DOCX, PPTX, spreadsheet, or another appropriate professional format. Markdown, HTML, or source files may be included as supporting artifacts, but should not be the only default deliverable for that class of work unless the runtime cannot create a professional artifact; if blocked, say so concretely.\n"
    "- For visual/shareable artifacts such as PDFs, slide decks, screenshots, or HTML reports, open or render the final artifact itself and verify that key text, tables, images, and pages are readable, not clipped, and not overlapped. Correct a detected layout defect or state the specific remaining limitation before `FINAL REPORT:`.\n"
    "- If you spawn any child agent, join every spawned child and incorporate its result before writing `FINAL REPORT:`. Do not report completion while a child remains running, open, or aborted.\n"
    "- Your final assistant message MUST end with a separate section exactly named `FINAL REPORT:`.\n"
    "- Put only the user-facing result after `FINAL REPORT:`. Include the concrete outcome, key facts, artifact/file names when useful, blockers, or the next decision needed.\n"
    "- If the user requested a very short answer or an exact string, put only that answer after `FINAL REPORT:`.\n"
    "- Do not put progress narration after `FINAL REPORT:`."
))
GLASSHIVE_NATIVE_CAPABILITY_INVENTORY = _worker_prompt("worker.native_capability_inventory", """Native capability discovery (choose when relevant, never forced):

- You may have worker-native CLI, browser/computer-use, MCP, plugin, and skill surfaces. Inspect what is actually available before saying a capability is unavailable, and do not claim to have used a capability unless you have evidence.
- In GlassHive workstation workspaces, a visible desktop/browser substrate may already be running. When browser or computer use is relevant, verify the live browser, noVNC desktop, local Chromium, `wmctrl`/`xdotool`, and WebDriver/Selenium endpoint before choosing a headless or offscreen path. Use the visible workstation surface when it improves user observability or task reliability.
- Treat preinstalled document/browser tooling as a substrate to verify, not a promise. Docker workstation images commonly include Chromium/noVNC, Selenium WebDriver on localhost, Python Selenium, `requests`, LibreOffice, Pandoc, and document libraries, but the worker should check the actual runtime before relying on any optional package or CLI.
- For deep research and document-generation work, use available research, browser, spreadsheet, PDF, document, deck, notebook, rendering, or verification tools when they materially improve the result. Prefer loading or invoking capabilities on demand instead of assuming a fixed skill catalog.
- Before writing scripts that import non-stdlib packages or call optional CLIs, verify the package/tool is available in this worker environment; otherwise use an available alternative or report the concrete dependency blocker.
- Do not overfit to examples, force a specific provider/tool/workflow, invent installed skills, or replace the worker's own planning and review with host-authored workflows.
""")
GLASSHIVE_WORKER_PROJECT_CONTRACT = f"""# GlassHive Worker Contract

- You are a general intelligent worker. Less is more: preserve the user's real goal, constraints, files, MCP/tool capabilities, and context without inventing project goals, success criteria, provider lists, forced artifacts, output schemas, rankings, or workflow steps.
- Treat MCP/tools as available capabilities, not proof that work was done. Use brokered MCP/tools when configured and appropriate; if a needed tool, grant, auth, file, or runtime is absent or fails, report that concrete blocker instead of pretending to have used it.
- Keep data in and data out exact. Read the actual workspace files, uploaded paths, MCP/tool results, generated outputs, and visible state before relying on them.
- Mention user-facing artifacts/files only when you intentionally created them, they are needed, or the user asked for them. Do not force a download when a concise chat answer satisfies the request.

{GLASSHIVE_NATIVE_CAPABILITY_INVENTORY}

{GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS}

{GLASSHIVE_WORKER_COMPLETION_CONTRACT}

{GLASSHIVE_SAFETY_CHECKPOINT_RULE}
"""
DEFAULT_ENTERPRISE_WORKER_ENV_KEYS = {
    "XAI_API_KEY",
    "GLASSHIVE_PEER_TOKEN",
    "GLASSHIVE_CONTEXT_TOKEN",
    GLASSHIVE_CAPABILITY_BROKER_TOKEN_ENV,
    GLASSHIVE_PROVIDER_SESSION_MODE_ENV,
    GLASSHIVE_PROVIDER_SESSION_EPOCH_ENV,
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_REVERSE_PROXY",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_EC2_METADATA_DISABLED",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_BEDROCK_SERVICE_TIER",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_URL",
    "PORTKEY_API_KEY",
    "PORTKEY_BASE_URL",
    "PORTKEY_PROVIDER",
    "PORTKEY_VIRTUAL_KEY",
    "PORTKEY_CONFIG",
    "WPR_CLAUDE_CODE_USE_API_KEY",
}
RUN_BOUND_PROVIDER_ENV_KEYS = {
    "XAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_REVERSE_PROXY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
    "PORTKEY_API_KEY",
    "PORTKEY_BASE_URL",
    "PORTKEY_PROVIDER",
    "PORTKEY_VIRTUAL_KEY",
    "PORTKEY_CONFIG",
    "WPR_CLAUDE_CODE_USE_API_KEY",
}
CODEX_DEPLOYMENT_PROVIDER_ENV_KEYS = {
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_REVERSE_PROXY",
    "PORTKEY_API_KEY",
    "PORTKEY_BASE_URL",
    "PORTKEY_PROVIDER",
    "PORTKEY_VIRTUAL_KEY",
    "PORTKEY_CONFIG",
}
CLAUDE_DEPLOYMENT_PROVIDER_ENV_KEYS = RUN_BOUND_PROVIDER_ENV_KEYS - CODEX_DEPLOYMENT_PROVIDER_ENV_KEYS - {"XAI_API_KEY"}
DEFAULT_SECRET_ENV_MARKERS = (
    "ACCESS_KEY",
    "API_KEY",
    "AUTH_TOKEN",
    "BEARER",
    "CLIENT_SECRET",
    "CUSTOM_HEADERS",
    "HMAC",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
    "VIRTUAL_KEY",
)
USER_PROVIDER_SECRET_ENV_PREFIXES = (
    "GMAIL_",
    "GOOGLE_",
    "GOOGLE_WORKSPACE_",
    "MICROSOFT_",
    "MS365_",
    "MS_GRAPH_",
    "OUTLOOK_",
)
USER_PROVIDER_SECRET_ENV_MARKERS = (
    "ACCESS_TOKEN",
    "CLIENT_SECRET",
    "ID_TOKEN",
    "OAUTH",
    "REFRESH_TOKEN",
    "SECRET",
    "SESSION_TOKEN",
    "TOKEN",
)
RESERVED_HOST_RUNTIME_ENV_KEYS = {
    "HOME",
    "PATH",
    "SHELL",
    "TERM",
    "TMPDIR",
    "USER",
    "LOGNAME",
}
SERVER_ONLY_RUNTIME_ENV_KEYS = {
    "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET",
    "VIVENTIUM_GLASSHIVE_ADMISSION_URL",
    "VIVENTIUM_GLASSHIVE_ADMISSION_SECRET",
}


RESERVED_HOST_RUNTIME_ENV_KEYS = {
    "HOME",
    "PATH",
    "SHELL",
    "TERM",
    "TMPDIR",
    "USER",
    "LOGNAME",
}

SERVER_ONLY_RUNTIME_ENV_KEYS = {
    "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET",
    "VIVENTIUM_GLASSHIVE_ADMISSION_URL",
    "VIVENTIUM_GLASSHIVE_ADMISSION_SECRET",
}

def _instruction_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _unique_instruction_parts(*values: Any) -> list[str]:
    parts: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _instruction_text(value)
        if not text or text in seen:
            continue
        parts.append(text)
        seen.add(text)
    return parts


def _split_viventium_feeling_capsules(value: Any) -> tuple[str, list[str]]:
    """Remove managed Feeling blocks while preserving their exact bytes for validation."""

    text = _instruction_text(value)
    if not text:
        return "", []
    clean_parts: list[str] = []
    capsules: list[str] = []
    cursor = 0
    while True:
        start = text.find(VIVENTIUM_FEELING_STATE_PREFIX, cursor)
        orphaned_end = text.find(VIVENTIUM_FEELING_STATE_END, cursor)
        if start < 0:
            if orphaned_end >= 0:
                raise ValueError("Malformed Viventium Feeling state instruction block")
            clean_parts.append(text[cursor:])
            break
        if orphaned_end >= 0 and orphaned_end < start:
            raise ValueError("Malformed Viventium Feeling state instruction block")
        if not text.startswith(VIVENTIUM_FEELING_STATE_START, start):
            raise ValueError("Malformed Viventium Feeling state instruction block")
        content_start = start + len(VIVENTIUM_FEELING_STATE_START)
        end = text.find(VIVENTIUM_FEELING_STATE_END, content_start)
        if end < 0 or text.find(VIVENTIUM_FEELING_STATE_PREFIX, content_start, end) >= 0:
            raise ValueError("Malformed Viventium Feeling state instruction block")
        end += len(VIVENTIUM_FEELING_STATE_END)
        clean_parts.append(text[cursor:start])
        capsules.append(text[start:end])
        cursor = end
    return "\n\n".join(
        part.strip() for part in clean_parts if part.strip()
    ), capsules


def _is_exact_conversation_feeling_mirror(
    bundle: JsonDict, capsules: list[str]
) -> bool:
    """Accept only the three non-projected mirrors produced for a native conversation."""

    if bundle.get("run_mode") != "conversation":
        return False
    field_capsules: dict[str, list[str]] = {}
    field_clean: dict[str, str] = {}
    for field in (
        "application_developer_instructions",
        "developer_instructions",
        "declared_developer_instruction_tail",
    ):
        clean, found = _split_viventium_feeling_capsules(bundle.get(field))
        field_clean[field] = clean
        field_capsules[field] = found
    if sum(len(found) for found in field_capsules.values()) != len(capsules):
        return False
    tail_capsules = field_capsules["declared_developer_instruction_tail"]
    if (
        len(tail_capsules) != 1
        or field_clean["declared_developer_instruction_tail"]
        or any(len(field_capsules[field]) != 1 for field in field_capsules)
    ):
        return False
    capsule = tail_capsules[0]
    return (
        all(found == [capsule] for found in field_capsules.values())
        and _instruction_text(bundle.get("developer_instructions")).endswith(capsule)
    )


@worker_prompt_layer_producer("viventium_feeling_state")
def canonicalize_viventium_feeling_projection(bundle: JsonDict) -> JsonDict:
    """Materialize one eligible capsule in AGENTS.md, or zero when scope is off."""

    capsules: list[str] = []

    def strip_capsules(value: Any) -> Any:
        if isinstance(value, str):
            clean, found = _split_viventium_feeling_capsules(value)
            capsules.extend(found)
            return clean
        if isinstance(value, dict):
            return {key: strip_capsules(item) for key, item in value.items()}
        if isinstance(value, list):
            return [strip_capsules(item) for item in value]
        return value

    canonical = strip_capsules(dict(bundle))

    projection = canonical.get("viventium_feelings_projection")
    if not isinstance(projection, dict):
        if len(capsules) > 1 and not _is_exact_conversation_feeling_mirror(
            bundle, capsules
        ):
            raise ValueError("Conflicting Viventium Feeling state instruction blocks")
        # Legacy native conversation authority has no projection envelope. It
        # may retain one structurally valid capsule, or the exact three-field
        # storage mirror used to reconstruct its sole projected developer
        # instruction. Any extra or conflicting capsule remains rejected.
        return dict(bundle)
    expected_fields = {
        "version",
        "enabled",
        "scope",
        "canonical_instruction_field",
        "snapshot_sha256",
        "expected_capsule_count",
    }
    version = projection.get("version")
    enabled = projection.get("enabled")
    scope = projection.get("scope")
    expected_count = projection.get("expected_capsule_count")
    declared_hash = projection.get("snapshot_sha256")
    if (
        set(projection) != expected_fields
        or type(version) is not int
        or version != 1
        or type(enabled) is not bool
        or not isinstance(scope, str)
        or scope not in {"all_agents", "conscious_agent", "unknown"}
        or type(expected_count) is not int
        or expected_count not in {0, 1}
        or projection.get("canonical_instruction_field") != "agents_md"
        or not isinstance(declared_hash, str)
        or (
            declared_hash not in {"", "none"}
            and (
                len(declared_hash) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in declared_hash
                )
            )
        )
    ):
        raise ValueError("Invalid Viventium Feeling projection contract")
    if enabled != (scope == "all_agents" and expected_count == 1):
        raise ValueError("Viventium Feeling projection contradicts its direct-worker scope")
    if not enabled:
        if expected_count != 0:
            raise ValueError("Scope-off Viventium Feeling projection must expect zero capsules")
        return canonical
    if declared_hash in {"", "none"} or len(capsules) != 1:
        raise ValueError("Enabled Viventium Feeling projection requires one exact capsule")
    capsule = capsules[0]
    actual_hash = hashlib.sha256(capsule.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(actual_hash, declared_hash):
        raise ValueError("Viventium Feeling projection snapshot hash mismatch")
    canonical["agents_md"] = "\n\n".join(
        part for part in (canonical.get("agents_md", "").strip(), capsule) if part
    )
    return canonical

_canonicalize_viventium_feeling_projection = (
    canonicalize_viventium_feeling_projection
)

@worker_prompt_layer_producer(
    "glasshive_worker_project_contract",
    "system_instructions",
)
def merge_glasshive_worker_instructions(*values: Any) -> str:
    extras = [
        text
        for text in _unique_instruction_parts(*values)
        if text.strip() != GLASSHIVE_WORKER_PROJECT_CONTRACT.strip()
    ]
    body = GLASSHIVE_WORKER_PROJECT_CONTRACT.rstrip()
    if not extras:
        return body + "\n"
    return body + "\n\nHost-provided instructions:\n" + "\n\n".join(extras).rstrip() + "\n"


@worker_prompt_layer_producer("agents_md")
def glasshive_project_agents_md(bundle: JsonDict) -> str:
    merged = merge_glasshive_worker_instructions(
        bundle.get("agents_md"), bundle.get("system_instructions")
    )
    projection = bundle.get("viventium_feelings_projection")
    if not isinstance(projection, dict) or projection.get("enabled") is not True:
        return merged
    stable_authority, capsules = _split_viventium_feeling_capsules(merged)
    if len(capsules) != 1:
        raise ValueError("Enabled worker authority requires one final Feeling capsule")
    final_authority = "\n\n".join(
        part for part in (stable_authority.rstrip(), capsules[0]) if part
    ) + "\n"
    return WorkerPromptLayerText(
        final_authority,
        worker_prompt_layer_emissions(merged),
    )


@worker_prompt_layer_producer("claude_md")
def glasshive_project_claude_md(bundle: JsonDict) -> str:
    explicit = _instruction_text(bundle.get("claude_md"))
    body = (
        "@AGENTS.md\n\n"
        "Claude worker context. Treat AGENTS.md as the canonical GlassHive project instruction source."
    )
    if explicit and explicit != "@AGENTS.md":
        body += "\n\nClaude-specific host instructions:\n" + explicit
    return body.rstrip() + "\n"


@worker_prompt_layer_producer("codex_md")
def glasshive_project_codex_md(bundle: JsonDict) -> str:
    explicit = _instruction_text(bundle.get("codex_md"))
    body = "Codex worker context. AGENTS.md is the canonical GlassHive project instruction source."
    if explicit:
        body += "\n\nCodex-specific host instructions:\n" + explicit
    return body.rstrip() + "\n"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "enabled"}


def _enterprise_mode_enabled() -> bool:
    return multi_user_security_enabled()


def _bootstrap_source_secret() -> str:
    # Source-path authority is its own trust plane. Callback or service bearer
    # compromise must not mint a file-copy token.
    return os.environ.get("GLASSHIVE_BOOTSTRAP_SOURCE_SECRET", "").strip()


def _canonical_source_for_token(source: Path | str) -> str:
    return os.fspath(Path(os.path.abspath(os.fspath(Path(source).expanduser()))))


def sign_bootstrap_source_path(source: Path | str, *, tenant_id: str | None = None, owner_id: str | None = None) -> str:
    secret = _bootstrap_source_secret()
    if not secret:
        return ""
    message = "\0".join(
        (
            "v1",
            _canonical_source_for_token(source),
            str(tenant_id or ""),
            str(owner_id or ""),
        )
    )
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"v1:{digest}"


def _source_token_is_valid(entry: dict[str, Any], source: Path | str, worker: dict[str, Any]) -> bool:
    expected = sign_bootstrap_source_path(
        source,
        tenant_id=str(worker.get("tenant_id") or ""),
        owner_id=str(worker.get("owner_id") or ""),
    )
    token = str(entry.get(BOOTSTRAP_SOURCE_TOKEN_KEY) or "").strip()
    return bool(expected and token and hmac.compare_digest(token, expected))


def resolve_authorized_bootstrap_source_path(
    entry: dict[str, Any], source: Path | str, worker: dict[str, Any]
) -> Path:
    """Apply one source-path trust contract for Docker and host-native workers."""

    if _enterprise_mode_enabled() and not _source_token_is_valid(entry, source, worker):
        raise PermissionError("Bootstrap source_path is not authorized for this enterprise user")
    return resolve_bootstrap_source_path(source)

def _worker_env_allowlist() -> set[str]:
    raw = os.environ.get("GLASSHIVE_WORKER_ENV_ALLOWLIST", "").strip()
    if not raw:
        return set(DEFAULT_ENTERPRISE_WORKER_ENV_KEYS)
    values = {item.strip() for item in raw.split(",") if item.strip()}
    disallowed = sorted(key for key in values if _looks_like_user_provider_secret_env_key(key))
    if disallowed:
        raise RuntimeError(
            "GLASSHIVE_WORKER_ENV_ALLOWLIST must not include user provider OAuth/session token keys: "
            + ", ".join(disallowed)
        )
    return (values | DEFAULT_ENTERPRISE_WORKER_ENV_KEYS) - SERVER_ONLY_RUNTIME_ENV_KEYS


def _looks_like_user_provider_secret_env_key(key: str) -> bool:
    upper = key.upper()
    return any(upper.startswith(prefix) for prefix in USER_PROVIDER_SECRET_ENV_PREFIXES) and any(
        marker in upper for marker in USER_PROVIDER_SECRET_ENV_MARKERS
    )


def _worker_secret_env_keys(env: dict[str, str]) -> set[str]:
    raw = os.environ.get("GLASSHIVE_WORKER_SECRET_ENV_KEYS", "").strip()
    configured = {item.strip() for item in raw.split(",") if item.strip()}
    detected = {
        key
        for key in env
        if any(marker in key.upper() for marker in DEFAULT_SECRET_ENV_MARKERS)
    }
    return configured | detected


def _worker_secret_env_exposure_mode() -> str:
    raw = os.environ.get("GLASSHIVE_WORKER_SECRET_ENV_EXPOSURE", "").strip().lower()
    if raw in {"shell", "runtime", "legacy"}:
        return "shell"
    return "run-only"


def bootstrap_profile_for(worker: dict[str, Any], runtime_name: str) -> str:
    configured = str(worker.get("bootstrap_profile") or "").strip()
    if configured:
        return configured
    if runtime_name == "codex-cli":
        return "codex-host"
    if runtime_name == "claude-code":
        return "claude-host"
    return "host-login"


def bootstrap_bundle_for(worker: dict[str, Any]) -> JsonDict:
    """Return the structured bootstrap bundle attached to a worker row.

    Typical shape, abbreviated:

        {
            "project_definition": "# User goal...",
            "files": [{"scope": "workspace", "path": "uploads/brief.pdf", "source_path": "..."}],
            "claude_project_mcp": {"glasshive-user-capabilities": {...}},
            "codex_config_append": "[mcp_servers.glasshive-user-capabilities]...",
            "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "..."}
        }

    The bundle is the host-to-worker data plane. It should carry real files, real scoped grants,
    and real configuration only; missing capabilities are represented as missing data, not invented
    prompt text.
    """
    from .peer_collaboration import project_peer_bootstrap
    from .coordinator_mcp import project_coordinator_bootstrap

    raw = worker.get("bootstrap_bundle_json")
    if isinstance(raw, dict):
        parsed = raw
    else:
        try:
            parsed = json.loads(str(raw)) if raw else {}
        except json.JSONDecodeError:
            parsed = {}
    return project_coordinator_bootstrap(worker, project_peer_bootstrap(worker, parsed if isinstance(parsed, dict) else {}))


def bootstrap_env_for(
    worker: dict[str, Any], runtime_name: str | None = None
) -> dict[str, str]:
    bundle = bootstrap_bundle_for(worker)
    raw = bundle.get("env")
    enterprise = _enterprise_mode_enabled()
    clean_room = (
        str(bundle.get("execution_policy") or "").strip()
        == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
    )
    allowed = (
        CLEAN_ROOM_RUNTIME_ENV_KEYS
        if clean_room
        else (_worker_env_allowlist() if enterprise else None)
    )
    if not isinstance(raw, dict):
        env = {}
    else:
        env = {}
        for key, value in raw.items():
            if value is None:
                continue
            env_key = str(key)
            upper_env_key = env_key.upper()
            if upper_env_key in RESERVED_HOST_RUNTIME_ENV_KEYS:
                continue
            if upper_env_key in SERVER_ONLY_RUNTIME_ENV_KEYS:
                continue
            if _looks_like_user_provider_secret_env_key(env_key):
                continue
            if allowed is not None and env_key not in allowed:
                continue
            env[env_key] = str(value)
    if not clean_room and _env_flag("GLASSHIVE_PROJECT_PROVIDER_ENV", default=enterprise):
        for key in _worker_env_allowlist():
            value = os.environ.get(key)
            if value and key not in env:
                env[key] = value
    if enterprise:
        selected_runtime = str(
            runtime_name or worker.get("profile") or worker.get("runtime") or ""
        ).strip().lower()
        if selected_runtime == "codex-cli" or selected_runtime == "openclaw-codex":
            selected_key = str(os.environ.get("WPR_CODEX_CLI_ENV_KEY") or "").strip()
            explicit_base = str(os.environ.get("WPR_CODEX_CLI_BASE_URL") or "").strip()
            if not selected_key:
                selected_key = (
                    "PORTKEY_API_KEY"
                    if os.environ.get("PORTKEY_BASE_URL", "").strip()
                    and not explicit_base
                    and not any(
                        os.environ.get(name, "").strip()
                        for name in (
                            "OPENAI_BASE_URL",
                            "OPENAI_API_BASE",
                            "OPENAI_REVERSE_PROXY",
                        )
                    )
                    else "OPENAI_API_KEY"
                )
            if selected_key not in {"OPENAI_API_KEY", "PORTKEY_API_KEY"}:
                profile_provider_keys = set()
            else:
                profile_provider_keys = {
                key
                for key in CODEX_DEPLOYMENT_PROVIDER_ENV_KEYS
                if key.startswith("PORTKEY_")
            } if selected_key == "PORTKEY_API_KEY" else {
                key
                for key in CODEX_DEPLOYMENT_PROVIDER_ENV_KEYS
                if not key.startswith("PORTKEY_")
            }
        elif selected_runtime.startswith("openclaw"):
            selected_key = str(os.environ.get("WPR_OPENCLAW_ENV_KEY") or "").strip()
            explicit_base = str(os.environ.get("WPR_OPENCLAW_BASE_URL") or "").strip()
            if not selected_key:
                selected_key = (
                    "PORTKEY_API_KEY"
                    if os.environ.get("PORTKEY_BASE_URL", "").strip()
                    and not explicit_base
                    and not any(
                        os.environ.get(name, "").strip()
                        for name in (
                            "OPENAI_BASE_URL",
                            "OPENAI_API_BASE",
                            "OPENAI_REVERSE_PROXY",
                        )
                    )
                    else "OPENAI_API_KEY"
                )
            if selected_key not in {"OPENAI_API_KEY", "PORTKEY_API_KEY"}:
                profile_provider_keys = set()
            else:
                profile_provider_keys = {
                key
                for key in CODEX_DEPLOYMENT_PROVIDER_ENV_KEYS
                if key.startswith("PORTKEY_")
            } if selected_key == "PORTKEY_API_KEY" else {
                key
                for key in CODEX_DEPLOYMENT_PROVIDER_ENV_KEYS
                if not key.startswith("PORTKEY_")
            }
        elif selected_runtime == "grok-build":
            profile_provider_keys = {"XAI_API_KEY"}
        elif selected_runtime == "claude-code":
            profile_provider_keys = CLAUDE_DEPLOYMENT_PROVIDER_ENV_KEYS
        else:
            profile_provider_keys = set()
        for key in RUN_BOUND_PROVIDER_ENV_KEYS - profile_provider_keys:
            env.pop(key, None)
    if worker.get("_glasshive_provider_account_bound") or worker.get(
        "_glasshive_inference_broker_bound"
    ):
        # The run launcher projects the selected personal home or short-lived
        # broker grant explicitly. Persisted deployment credentials are sourced
        # by the shell after docker-exec env processing and would otherwise
        # override that run-scoped selection.
        for key in RUN_BOUND_PROVIDER_ENV_KEYS:
            env.pop(key, None)
    return env


def apply_bootstrap(
    *,
    home_dir: Path,
    workspace_dir: Path,
    runtime_name: str,
    worker: dict[str, Any],
    copy_file: Callable[[Path, Path], None],
    copy_tree: Callable[[Path, Path], None],
    trusted_state_dir: Path | None = None,
) -> None:
    """Materialize login/config/files for a fresh sandbox worker.

    Local developer mode may copy existing CLI auth so the worker can run with the owner's tools.
    Enterprise mode does not copy host auth files; it projects only the scoped bundle/env allowed by
    policy and writes MCP grants into owner-only files.
    """
    profile = bootstrap_profile_for(worker, runtime_name)
    bundle = canonicalize_viventium_feeling_projection(
        bootstrap_bundle_for(worker)
    )
    execution_policy = str(bundle.get("execution_policy") or "").strip()
    if (
        execution_policy == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
        and profile != "clean-room"
    ):
        raise PermissionError(
            "The Parallel clean-room execution policy requires the clean-room bootstrap profile"
        )

    if execution_policy != PARALLEL_CLEAN_ROOM_EXECUTION_POLICY:
        _invalidate_clean_room_execution_policy(trusted_state_dir)

    if profile == "clean-room":
        _purge_clean_room_authority(home_dir, workspace_dir, trusted_state_dir)

    if (
        runtime_name == "codex-cli"
        and str(worker.get("execution_mode") or "") == "docker"
        and (
            not _enterprise_mode_enabled()
            or bool(worker.get("_glasshive_provider_account_bound"))
        )
    ):
        provision_codex_official_marketplace(home_dir)

    if (
        profile not in {"clean-room", "none"}
        and not _enterprise_mode_enabled()
        and not worker.get("_glasshive_provider_account_bound")
    ):
        if profile in {"host-login", "full-local", "codex-host"} or runtime_name in {"codex-cli", "openclaw"}:
            copy_file(Path.home() / ".codex" / "auth.json", home_dir / ".codex" / "auth.json")
        if profile in {"host-login", "full-local", "claude-host"} or runtime_name in {"claude-code", "openclaw"}:
            copy_file(Path.home() / ".claude.json", home_dir / ".claude.json")
        if profile in {"host-login", "full-local", "claude-host"} and runtime_name == "claude-code":
            copy_tree(Path.home() / ".claude", home_dir / ".claude")
        if profile in {"host-login", "full-local", "claude-host"} and runtime_name == "openclaw":
            copy_file(Path.home() / ".claude" / "settings.json", home_dir / ".claude" / "settings.json")
        if profile in {"host-login", "full-local", "codex-host", "claude-host"}:
            copy_file(Path.home() / ".gitconfig", home_dir / ".gitconfig")

    # Parallel run authority is projected later into the attested container
    # generation's /run tmpfs. Never persist it in the bind-mounted worker home.
    _write_runtime_env(
        home_dir,
        (
            {}
            if execution_policy == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
            else bootstrap_env_for(worker, runtime_name)
        ),
        force_run_only_secrets=execution_policy == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
    )
    _write_project_files(home_dir, workspace_dir, bundle, worker, copy_file, copy_tree)
    _write_claude_project_files(
        workspace_dir, bundle, grok_acp=runtime_name == "grok-build"
    )
    _write_codex_config(home_dir, bundle)
    _write_manifest(home_dir, profile, bundle)
    if execution_policy == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY:
        _record_clean_room_execution_policy(trusted_state_dir)


def refresh_runtime_env_for_worker(
    home_dir: Path, worker: dict[str, Any], runtime_name: str | None = None
) -> None:
    """Refresh per-run environment projection without rewriting project files."""
    bundle = bootstrap_bundle_for(worker)
    clean_room = (
        str(bundle.get("execution_policy") or "").strip()
        == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
    )
    _write_runtime_env(
        home_dir,
        {} if clean_room else bootstrap_env_for(worker, runtime_name),
        force_run_only_secrets=clean_room,
    )


def refresh_project_runtime_files_for_worker(home_dir: Path, workspace_dir: Path, worker: dict[str, Any]) -> None:
    """Refresh run-scoped MCP/client config without copying host auth or user files."""
    bundle = canonicalize_viventium_feeling_projection(
        bootstrap_bundle_for(worker)
    )
    from .native_context_projection import private_refresh
    if not private_refresh(home_dir, workspace_dir, worker, bundle):
        _write_claude_project_files(
            workspace_dir, bundle,
            grok_acp=str(worker.get("profile") or "") == "grok-build",
        )
        _write_codex_config(home_dir, bundle)
    from .workspace_files import materialize_managed_file
    for entry in bundle.get("files", []):
        if isinstance(entry, dict) and entry.get("managed_upload_id"):
            materialize_managed_file(workspace_dir, entry, worker)


_SANDBOX_REMOVE_MAX_REPLACEMENTS = 16

def _sandbox_directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )

def _remove_sandbox_entry_at(parent_fd: int, name: str) -> None:
    """Remove one entry relative to a trusted directory descriptor, never through a link."""

    for _ in range(_SANDBOX_REMOVE_MAX_REPLACEMENTS):
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return

        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            try:
                os.unlink(name, dir_fd=parent_fd)
            except FileNotFoundError:
                continue
            except IsADirectoryError:
                # The worker replaced the entry between lstat and unlink. Re-evaluate it without
                # ever resolving the replacement as a path.
                continue
            continue

        try:
            child_fd = os.open(
                name,
                _sandbox_directory_open_flags(),
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            continue
        except OSError as exc:
            # O_NOFOLLOW reports a replaced symlink as ELOOP on Unix. The next iteration removes
            # that link relative to the already-trusted parent descriptor.
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                continue
            raise

        try:
            opened_metadata = os.fstat(child_fd)
            for child_name in os.listdir(child_fd):
                _remove_sandbox_entry_at(child_fd, child_name)
        finally:
            os.close(child_fd)

        try:
            current_metadata = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if (current_metadata.st_dev, current_metadata.st_ino) != (
            opened_metadata.st_dev,
            opened_metadata.st_ino,
        ):
            # The opened directory was renamed after open. Its contents were cleaned through the
            # retained descriptor; loop to remove the replacement at the original sandbox name.
            continue
        try:
            os.rmdir(name, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        except OSError:
            # A concurrent sandbox process may have recreated a child. Re-open and clean the
            # current generation rather than falling back to a path-following recursive remover.
            continue
        return

    raise PermissionError("Sandbox authority path changed repeatedly during cleanup")

def _remove_sandbox_root_symlink(root: Path, name: str) -> None:
    """Remove a direct child symlink without resolving a replaced sandbox root."""

    if not name or name in {".", ".."} or Path(name).name != name:
        raise ValueError("Sandbox root entry must be one normalized path component")
    try:
        root_fd = os.open(root, _sandbox_directory_open_flags())
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PermissionError("Sandbox authority root must be a real directory") from exc
    try:
        try:
            metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISLNK(metadata.st_mode):
            return
        try:
            os.unlink(name, dir_fd=root_fd)
        except FileNotFoundError:
            return
        except IsADirectoryError as exc:
            # A worker replaced the link with a directory between lstat and unlink. Never recurse
            # through that unverified generation; the next safe write will fail closed if needed.
            raise PermissionError("Sandbox authority entry changed during cleanup") from exc
    finally:
        os.close(root_fd)

def _remove_sandbox_authority_path(root: Path, relative_path: str) -> None:
    """Remove one sandbox-owned path using only fd-relative, no-follow operations."""

    relative = Path(relative_path)
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ValueError("Sandbox authority path must be a normalized relative path")

    try:
        root_fd = os.open(root, _sandbox_directory_open_flags())
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PermissionError("Sandbox authority root must be a real directory") from exc

    descriptors = [root_fd]
    opened_ancestors: list[tuple[int, str, os.stat_result]] = []
    try:
        current_fd = root_fd
        for part in relative.parts[:-1]:
            try:
                child_fd = os.open(
                    part,
                    _sandbox_directory_open_flags(),
                    dir_fd=current_fd,
                )
            except FileNotFoundError:
                return
            except OSError as exc:
                try:
                    metadata = os.stat(
                        part,
                        dir_fd=current_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    return
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    _remove_sandbox_entry_at(current_fd, part)
                    return
                raise PermissionError("Sandbox authority ancestor could not be opened safely") from exc
            descriptors.append(child_fd)
            opened_ancestors.append((current_fd, part, os.fstat(child_fd)))
            current_fd = child_fd

        _remove_sandbox_entry_at(current_fd, relative.parts[-1])

        # An ancestor may have been renamed after its safe open and replaced with a symlink. Clean
        # that replacement at the trusted parent boundary so later bootstrap writes cannot follow
        # it outside the worker root. A real-directory replacement is ambiguous and fails closed.
        for parent_fd, part, opened_metadata in reversed(opened_ancestors):
            try:
                current_metadata = os.stat(
                    part,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            if (current_metadata.st_dev, current_metadata.st_ino) == (
                opened_metadata.st_dev,
                opened_metadata.st_ino,
            ):
                continue
            if stat.S_ISLNK(current_metadata.st_mode) or not stat.S_ISDIR(
                current_metadata.st_mode
            ):
                _remove_sandbox_entry_at(parent_fd, part)
                continue
            raise PermissionError("Sandbox authority ancestor changed during cleanup")
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)

def _clean_room_policy_marker_path(trusted_state_dir: Path | None) -> Path | None:
    return (
        trusted_state_dir / ".parallel-clean-room-v1"
        if trusted_state_dir is not None
        else None
    )

def _has_trusted_clean_room_policy_marker(trusted_state_dir: Path | None) -> bool:
    marker = _clean_room_policy_marker_path(trusted_state_dir)
    if marker is None:
        return False
    try:
        state_metadata = trusted_state_dir.lstat()
        marker_metadata = marker.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(state_metadata.st_mode) or stat.S_ISLNK(state_metadata.st_mode):
        return False
    if not stat.S_ISREG(marker_metadata.st_mode) or stat.S_ISLNK(marker_metadata.st_mode):
        return False
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(marker, flags)
    except OSError:
        return False
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (
            marker_metadata.st_dev,
            marker_metadata.st_ino,
        ):
            return False
        return os.read(descriptor, 128) == b"parallel-clean-room-v1\n"
    finally:
        os.close(descriptor)

def _record_clean_room_execution_policy(trusted_state_dir: Path | None) -> None:
    marker = _clean_room_policy_marker_path(trusted_state_dir)
    if marker is None:
        return
    trusted_state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = trusted_state_dir.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PermissionError("Clean-room trusted state must be a real directory")
    if _has_trusted_clean_room_policy_marker(trusted_state_dir):
        return
    if marker.exists() or marker.is_symlink():
        marker.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(marker, flags, 0o600)
    try:
        os.write(descriptor, b"parallel-clean-room-v1\n")
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def _invalidate_clean_room_execution_policy(trusted_state_dir: Path | None) -> None:
    """Make the next clean-room transition purge complete host-auth trees again."""

    marker = _clean_room_policy_marker_path(trusted_state_dir)
    if marker is None:
        return
    try:
        metadata = trusted_state_dir.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PermissionError("Clean-room trusted state must be a real directory")
    marker.unlink(missing_ok=True)

def _purge_clean_room_authority(
    home_dir: Path,
    workspace_dir: Path,
    trusted_state_dir: Path | None,
) -> None:
    if home_dir.is_symlink() or workspace_dir.is_symlink():
        raise PermissionError("Clean-room sandbox roots must not be symbolic links")
    if not _has_trusted_clean_room_policy_marker(trusted_state_dir):
        # A legacy host profile may have copied these complete trees. Their arbitrary hooks,
        # plugins, settings, and sessions cannot be distinguished from worker-created state, so
        # the one-time policy transition resets them completely. Later clean-room refreshes retain
        # worker session continuity while removing the credential/config paths above.
        _remove_sandbox_authority_path(home_dir, ".claude")
        _remove_sandbox_authority_path(home_dir, ".codex")
    _remove_sandbox_root_symlink(home_dir, ".glasshive")
    for relative_path in CLEAN_ROOM_HOME_AUTHORITY_PATHS:
        _remove_sandbox_authority_path(home_dir, relative_path)
    for relative_path in CLEAN_ROOM_WORKSPACE_AUTHORITY_PATHS:
        _remove_sandbox_authority_path(workspace_dir, relative_path)

def _atomic_write_text(path: Path, text: str, *, mode: int = 0o644) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(text)
    tmp_path.chmod(mode)
    tmp_path.replace(path)


def _write_env_file(path: Path, env: dict[str, str], *, mode: int = 0o644) -> None:
    if env:
        lines = [f"export {key}={shlex.quote(value)}" for key, value in sorted(env.items())]
        _atomic_write_text(path, "\n".join(lines) + "\n", mode=mode)
        return
    if path.exists():
        path.unlink()


def _write_runtime_env(
    home_dir: Path,
    env: dict[str, str],
    *,
    force_run_only_secrets: bool = False,
) -> None:
    glasshive_dir = home_dir / ".glasshive"
    glasshive_dir.mkdir(parents=True, exist_ok=True)
    runtime_env = glasshive_dir / "runtime.env"
    secret_env = glasshive_dir / "secret-runtime.env"
    secret_keys_path = glasshive_dir / "secret-runtime.keys"
    secret_env_values: dict[str, str] = {}
    shell_env_values = dict(env)
    if (
        force_run_only_secrets
        or (
            _enterprise_mode_enabled()
            and _worker_secret_env_exposure_mode() == "run-only"
        )
    ):
        secret_keys = _worker_secret_env_keys(env)
        secret_env_values = {key: value for key, value in env.items() if key in secret_keys}
        shell_env_values = {key: value for key, value in env.items() if key not in secret_keys}
    runtime_env_mode = 0o600 if _worker_secret_env_keys(shell_env_values) else 0o644
    if force_run_only_secrets:
        _write_clean_room_env_file(
            home_dir,
            Path(".glasshive/runtime.env"),
            shell_env_values,
            mode=runtime_env_mode,
        )
        _write_clean_room_env_file(
            home_dir,
            Path(".glasshive/secret-runtime.env"),
            secret_env_values,
            mode=0o600,
        )
        if secret_env_values:
            _write_clean_room_file(
                home_dir,
                Path(".glasshive/secret-runtime.keys"),
                ("\n".join(sorted(secret_env_values)) + "\n").encode("utf-8"),
                mode=0o600,
            )
        else:
            _unlink_clean_room_file(
                home_dir,
                Path(".glasshive/secret-runtime.keys"),
            )
        return
    _write_env_file(runtime_env, shell_env_values, mode=runtime_env_mode)
    _write_env_file(secret_env, secret_env_values, mode=0o600)
    if secret_env_values:
        _atomic_write_text(secret_keys_path, "\n".join(sorted(secret_env_values)) + "\n", mode=0o600)
    elif secret_keys_path.exists():
        secret_keys_path.unlink()
    bashrc = home_dir / ".bashrc"
    source_line = 'if [ -f "$HOME/.glasshive/runtime.env" ]; then source "$HOME/.glasshive/runtime.env"; fi'
    existing = bashrc.read_text() if bashrc.exists() else ""
    if source_line not in existing:
        prefix = existing.rstrip() + ("\n" if existing.strip() else "")
        bashrc.write_text(prefix + source_line + "\n")


def _safe_relative_path(raw_path: str) -> Path:
    relative = Path(raw_path.strip().lstrip("/"))
    if relative.is_absolute() or ".." in relative.parts or not str(relative):
        raise ValueError(f"Unsafe bootstrap path: {raw_path}")
    return relative


@contextmanager
def _sandbox_parent_descriptor(root: Path, relative: Path):
    """Open/create a target's parent chain without following worker-owned links."""

    if root.is_symlink():
        raise PermissionError("Clean-room file targets must not use symbolic links")
    root.mkdir(parents=True, exist_ok=True)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    descriptors: list[int] = []
    try:
        try:
            descriptor = os.open(root, directory_flags)
        except OSError as exc:
            raise PermissionError(
                "Clean-room file targets must not use symbolic links"
            ) from exc
        descriptors.append(descriptor)
        for part in relative.parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            try:
                descriptor = os.open(part, directory_flags, dir_fd=descriptor)
            except OSError as exc:
                raise PermissionError(
                    "Clean-room file targets must not use symbolic links"
                ) from exc
            descriptors.append(descriptor)
        yield descriptor, relative.name
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)

def _write_clean_room_file(
    root: Path,
    relative: Path,
    content: bytes,
    *,
    mode: int = 0o600,
) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_TRUNC
        | getattr(os, "O_NOFOLLOW", 0)
    )
    with _sandbox_parent_descriptor(root, relative) as (parent_descriptor, filename):
        try:
            descriptor = os.open(filename, flags, mode, dir_fd=parent_descriptor)
        except OSError as exc:
            raise PermissionError(
                "Clean-room file targets must not use symbolic links"
            ) from exc
        try:
            os.fchmod(descriptor, mode)
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

def _read_private_native_file(root: Path, relative: Path) -> str:
    """Read only the private regular inode opened through no-follow descriptors."""
    with _sandbox_parent_descriptor(root, relative) as (parent, name):
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return ""
        except OSError as exc:
            raise PermissionError("Private native configuration must be a regular unlinked file") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise PermissionError("Private native configuration must be a regular unlinked file")
            with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as source:
                return source.read()
        finally:
            os.close(descriptor)


def _write_private_native_file(root: Path, relative: Path, content: bytes, *, mode: int = 0o600) -> None:
    """Replace a private entry atomically; never truncate/chmod an existing inode.

    O_NOFOLLOW alone cannot protect a hard-linked inode. The target check rejects
    static links; a substitution after the check is replaced, never dereferenced.
    """
    import secrets

    with _sandbox_parent_descriptor(root, relative) as (parent, name):
        try:
            metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            metadata = None
        if metadata is not None and (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1):
            raise PermissionError("Private native configuration must be a regular unlinked file")
        temporary = ".native-config-" + secrets.token_hex(16)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
        try:
            os.fchmod(descriptor, mode)
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
            os.fsync(descriptor)
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def _write_clean_room_env_file(
    root: Path,
    relative: Path,
    env: dict[str, str],
    *,
    mode: int,
) -> None:
    if not env:
        _unlink_clean_room_file(root, relative)
        return
    content = (
        ("\n".join(f"export {key}={shlex.quote(value)}" for key, value in sorted(env.items())) + "\n").encode(
            "utf-8"
        )
        if env
        else b""
    )
    _write_clean_room_file(root, relative, content, mode=mode)

def _unlink_clean_room_file(root: Path, relative: Path) -> None:
    with _sandbox_parent_descriptor(root, relative) as (parent_descriptor, filename):
        try:
            os.unlink(filename, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass

def _ensure_clean_room_directory(root: Path, relative: Path) -> None:
    with _sandbox_parent_descriptor(root, relative) as (parent_descriptor, filename):
        try:
            os.mkdir(filename, mode=0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            pass
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
            os, "O_NOFOLLOW", 0
        )
        try:
            descriptor = os.open(filename, flags, dir_fd=parent_descriptor)
        except OSError as exc:
            raise PermissionError(
                "Clean-room file targets must not use symbolic links"
            ) from exc
        os.close(descriptor)

def _copy_clean_room_tree(source: Path, root: Path, relative: Path) -> None:
    _ensure_clean_room_directory(root, relative)
    for child in sorted(source.rglob("*")):
        child_relative = relative / child.relative_to(source)
        if child.is_symlink():
            raise PermissionError("Clean-room source trees must not contain symbolic links")
        if child.is_dir():
            _ensure_clean_room_directory(root, child_relative)
        elif child.is_file():
            _write_clean_room_file(
                root,
                child_relative,
                child.read_bytes(),
                mode=child.stat().st_mode & 0o700 or 0o600,
            )

def _source_path_from_entry(entry: dict[str, Any]) -> Path | None:
    for key in ("source_path", "local_path", "upload_path", "absolute_path", "filepath"):
        value = str(entry.get(key) or "").strip()
        if value:
            return Path(value).expanduser()
    return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _allowed_bootstrap_source_roots() -> list[tuple[Path, Path]]:
    raw = os.environ.get("WPR_BOOTSTRAP_SOURCE_ROOTS", "").strip()
    if not raw:
        return []
    roots: list[tuple[Path, Path]] = []
    for item in raw.split(os.pathsep):
        item = item.strip()
        if not item:
            continue
        lexical = Path(os.path.abspath(os.fspath(Path(item).expanduser())))
        try:
            roots.append((lexical, lexical.resolve(strict=True)))
        except FileNotFoundError:
            continue
    return roots


def _bootstrap_source_max_bytes() -> int | None:
    raw = os.environ.get("WPR_BOOTSTRAP_SOURCE_MAX_BYTES", "").strip()
    if not raw:
        return DEFAULT_BOOTSTRAP_SOURCE_MAX_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("WPR_BOOTSTRAP_SOURCE_MAX_BYTES must be a non-negative integer") from exc
    if value < 0:
        raise ValueError("WPR_BOOTSTRAP_SOURCE_MAX_BYTES must be a non-negative integer")
    return value


def _path_has_symlink_component(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _assert_source_size(path: Path, max_bytes: int | None) -> None:
    if path.is_dir():
        total = 0
        for child in path.rglob("*"):
            if child.is_symlink():
                raise PermissionError(f"Bootstrap source path must not contain symlinks: {child}")
            if child.is_file():
                total += child.stat().st_size
                if max_bytes is not None and total > max_bytes:
                    raise PermissionError(f"Bootstrap source path exceeds size limit: {path}")
        return
    if max_bytes is not None and path.stat().st_size > max_bytes:
        raise PermissionError(f"Bootstrap source path exceeds size limit: {path}")


def resolve_bootstrap_source_path(source: Path | str) -> Path:
    raw = Path(source).expanduser()
    if not raw.is_absolute():
        raise PermissionError(f"Bootstrap source path must be absolute: {source}")
    lexical = Path(os.path.abspath(os.fspath(raw)))
    roots = _allowed_bootstrap_source_roots()
    if not roots:
        raise PermissionError("Bootstrap source_path is disabled until WPR_BOOTSTRAP_SOURCE_ROOTS allows trusted roots")
    try:
        resolved = lexical.resolve(strict=True)
    except FileNotFoundError:
        raise FileNotFoundError(f"Bootstrap source file not found: {source}") from None
    allowed_root: Path | None = None
    for lexical_root, resolved_root in roots:
        lexical_allowed = _is_relative_to(lexical, lexical_root) or _is_relative_to(lexical, resolved_root)
        if lexical_allowed and _is_relative_to(resolved, resolved_root):
            allowed_root = lexical_root if _is_relative_to(lexical, lexical_root) else resolved_root
            break
    if allowed_root is None:
        raise PermissionError(f"Bootstrap source path is outside trusted roots: {source}")
    if _path_has_symlink_component(lexical, allowed_root):
        raise PermissionError(f"Bootstrap source path must not use symlinks: {source}")
    _assert_source_size(resolved, _bootstrap_source_max_bytes())
    return resolved


def _write_project_files(
    home_dir: Path,
    workspace_dir: Path,
    bundle: JsonDict,
    worker: dict[str, Any],
    copy_file: Callable[[Path, Path], None],
    copy_tree: Callable[[Path, Path], None],
) -> None:
    """Materialize `bundle["files"]` into the worker home or workspace.

    Supported entries:
    - inline text: `{"path": "uploads/note.txt", "content": "..."}`
    - inline bytes: `{"path": "uploads/file.pdf", "encoding": "base64", "content_base64": "..."}`
    - trusted source copy: `{"path": "uploads/file.pdf", "source_path": "/trusted/file.pdf"}`

    Enterprise source copies require a signed path token scoped to the same tenant/user.
    """
    def allows_empty(entry: dict[str, Any]) -> bool:
        value = entry.get("allow_empty")
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    def require_non_empty(rel_path: str, size: int, entry: dict[str, Any]) -> None:
        if size <= 0 and not allows_empty(entry):
            raise ValueError(f"Bootstrap file {rel_path} is empty; set allow_empty=true to materialize an empty file")

    clean_room = (
        str(bundle.get("execution_policy") or "").strip()
        == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
    )
    files = bundle.get("files")
    if not isinstance(files, list):
        return
    for entry in files:
        if not isinstance(entry, dict):
            continue
        if entry.get("managed_upload_id"):
            from .workspace_files import materialize_managed_file
            if str(entry.get("scope") or "workspace") != "workspace":
                raise ValueError("Managed files must belong to the workspace")
            materialize_managed_file(workspace_dir, entry, worker)
            continue
        scope = str(entry.get("scope") or "workspace").strip().lower()
        raw_path = str(entry.get("path") or "").strip()
        if not raw_path:
            filename = str(entry.get("filename") or entry.get("file_id") or "").strip()
            raw_path = f"uploads/{filename}" if filename else ""
        rel_path = raw_path.strip().lstrip("/")
        if not rel_path:
            continue
        root = home_dir if scope == "home" else workspace_dir
        relative_path = _safe_relative_path(rel_path)
        target = root / relative_path
        if not clean_room:
            target.parent.mkdir(parents=True, exist_ok=True)
        if str(entry.get("encoding") or "").strip().lower() == "base64" or "content_base64" in entry:
            raw = str(entry.get("content_base64") or entry.get("content") or "")
            try:
                decoded = base64.b64decode(raw, validate=True)
            except Exception as exc:
                raise ValueError(f"Invalid base64 bootstrap content for {rel_path}") from exc
            require_non_empty(rel_path, len(decoded), entry)
            if clean_room:
                _write_clean_room_file(root, relative_path, decoded)
            else:
                target.write_bytes(decoded)
            continue
        if "content" in entry:
            content = str(entry.get("content") or "")
            require_non_empty(rel_path, len(content.encode("utf-8")), entry)
            if clean_room:
                _write_clean_room_file(root, relative_path, content.encode("utf-8"))
            else:
                target.write_text(content)
            continue
        source = _source_path_from_entry(entry)
        if source is None:
            raise ValueError(f"Bootstrap file {rel_path} is missing content or source_path")
        source = resolve_authorized_bootstrap_source_path(entry, source, worker)
        if not source.exists():
            raise FileNotFoundError(f"Bootstrap source file not found: {source}")
        if source.is_dir():
            if clean_room:
                _copy_clean_room_tree(source, root, relative_path)
            else:
                copy_tree(source, target)
        else:
            require_non_empty(rel_path, source.stat().st_size, entry)
            if clean_room:
                _write_clean_room_file(
                    root,
                    relative_path,
                    source.read_bytes(),
                    mode=source.stat().st_mode & 0o700 or 0o600,
                )
            else:
                copy_file(source, target)


def _write_claude_project_files(
    workspace_dir: Path, bundle: JsonDict, *, private: bool = False,
    grok_acp: bool = False,
) -> None:
    """Write project-scoped Claude/Codex instruction and MCP files.

    Claude reads `.mcp.json` and `.claude/settings.local.json` from the project. Codex reads
    `AGENTS.md` and, for MCP, the worker-specific `.codex/config.toml` written under the worker
    home. The lower-case files are compatibility mirrors for older agents/tools.
    """
    clean_room = private or (
        str(bundle.get("execution_policy") or "").strip()
        == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
    )
    if private:
        # Validate every generated private target before changing any file. Actual
        # writes also use no-follow descriptors to reject substitution after this check.
        targets = [Path(name) for name in ("agents.md", "AGENTS.md", "claude.md", "CLAUDE.md", "codex.md", "CODEX.md")]
        if isinstance(bundle.get("claude_settings_local"), dict):
            targets.append(Path(".claude/settings.local.json"))
        if grok_acp or isinstance(bundle.get("claude_project_mcp"), dict):
            targets.append(Path(".mcp.json"))
        for relative in targets:
            with _sandbox_parent_descriptor(workspace_dir, relative) as (descriptor, filename):
                try:
                    info = os.stat(filename, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise PermissionError("Private configuration targets must be regular, unlinked files")
    write_file = _write_private_native_file if private else _write_clean_room_file
    workspace_dir.mkdir(parents=True, exist_ok=True)
    settings_local = bundle.get("claude_settings_local")
    if isinstance(settings_local, dict):
        content = json.dumps(settings_local, indent=2, sort_keys=True) + "\n"
        if clean_room:
            write_file(
                workspace_dir,
                Path(".claude/settings.local.json"),
                content.encode("utf-8"),
            )
        else:
            target = workspace_dir / ".claude" / "settings.local.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            target.chmod(0o600)

    # Grok receives selected servers through ACP session/new. Its disabled
    # Claude-compatibility importer also uses .mcp.json to reject duplicate
    # ACP HTTP URLs, so a generated Claude file would silently remove them.
    # This per-member file is empty for Grok; ACP stays the run-scoped authority.
    project_mcp = {"mcpServers": {}} if grok_acp else bundle.get("claude_project_mcp")
    if isinstance(project_mcp, dict):
        payload = _claude_project_mcp_payload(bundle, project_mcp)
        content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if clean_room:
            write_file(
                workspace_dir,
                Path(".mcp.json"),
                content.encode("utf-8"),
            )
        else:
            target = workspace_dir / ".mcp.json"
            target.write_text(content)
            target.chmod(0o600)

    agents_md = glasshive_project_agents_md(bundle)
    claude_md = glasshive_project_claude_md(bundle)
    codex_md = glasshive_project_codex_md(bundle)
    for filename, content in (
        ("agents.md", agents_md),
        ("AGENTS.md", agents_md),
        ("claude.md", claude_md),
        ("CLAUDE.md", claude_md),
        ("codex.md", codex_md),
        ("CODEX.md", codex_md),
    ):
        if clean_room:
            write_file(
                workspace_dir,
                Path(filename),
                content.encode("utf-8"),
                mode=0o644,
            )
        else:
            (workspace_dir / filename).write_text(content)


def _claude_project_mcp_payload(bundle: JsonDict, project_mcp: dict[str, Any]) -> JsonDict:
    """Normalize Claude MCP config and avoid embedding broker tokens in `.mcp.json`.

    Hosts may construct a Claude MCP payload with a literal bearer grant for convenience. Before the
    file hits disk, replace the literal grant with `${GLASSHIVE_CAPABILITY_BROKER_TOKEN}` whenever
    the same token is present in the scoped bootstrap env.
    """
    payload = project_mcp if isinstance(project_mcp.get("mcpServers"), dict) else {"mcpServers": project_mcp}
    payload = json.loads(json.dumps(payload))
    env = bootstrap_env_for({"bootstrap_bundle_json": bundle})
    grant = str(env.get(GLASSHIVE_CAPABILITY_BROKER_TOKEN_ENV) or "").strip()
    if not grant:
        return payload
    env_auth = f"Bearer ${{{GLASSHIVE_CAPABILITY_BROKER_TOKEN_ENV}}}"
    literal_auth = f"Bearer {grant}"
    servers = payload.get("mcpServers")
    if not isinstance(servers, dict):
        return payload
    for server_name, config in servers.items():
        if not isinstance(config, dict):
            continue
        if (
            str(bundle.get("execution_policy") or "").strip()
            == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
            and server_name == PARALLEL_CLEAN_ROOM_BROKER_NAME
        ):
            config["url"] = PARALLEL_CLEAN_ROOM_BROKER_PROXY_URL
        headers = config.get("headers")
        if not isinstance(headers, dict):
            continue
        if str(headers.get("Authorization") or "").strip() == literal_auth:
            headers["Authorization"] = env_auth
    return payload


def claude_project_mcp_payload_for_bundle(bundle: JsonDict, project_mcp: dict[str, Any]) -> JsonDict:
    """Public wrapper for host and sandbox bootstrap paths that write Claude `.mcp.json` files."""
    return _claude_project_mcp_payload(bundle, project_mcp)


def _write_codex_config(home_dir: Path, bundle: JsonDict, *, private: bool = False) -> None:
    """Append/refresh worker-local Codex MCP config without duplicating old server blocks."""
    append = bundle.get("codex_config_append")
    if not isinstance(append, str) or not append.strip():
        return
    clean_room = (
        str(bundle.get("execution_policy") or "").strip()
        == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
    )
    if clean_room:
        append = "\n".join(
            (
                f"[mcp_servers.{PARALLEL_CLEAN_ROOM_BROKER_NAME}]",
                f"url = {json.dumps(PARALLEL_CLEAN_ROOM_BROKER_PROXY_URL)}",
                (
                    "bearer_token_env_var = "
                    f"{json.dumps(GLASSHIVE_CAPABILITY_BROKER_TOKEN_ENV)}"
                ),
            )
        )
    if clean_room:
        project_mcp = bundle.get("claude_project_mcp")
        servers = project_mcp.get("mcpServers") if isinstance(project_mcp, dict) else None
        peer_mcp = servers.get("xperfect-peers") if isinstance(servers, dict) else None
        if isinstance(peer_mcp, dict) and "GLASSHIVE_PEER_TOKEN" in bundle.get("env", {}):
            append += (
                "\n\n[mcp_servers.xperfect-peers]\nurl = "
                + json.dumps(peer_mcp["url"])
                + '\nbearer_token_env_var = "GLASSHIVE_PEER_TOKEN"\n'
            )
    if clean_room:
        raw_context_mcp = bundle.get("claude_project_mcp") or {}
        context_mcp = (raw_context_mcp.get("mcpServers", raw_context_mcp)).get("xperfect-context")
        if isinstance(context_mcp, dict) and "GLASSHIVE_CONTEXT_TOKEN" in bundle.get("env", {}):
            append += "\n" + "\n".join(("[mcp_servers.xperfect-context]", "url = " + json.dumps(context_mcp["url"]), 'bearer_token_env_var = "GLASSHIVE_CONTEXT_TOKEN"')) + "\n"
    target = home_dir / ".codex" / "config.toml"
    if not clean_room and not private:
        target.parent.mkdir(parents=True, exist_ok=True)
    existing = "" if clean_room else (_read_private_native_file(home_dir, Path(".codex/config.toml")) if private else (target.read_text() if target.exists() else ""))
    mcp_names = _codex_mcp_server_names(append)
    if mcp_names:
        existing = _strip_codex_mcp_server_blocks(existing, mcp_names)
    prefix = existing.rstrip() + ("\n\n" if existing.strip() else "")
    content = prefix + append.strip() + "\n"
    if private:
        _write_private_native_file(home_dir, Path(".codex/config.toml"), content.encode("utf-8"))
    elif clean_room:
        _write_clean_room_file(
            home_dir,
            Path(".codex/config.toml"),
            content.encode("utf-8"),
        )
    else:
        target.write_text(content)
        target.chmod(0o600)


def _codex_mcp_server_names(config_text: str) -> set[str]:
    return {
        match.group(1).strip()
        for match in re.finditer(r"(?m)^\s*\[mcp_servers\.([^\]\s]+)\]\s*$", config_text)
        if match.group(1).strip()
    }


def _strip_codex_mcp_server_blocks(config_text: str, names: set[str]) -> str:
    if not config_text.strip() or not names:
        return config_text.rstrip()
    output: list[str] = []
    skipping = False
    for line in config_text.splitlines():
        section = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if section:
            section_name = section.group(1).strip()
            server_name = section_name[len("mcp_servers.") :].split(".", 1)[0].strip("\"'")
            skipping = section_name.startswith("mcp_servers.") and server_name in names
        if not skipping:
            output.append(line)
    return "\n".join(output).rstrip()


def _write_manifest(home_dir: Path, profile: str, bundle: JsonDict) -> None:
    glasshive_dir = home_dir / ".glasshive"
    glasshive_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "bootstrap_profile": profile,
        "execution_policy": str(bundle.get("execution_policy") or "").strip(),
        "bundle_keys": sorted(bundle.keys()),
        "env_keys": sorted(bootstrap_env_for({"bootstrap_bundle_json": bundle}).keys()),
        "file_count": len(bundle.get("files") or []) if isinstance(bundle.get("files"), list) else 0,
        "has_claude_project_mcp": isinstance(bundle.get("claude_project_mcp"), dict),
        "has_claude_settings_local": isinstance(bundle.get("claude_settings_local"), dict),
        "has_codex_config_append": bool(str(bundle.get("codex_config_append") or "").strip()),
    }
    content = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if str(bundle.get("execution_policy") or "").strip() == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY:
        _write_clean_room_file(
            home_dir,
            Path(".glasshive/bootstrap-manifest.json"),
            content.encode("utf-8"),
            mode=0o644,
        )
    else:
        (glasshive_dir / "bootstrap-manifest.json").write_text(content)
