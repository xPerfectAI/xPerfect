import MarkdownIt from './vendor/markdown-it-15.0.3.mjs';

// Native file references are transport identities, not browser URLs. The owning
// artifact actions provide authorized Open/Download links; do not guess a file
// binding from its displayed name or send an unresolved identity to /watch/.
export function createResultMarkdown() {
  const markdown = new MarkdownIt({ html: false, linkify: false, typographer: false });
  const nativeReference = (value) => String(value || '').toLowerCase().startsWith('artifact_sha256:');
  const decodedPath = (value) => {
    try { return decodeURIComponent(String(value || '')); } catch { return ''; }
  };
  markdown.core.ruler.after('inline', 'native-artifact-labels', (state) => {
    const artifacts = Array.isArray(state.env?.resultArtifacts) ? state.env.resultArtifacts : [];
    const aliases = new Map();
    const roots = new Set();
    for (const item of artifacts) {
      const url = String(item?.open_url || '');
      if (!url.startsWith('/') || url.startsWith('//') || /[\\\u0000-\u001f]/.test(url)) continue;
      for (const path of Array.isArray(item?.native_paths) ? item.native_paths : []) {
        if (typeof path === 'string' && path) aliases.set(path, url);
      }
      if (typeof item?.native_workspace_root === 'string' && item.native_workspace_root.length > 1 && item.native_workspace_root.startsWith('/')) {
        roots.add(item.native_workspace_root.replace(/\/$/, ''));
      }
    }
    for (const block of state.tokens) {
      const links = [];
      for (const token of block.children || []) {
        if (token.type === 'link_open') {
          const href = String(token.attrGet('href') || '');
          const path = decodedPath(href);
          const nativePath = path.startsWith('/workspace/') || path.startsWith('file:')
            || [...roots].some(root => path.startsWith(`${root}/`));
          const resolved = !nativeReference(href) && aliases.get(path);
          if (resolved) token.attrSet('href', resolved);
          token.hidden = nativeReference(href) || Boolean(nativePath && !resolved);
          links.push(token.hidden);
        } else if (token.type === 'link_close') {
          token.hidden = links.pop() === true;
        } else if (token.type === 'image' && nativeReference(token.attrGet('src'))) {
          token.type = 'text';
          token.tag = '';
          token.attrs = null;
          token.children = null;
        }
      }
    }
  });
  markdown.renderer.rules.link_open = (tokens, index, options, env, self) => {
    tokens[index].attrSet('target', '_blank');
    tokens[index].attrSet('rel', 'noopener noreferrer');
    return self.renderToken(tokens, index, options);
  };
  return markdown;
}
