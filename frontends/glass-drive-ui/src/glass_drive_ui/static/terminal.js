(() => {
  'use strict';
  const workerId = location.pathname.split('/')[3];
  const status = document.getElementById('terminal-status');
  const host = document.getElementById('terminal');
  const nonce = document.querySelector('meta[name="terminal-style-nonce"]').content;
  // xterm's documented documentOverride keeps its two generated stylesheet
  // elements inside this response's nonce policy. Vendor bytes remain intact.
  const terminalDocument = new Proxy(document, { get(target, key) {
    if (key === 'createElement') return (...args) => {
      const element = target.createElement(...args);
      if (String(args[0]).toLowerCase() === 'style') element.nonce = nonce;
      return element;
    };
    const value = Reflect.get(target, key, target);
    return typeof value === 'function' ? value.bind(target) : value;
  }});
  const terminal = new Terminal({ documentOverride: terminalDocument, convertEol: true,
    cursorBlink: true, fontSize: 14, fontFamily: 'Menlo, Monaco, Consolas, monospace',
    theme: { background: '#10151b', foreground: '#e4e8ed' } });
  terminal.open(host);
  let socket;
  const csrf = () => decodeURIComponent(document.cookie.split('; ').find(item => item.startsWith('glasshive_csrf='))?.slice(15) || '');
  function resize() {
    const cols = Math.max(20, Math.floor((host.clientWidth - 20) / 8.45));
    const rows = Math.max(4, Math.floor((host.clientHeight - 20) / 17));
    terminal.resize(cols, rows);
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'resize', cols, rows }));
  }
  function connect() {
    if (socket) socket.close();
    status.textContent = 'Connecting…';
    const protocols = ['xperfect-terminal'];
    if (csrf()) protocols.push(`csrf.${csrf()}`);
    const next = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/workers/${encodeURIComponent(workerId)}/terminal`, protocols);
    socket = next;
    next.onopen = () => { if (socket !== next) return; status.textContent = 'Terminal connected'; resize(); terminal.focus(); };
    next.onmessage = event => { if (socket === next) terminal.write(event.data); };
    next.onclose = () => { if (socket === next) status.textContent = 'Terminal disconnected. Unlock again if your session expired, then reconnect.'; };
    next.onerror = () => { if (socket === next) status.textContent = 'Terminal unavailable. Check the workspace status and reconnect.'; };
  }
  terminal.onData(data => { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'input', data })); });
  new ResizeObserver(resize).observe(host);
  document.getElementById('reconnect').addEventListener('click', connect);
  for (const button of document.querySelectorAll('[data-action]')) button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const response = await fetch(`/api/worker/${encodeURIComponent(workerId)}/action/${button.dataset.action}`, {
        method: 'POST', headers: { 'X-GlassHive-CSRF': csrf() }
      });
      if (!response.ok) throw new Error('Action unavailable. Check your session and workspace status.');
      status.textContent = 'Action accepted';
      if (button.dataset.action === 'resume') connect();
    } catch (error) { status.textContent = error.message; }
    finally { button.disabled = false; }
  });
  connect();
})();
