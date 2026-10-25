"""Read-only file presentation; authorization and exact byte transport stay in the API."""
from __future__ import annotations

import csv
import mimetypes
import os
import re
from html import escape
from itertools import islice
from io import StringIO
from pathlib import Path
from urllib.parse import urlparse

from markdown_it import MarkdownIt


INLINE_MEDIA_TYPES = frozenset({
    "application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp",
    "image/avif", "image/bmp", "audio/mpeg", "audio/mp4", "audio/ogg",
    "audio/wav", "audio/x-wav", "audio/webm", "audio/flac", "audio/aac",
    "video/mp4", "video/webm", "video/ogg", "video/quicktime",
})
MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})
TABLE_TYPES = {"text/csv": ",", "application/csv": ",", "text/tab-separated-values": "\t"}
HTML_PREVIEW_POLICY = (
    "default-src 'none'; script-src 'none'; connect-src 'none'; object-src 'none'; "
    "form-action 'none'; img-src data:; style-src 'unsafe-inline'; frame-src 'none'; base-uri 'none'"
)
PREVIEW_SECURITY_HEADERS = {
    "Cache-Control": "no-store, no-cache, private, max-age=0",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'none'; connect-src 'none'; object-src 'none'; "
        "form-action 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'; "
        "frame-src 'self'; base-uri 'none'; frame-ancestors 'self'"
    ),
    "Pragma": "no-cache", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "SAMEORIGIN",
}
INLINE_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'none'; object-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'",
    "Referrer-Policy": "no-referrer", "X-Frame-Options": "SAMEORIGIN",
}
TEXT_SUFFIXES = frozenset({
    ".css", ".csv", ".htm", ".html", ".ini", ".js", ".json", ".log", ".md",
    ".markdown", ".py", ".sh", ".sql", ".toml", ".ts", ".tsv", ".txt",
    ".xml", ".yaml", ".yml",
})


def preview_media_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or ("text/plain" if Path(name).suffix.lower() in TEXT_SUFFIXES else "application/octet-stream")


def is_text_preview(name: str, media_type: str) -> bool:
    return (Path(name).suffix.lower() in TEXT_SUFFIXES or media_type.startswith("text/")
            or media_type in {"application/json", "application/xml", "application/x-yaml", "image/svg+xml"})


def preview_text_limit() -> int:
    return max(4096, min(int(os.environ.get("GLASSHIVE_ARTIFACT_PREVIEW_MAX_BYTES", str(512 * 1024))), 5 * 1024 * 1024))


def preview_appearance(request) -> str:
    # Presentation only: this enum never grants access. The proxy projects its
    # own origin's preference; direct readers use only the configured UI port.
    allowed = {"system", "light", "dark"}
    projected = request.headers.get("x-xperfect-appearance", "")
    if projected in allowed:
        return projected
    operator = os.environ.get("GLASSHIVE_OPERATOR_BASE_URL") or os.environ.get("WPR_OPERATOR_BASE_URL") or str(request.base_url)
    parsed = urlparse(operator)
    port = str(parsed.port or (443 if parsed.scheme == "https" else 80))
    value = request.cookies.get(f"xperfect_appearance_{port}", "")
    return value if value in allowed else "system"


def is_preview_document(request) -> bool:
    return (request.method in {"GET", "HEAD"} and "text/html" in request.headers.get("accept", "").lower()
            and request.query_params.get("inline") != "1" and bool(re.fullmatch(
                r"/v1/(?:link-refs/[^/]+|signed-links/[^/]+|workers/[^/]+/(?:artifacts/open|files/[^/]+/preview))",
                request.url.path)))


PREVIEW_ERRORS = {
    401: ("This file link is unavailable", "The link may have expired. Open it from the workspace again, or ask the owner for a new link."),
    403: ("You cannot open this file", "Open the workspace with an account that has access, or ask its owner for help."),
    404: ("This file is no longer available", "Return to the workspace and choose an available file."),
    409: ("This file has changed", "Close this tab and refresh Files, then open the file again."),
    413: ("This file is too large to preview", "Return to the workspace and use its Download action."),
    415: ("This format cannot be previewed here", "Return to the workspace and download the file to open it in its app."),
    422: ("This preview link is incomplete", "Refresh Files and open the file again."),
}


