"""Worker-scoped terminal UI and authenticated private runtime WebSocket hop."""
from __future__ import annotations

import asyncio
import hmac
import re
import secrets
from urllib.parse import urlencode, urlsplit, urlunsplit

import anyio
import httpx
import websockets
from fastapi import HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse
from starlette.websockets import WebSocketDisconnect

# The live view names the exact run its terminal shows; the runtime checks it belongs here.
RUN_ID = re.compile(r'run_[A-Za-z0-9_-]{1,64}')


def install_terminal_routes(app, *, static_dir, client_for_request, identity_for_request,
                            restricted_identity, runtime_headers, runtime_base_url,
                            human_auth, session_for_request, uses_worker_view):
    def authorize(request, worker_id):
        if uses_worker_view(request, worker_id) or restricted_identity(identity_for_request(request, worker_id)):
            raise HTTPException(403, "An authenticated operator is required for terminal control")
        return client_for_request(request, worker_id, internal_details=True)

    @app.get('/ui/workers/{worker_id}/terminal', response_class=HTMLResponse)
    async def terminal_page(request: Request, worker_id: str):
        active_client = authorize(request, worker_id)
        try:
            await asyncio.to_thread(active_client.worker_live, worker_id)
        except httpx.HTTPStatusError as exc:
            raise HTTPException(404 if exc.response.status_code == 404 else 502, 'Terminal is unavailable') from exc
        nonce = secrets.token_urlsafe(24)
        html = (static_dir / 'terminal.html').read_text().replace('{{STYLE_NONCE}}', nonce)
        return HTMLResponse(html, headers={
            'Cache-Control': 'no-store',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'nonce-" + nonce + "'; "
                "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'; form-action 'self'",
        })

    @app.websocket('/ws/workers/{worker_id}/terminal')
    async def terminal_socket(websocket: WebSocket, worker_id: str):
        run_id = str(websocket.query_params.get('run') or '')
        try:
            if run_id and not RUN_ID.fullmatch(run_id):
                raise HTTPException(404, 'Run is unavailable')
            authorize(websocket, worker_id)
            # A browser WebSocket is a mutation surface. Check exact Origin and
            # the existing session CSRF token; neither secret goes into its URL.
            parts = urlsplit(str(websocket.url))
            expected_origin = urlunsplit(('https' if parts.scheme == 'wss' else 'http', parts.netloc, '', '', ''))
            if websocket.headers.get('origin') != expected_origin:
                raise HTTPException(403, 'Terminal origin is not allowed')
            protocols = [value.strip() for value in websocket.headers.get('sec-websocket-protocol', '').split(',')]
            if 'xperfect-terminal' not in protocols:
                raise HTTPException(403, 'Terminal protocol is required')
            if human_auth.session_enabled:
                session = session_for_request(websocket)
                supplied = [value[5:] for value in protocols if value.startswith('csrf.')]
                cookie = str(websocket.cookies.get('glasshive_csrf') or '')
                if (len(supplied) != 1 or not cookie or not hmac.compare_digest(cookie, supplied[0])
                        or not human_auth.session_csrf_valid(session, supplied[0])):
                    raise HTTPException(403, 'Terminal session verification failed')
            headers = runtime_headers(websocket, worker_id, role_override='operator')
        except HTTPException:
            await websocket.close(code=1008)
            return
        base = urlsplit(runtime_base_url())
        target = urlunsplit(('wss' if base.scheme == 'https' else 'ws', base.netloc,
                             '/ws/workers/' + worker_id + '/terminal',
                             urlencode({'run': run_id}) if run_id else '', ''))
        tasks = set()
        connection = None
        try:
            async with websockets.connect(target, additional_headers=headers, max_size=1024 * 1024) as upstream:
                connection = upstream
                await websocket.accept(subprotocol='xperfect-terminal')

                async def send_input():
                    while True:
                        await upstream.send(await websocket.receive_text())

                async def receive_output():
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)

                async def validate_session():
                    while True:
                        await asyncio.sleep(2)
                        # Password rotation, logout, expiry and revocation stop
                        # active terminal authority as well as the next request.
                        authorize(websocket, worker_id)

                tasks = {asyncio.create_task(send_input()), asyncio.create_task(receive_output()),
                         asyncio.create_task(validate_session())}
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        except (WebSocketDisconnect, websockets.ConnectionClosed):
            pass
        except Exception:
            try:
                await websocket.close(code=1011, reason='Terminal disconnected')
            except RuntimeError:
                pass
        finally:
            for task in tasks:
                task.cancel()
            # The runtime says why a run's terminal closed (not started yet, session ended,
            # saved output shown); pass that on so the page can explain it.
            code = getattr(connection, 'close_code', None)
            reason = str(getattr(connection, 'close_reason', '') or '') if code else ''
            with anyio.CancelScope(shield=True):
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                try:
                    if isinstance(code, int) and 4000 <= code < 5000:
                        await websocket.close(code=code, reason=reason)
                    else:
                        await websocket.close(code=1000)
                except RuntimeError:
                    pass
