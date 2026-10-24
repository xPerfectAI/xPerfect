import MarkdownIt from './vendor/markdown-it-15.0.3.mjs';

// Native file references are transport identities, not browser URLs. The owning
// artifact actions provide authorized Open/Download links; do not guess a file
// binding from its displayed name or send an unresolved identity to /watch/.
export function createResultMarkdown() {
  const markdown = new MarkdownIt({ html: false, linkify: false, typographer: false });
  const nativeReference = (value) => String(value || '').toLowerCase().startsWith('artifact_sha256:');
  markdown.core.ruler.after('inline', 'native-artifact-labels', (state) => {
    for (const block of state.tokens) {
      const links = [];
      for (const token of block.children || []) {
        if (token.type === 'link_open') {
          token.hidden = nativeReference(token.attrGet('href'));
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