def install_preview_error_handlers(app) -> None:
    """Wrap only document reads; preserve existing JSON/provider error owners."""
    from fastapi import HTTPException
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import HTMLResponse
    from .workspace_files import FileAdmissionError

    for error in (HTTPException, RequestValidationError, FileAdmissionError):
        previous = app.exception_handlers.get(error)
        def wrapper(original):
            async def handle(request, exc):
                if is_preview_document(request):
                    status = getattr(exc, "status_code", 422)
                    title, _ = PREVIEW_ERRORS.get(status, ("This preview is temporarily unavailable", "Return to the workspace and try again."))
                    html = render_artifact_page(display_name=title, suffix="", media_type="", content=b"", size=0,
                        download_url="", workspace_url=os.environ.get("GLASSHIVE_OPERATOR_BASE_URL") or "/",
                        inline_url="", text_limit=4096, appearance=preview_appearance(request), error_status=status)
                    return HTMLResponse(html, status_code=status, headers=PREVIEW_SECURITY_HEADERS)
                return await original(request, exc)
            return handle
        # All three error types already have an owning handler on the API.
        if previous is not None:
            app.add_exception_handler(error, wrapper(previous))


def _markdown(text: str) -> str:
    # Python's CommonMark preset permits raw HTML. Use explicit options, as in
    # the existing JS result renderer, and the built-in table/strikethrough rules.
    renderer = MarkdownIt("default", {"html": False, "linkify": False, "typographer": False})

    def transport_labels(state):
        for block in state.tokens:
            links = []
            for token in block.children or []:
                if token.type == "link_open":
                    destination = urlparse(str(token.attrGet("href") or ""))
                    token.hidden = not ((destination.scheme.lower() in {"http", "https"} and destination.netloc)
                                        or (destination.scheme.lower() == "mailto" and destination.path))
                    links.append(token.hidden)
                elif token.type == "link_close":
                    token.hidden = links.pop() if links else False

    renderer.core.ruler.after("inline", "native-artifact-labels", transport_labels)

    def link_open(self, tokens, index, options, env):
        tokens[index].attrSet("target", "_blank")
        tokens[index].attrSet("rel", "noopener noreferrer")
        return self.renderToken(tokens, index, options, env)

    def image(self, tokens, index, options, env):
        # A relative filename is not an authorized file binding. Do not fetch
        # neighboring files or tracking pixels while reading an untrusted report.
        label = self.renderInlineAsText(tokens[index].children or [], options, env)
        return f'<span class="image-label">Image not shown: {escape(label or "Image")}</span>'

    renderer.add_render_rule("link_open", link_open)
    renderer.add_render_rule("image", image)
    return renderer.render(text)


def _table(text: str, delimiter: str, *, byte_truncated: bool = False) -> tuple[str, str]:
    incomplete = False
    if byte_truncated and text and not text.endswith(("\n", "\r")):
        # csv.reader owns quoted records. Remove a cut physical line first;
        # if it belongs to a multiline record the reader rejects that final
        # record, while earlier complete records can still be displayed.
        lines = text.splitlines(keepends=True)
        text = "".join(lines[:-1])
        incomplete = True
    rows = []
    try:
        for row in islice(csv.reader(StringIO(text, newline=""), delimiter=delimiter, strict=True), 202):
            rows.append(row)
    except (csv.Error, ValueError):
        if not byte_truncated or not rows:
            return "", "This table could not be read. Its source is shown below."
        incomplete = True
    if not rows or not any(rows):
        if byte_truncated:
            return "", "No complete table record fits this preview. Download the complete table."
        return '<p class="notice">This file is empty.</p>', ""
    columns = min(40, max(len(row) for row in rows))
    cells = lambda row, tag: "".join(f"<{tag}>{escape(value)}</{tag}>" for value in (row + [""] * columns)[:columns])
    header = f"<thead><tr>{cells(rows[0], 'th')}</tr></thead>"
    body = "".join(f"<tr>{cells(row, 'td')}</tr>" for row in rows[1:201])
    result = f'<div class="table-scroll" tabindex="0" aria-label="File table"><table>{header}<tbody>{body}</tbody></table></div>'
    notes = []
    if len(rows) > 201:
        notes.append("Showing the first 200 data rows.")
    if any(len(row) > 40 for row in rows):
        notes.append("Showing the first 40 columns.")
    if incomplete:
        notes.append("An incomplete final table record is omitted.")
    note = " ".join(notes)
    return result, note


def _source(text: str, *, disclosure: bool = False) -> str:
    source = f'<pre class="artifact-preview">{escape(text)}</pre>'
    return f'<details class="source-preview"><summary>View source</summary>{source}</details>' if disclosure else source


