"""Recognizable credentials shared by output and failure-diagnostic redaction paths."""

import re
from typing import Callable


def _redact_pair_or_preserve_uri_authority(match: re.Match[str]) -> str:
    # Consume a syntactically valid URI host:port before scanning its path, so a long
    # path is not mistaken for the secret half. Path/query credentials still scan.
    uri_authority = match.group("uri_authority")
    if uri_authority:
        return uri_authority
    value = match.group(0)
    # This is a content-addressed, public artifact identity, not a credential.
    # Preserve only the exact typed form; arbitrary label:value pairs still
    # fail closed through the generic credential rule below.
    if re.fullmatch(r"artifact_sha256:[a-f0-9]{64}", value):
        return value
    return "[REDACTED_CREDENTIAL]"


# Preserve the pre-existing generic credential-pair family as well as known formats.
# Unquoted ID:secret text is ambiguous; do not guess safety from an arbitrary label.
RedactionRule = tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]]
CREDENTIAL_REDACTIONS: tuple[RedactionRule, ...] = (
    (
        re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/\s<>@\"']*:[^/\s<>@\"']+@"),
        r"\1[REDACTED_CREDENTIAL]@",
    ),
    (
        re.compile(r"(?i)(?<![A-Za-z0-9_])(bot)?[0-9]{6,}:[A-Za-z0-9_-]{30,}"),
        r"\1[REDACTED_CREDENTIAL]",
    ),
    (
        re.compile(
            r"(?P<uri_authority>[a-zA-Z][a-zA-Z0-9+.-]*://[a-zA-Z0-9.-]+:[0-9]{1,5}(?=[/?#\s]|$))"
            r"|\b[A-Za-z0-9_]{8,}:[A-Za-z0-9_./+=-]{20,}\b"
        ),
        _redact_pair_or_preserve_uri_authority,
    ),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[REDACTED_AWS_ACCESS_KEY]"),
    (re.compile(r"\bghp_[A-Za-z0-9_]{8,}\b"), "ghp_[REDACTED]"),
    (re.compile(r"\bxoxb-[A-Za-z0-9-]{8,}\b"), "xoxb-[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "[REDACTED_JWT]"),
    (re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*\Z"), "[REDACTED_PRIVATE_KEY]"),
)
