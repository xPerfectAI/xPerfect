"""Human document errors without changing API responses or file authority."""
import re

from fastapi import HTTPException
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse


def install_file_preview_errors(app):
    def document(request):
        return (request.method in {"GET", "HEAD"} and "text/html" in request.headers.get("accept", "").lower()
                and request.query_params.get("inline") != "1" and bool(re.fullmatch(
                    r"/(?:v1/(?:link-refs/[^/]+|signed-links/[^/]+|workers/[^/]+/artifacts/open)|api/workspace/[^/]+/files/[^/]+/preview)",
                    request.url.path)))

    for error in (HTTPException, RequestValidationError):
        previous = app.exception_handlers.get(error, http_exception_handler)

        def wrapper(original):
            async def handle(request, exc):
                if not document(request):
                    return await original(request, exc)
                status = getattr(exc, "status_code", 422)
                title, message = {
                    401: ("This file link is unavailable", "The link may have expired. Open it from the workspace again, or ask the owner for a new link."),
                    403: ("You cannot open this file", "Open the workspace with an account that has access, or ask its owner for help."),
                    404: ("This file is no longer available", "Return to the workspace and choose an available file."),
                    409: ("This file has changed", "Close this tab and refresh Files, then open the file again."),
                    413: ("This file is too large to preview", "Return to the workspace and use its Download action."),
                    415: ("This format cannot be previewed here", "Return to the workspace and download the file to open it in its app."),
                    422: ("This preview link is incomplete", "Refresh Files and open the file again."),
                }.get(status, ("This preview is temporarily unavailable", "Return to the workspace and try again."))
                # Fixed copy only. Never interpolate exception details, private
                # filenames, source paths, tokens, or remote response bodies.
                html = f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · xPerfect</title>
<script src="/static/theme.js"></script><link rel="stylesheet" href="/static/theme.css">
<style>.reader-error{{max-width:960px;margin:auto;padding:clamp(20px,5vw,48px)}}.reader-error header{{font-size:18px;font-weight:650;margin-bottom:32px}}.reader-error section{{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:clamp(20px,4vw,32px)}}.reader-error h1{{font-size:clamp(22px,4vw,28px);line-height:1.3;margin:0 0 16px}}.reader-error p{{line-height:1.6;color:var(--muted);margin:0 0 24px}}.reader-error a{{color:var(--accent);text-underline-offset:3px;display:inline-flex;align-items:center;min-height:44px}}.reader-error a:focus-visible{{outline:2px solid var(--accent);outline-offset:4px}}</style></head>
<body><main class="reader-error"><header>xPerfect</header><section><h1>{title}</h1><p>{message}</p><a href="/" target="_top">View workspaces</a></section></main></body></html>'''
                return HTMLResponse(html, status_code=status, headers={
                    "Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer",
                    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "SAMEORIGIN",
                    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'",
                })
            return handle

        app.add_exception_handler(error, wrapper(previous))