def render_artifact_page(*, display_name: str, suffix: str, media_type: str, content: bytes,
                         size: int, download_url: str, workspace_url: str, inline_url: str,
                         text_limit: int, appearance: str = "system", error_status: int | None = None) -> str:
    """Render bounded content with no executable artifact markup in the host page."""
    suffix = suffix.lower()
    appearance = appearance if appearance in {"light", "dark"} else "system"
    text = content[:text_limit].decode("utf-8", errors="replace")
    truncated = size > text_limit
    note = "Preview is shortened by the text size limit. Download file for the complete content." if truncated else ""
    kind = "File"
    source = ""
    if error_status is not None:
        kind = "File preview"
        _, message = PREVIEW_ERRORS.get(error_status, ("This preview is temporarily unavailable", "Return to the workspace and try again."))
        preview = f'<p>{escape(message)}</p>'
        note = ""
    elif media_type == "text/html" and suffix in {".html", ".htm"}:
        kind = "Web page"
        if truncated:
            preview = '<p class="notice">This page is too large for a safe preview. Download it to open the complete file.</p>'
            note = ""
        else:
            # The nested policy remains stricter than the host's media policy.
            # It blocks even same-origin subresources inside untrusted HTML.
            document = f'<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="{escape(HTML_PREVIEW_POLICY, quote=True)}"></head><body>{text}</body></html>'
            preview = ('<iframe class="html-preview" sandbox credentialless referrerpolicy="no-referrer" '
                       f'title="Rendered preview of {escape(display_name, quote=True)}" srcdoc="{escape(document, quote=True)}"></iframe>')
            source = _source(text, disclosure=True)
    elif suffix in MARKDOWN_SUFFIXES or media_type in {"text/markdown", "text/x-markdown"}:
        kind = "Document"
        preview = f'<article class="document-preview rendered-result">{_markdown(text)}</article>' if text.strip() else '<p class="notice">This document is empty.</p>'
        source = _source(text, disclosure=True)
    elif suffix in {".csv", ".tsv"} or media_type in TABLE_TYPES:
        kind = "Table"
        delimiter = "\t" if suffix == ".tsv" else TABLE_TYPES.get(media_type, ",")
        preview, table_note = _table(text, delimiter, byte_truncated=truncated)
        note = " ".join(value for value in (note, table_note) if value)
        source = _source(text, disclosure=bool(preview))
    elif media_type == "application/pdf":
        kind = "PDF document"
        preview = (f'<iframe class="pdf-preview" referrerpolicy="no-referrer" title="{escape(display_name, quote=True)}" src="{escape(inline_url, quote=True)}"></iframe>'
                   f'<p class="notice">If the PDF does not appear, <a href="{escape(inline_url, quote=True)}" target="_blank" rel="noopener noreferrer">open the PDF in a new tab</a> or download it.</p>')
        note = ""
    elif media_type in INLINE_MEDIA_TYPES:
        note = ""
        if media_type.startswith("image/"):
            kind = "Image"
            preview = f'<img class="image-preview" src="{escape(inline_url, quote=True)}" alt="{escape(display_name, quote=True)}" />'
        else:
            tag = "audio" if media_type.startswith("audio/") else "video"
            kind = "Audio" if tag == "audio" else "Video"
            preview = (f'<{tag} class="media-preview" controls preload="metadata" src="{escape(inline_url, quote=True)}">'
                       f'Your browser cannot play this file. <a href="{escape(download_url, quote=True)}">Download file</a>.</{tag}>'
                       '<p class="notice">Playback depends on the formats your browser supports. Download to use another app.</p>')
    elif media_type.startswith("text/") or media_type in {"application/json", "application/xml", "application/x-yaml", "image/svg+xml"} or suffix in {".js", ".ts", ".py", ".sh", ".sql", ".toml", ".yaml", ".yml", ".ini", ".log"}:
        kind = "Text file" if suffix in {".txt", ".log"} else "Source file"
        preview = _source(text) if text else '<p class="notice">This file is empty.</p>'
    else:
        note = ""
        preview = '<section class="no-preview"><h2>Your file is ready</h2><p>Download this file to open it in its app. A browser preview is not available for this format.</p></section>'
    notice = f'<p class="notice" role="status">{escape(note)}</p>' if note else ""
    download_action = f'<a class="button" download href="{escape(download_url, quote=True)}">Download file</a>' if download_url else ""
    workspace_label = "View workspaces" if error_status is not None else "View workspace"
    file_details = (f'<details class="file-details"><summary>File details</summary><dl><dt>Format</dt><dd>{escape(media_type)}</dd><dt>Size</dt><dd>{size:,} bytes</dd></dl></details>' if error_status is None else "")
    return f'''<!doctype html>
<html lang="en" data-theme="{appearance}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(display_name)} · xPerfect</title><style>
:root{{color-scheme:light dark;--bg:light-dark(#f4f5f7,#151618);--panel:light-dark(#ffffff,#1c1e21);--text:light-dark(#17191e,#e8eaed);--muted:light-dark(#555a62,#bdc2ca);--line:light-dark(#d4d6da,#50565e);--accent:light-dark(#786248,#d8c3a5);--control-bg:light-dark(#f7f8fa,#25282c);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}}
:root[data-theme="light"]{{color-scheme:light}}:root[data-theme="dark"]{{color-scheme:dark}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text)}}
.file-shell{{max-width:1360px;margin:auto;padding:clamp(16px,3vw,32px)}}.topbar{{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:24px}}.brand{{font-size:18px;font-weight:650;letter-spacing:-.025em}}.actions{{display:flex;gap:10px;flex-wrap:wrap}}
a{{color:var(--accent);text-underline-offset:3px}}a.button{{display:inline-flex;align-items:center;justify-content:center;min-height:42px;padding:10px 16px;border:1px solid var(--line);border-radius:12px;background:var(--text);color:var(--panel);font-size:14px;font-weight:600;text-decoration:none}}a.secondary{{background:var(--panel);color:var(--text)}}:is(a,summary,[tabindex]):focus-visible{{outline:2px solid var(--accent);outline-offset:3px}}
.file-heading{{margin-bottom:20px}}.file-kind{{margin:0 0 6px;color:var(--muted);font-size:13px}}.file-title{{margin:0;font-size:clamp(20px,3vw,28px);font-weight:600;line-height:1.3;overflow-wrap:anywhere}}
.preview{{padding:clamp(16px,3vw,32px);border:1px solid var(--line);border-radius:16px;background:var(--panel);min-width:0}}.document-preview{{max-width:960px;margin:auto;line-height:1.65;overflow-wrap:anywhere}}.document-preview>:first-child{{margin-top:0}}.document-preview>:last-child{{margin-bottom:0}}.document-preview h1{{font-size:clamp(26px,4vw,32px);line-height:1.25}}.document-preview h2{{font-size:24px;line-height:1.3}}.document-preview h3{{font-size:20px}}.document-preview :is(h1,h2,h3,h4){{margin:1.1em 0 .5em;font-weight:600}}.document-preview p{{margin:0 0 1em}}.document-preview blockquote{{margin:1em 0;padding-left:18px;border-left:3px solid var(--line);color:var(--muted)}}.document-preview :is(ul,ol){{padding-left:1.6em}}
table{{width:100%;border-collapse:collapse;font-size:14px;line-height:1.5}}th,td{{padding:10px 12px;border:1px solid var(--line);text-align:left;vertical-align:top;overflow-wrap:anywhere}}th{{background:var(--control-bg);font-weight:600}}.document-preview table{{display:block;overflow-x:auto;max-width:100%;margin:1em 0}}.table-scroll{{overflow:auto;max-height:70vh;max-width:100%}}.table-scroll table{{min-width:400px}}.table-scroll thead{{position:sticky;top:0}}
pre{{white-space:pre;overflow:auto;max-width:100%;margin:0;padding:16px;border:1px solid var(--line);border-radius:12px;background:var(--control-bg);font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace}}.artifact-preview{{white-space:pre-wrap;overflow-wrap:anywhere}}code{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.9em}}pre code{{font:inherit}}.source-preview,.file-details{{margin-top:20px}}summary{{cursor:pointer;color:var(--muted);font-size:13px;padding:8px 0}}.source-preview[open] summary{{margin-bottom:8px}}.file-details dl{{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;font-size:13px;color:var(--muted)}}.file-details dd{{margin:0;overflow-wrap:anywhere}}
.notice{{font-size:13px;line-height:1.6;color:var(--muted);margin:16px 0 0}}.no-preview h2{{font-size:20px;margin:0 0 8px}}.no-preview p{{margin:0;line-height:1.6;color:var(--muted)}}.image-preview{{display:block;max-width:100%;max-height:75vh;object-fit:contain;margin:auto}}.media-preview{{display:block;width:100%;max-height:75vh}}.html-preview,.pdf-preview{{display:block;width:100%;height:72vh;min-height:280px;border:1px solid var(--line);border-radius:8px;background:white}}
@media(max-width:600px){{.actions{{width:100%}}.actions .button{{flex:1}}.preview{{padding:16px 12px}}th,td{{padding:8px}}.file-details dl{{grid-template-columns:1fr}}}}
</style></head><body><main class="file-shell"><header class="topbar"><span class="brand">xPerfect</span><nav class="actions" aria-label="File actions">{download_action}<a class="button secondary" href="{escape(workspace_url, quote=True)}" target="_top" rel="noopener noreferrer">{workspace_label}</a></nav></header>
<div class="file-heading"><p class="file-kind">{kind}</p><h1 class="file-title">{escape(display_name)}</h1></div>
<section class="preview" aria-label="File preview">{notice}{preview}{source}</section>
{file_details}
</main></body></html>'''
