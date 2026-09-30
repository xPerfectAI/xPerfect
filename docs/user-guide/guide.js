// System appearance is the default. An explicit visitor choice remains optional.
const root = document.documentElement;
const systemTheme = matchMedia('(prefers-color-scheme: dark)');
let appearance = 'system';
try {
  const stored = localStorage.getItem('xp-manual-theme');
  if (['system', 'light', 'dark'].includes(stored)) appearance = stored;
} catch { /* The manual remains readable without storage. */ }
function renderTheme() {
  root.dataset.appearance = appearance;
  const actual = appearance === 'system' ? (systemTheme.matches ? 'dark' : 'light') : appearance;
  document.querySelectorAll('.theme').forEach(button => {
    button.textContent = appearance === 'system' ? `◐ System · ${actual}` : `◐ ${appearance[0].toUpperCase()}${appearance.slice(1)}`;
    const next = { system: 'light', light: 'dark', dark: 'system' }[appearance];
    button.setAttribute('aria-label', `Appearance: ${appearance}. Switch to ${next}.`);
    button.title = 'Cycle system, light and dark appearance';
  });
}
renderTheme();
systemTheme.addEventListener('change', renderTheme);
document.querySelectorAll('.theme').forEach(button => {
  button.addEventListener('click', () => {
    appearance = { system: 'light', light: 'dark', dark: 'system' }[appearance];
    try { localStorage.setItem('xp-manual-theme', appearance); } catch { /* Keep the choice for this visit. */ }
    renderTheme();
  });
});
const tabs = [...document.querySelectorAll('[data-route]')];
function activateTab(selected) {
  tabs.forEach(tab => {
    tab.classList.toggle('active', tab === selected);
    tab.setAttribute('aria-selected', String(tab === selected));
    tab.tabIndex = tab === selected ? 0 : -1;
  });
  document.querySelectorAll('[data-panel]').forEach(panel => {
    panel.hidden = panel.dataset.panel !== selected.dataset.route;
  });
}
tabs.forEach((tab, index) => {
  tab.id = `tab-${tab.dataset.route}`;
  tab.setAttribute('aria-controls', `panel-${tab.dataset.route}`);
  const panel = document.querySelector(`[data-panel="${tab.dataset.route}"]`);
  panel.id = `panel-${tab.dataset.route}`;
  panel.setAttribute('role', 'tabpanel');
  panel.setAttribute('aria-labelledby', tab.id);
  tab.addEventListener('click', () => activateTab(tab));
  tab.addEventListener('keydown', event => {
    let next;
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
    if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = tabs.length - 1;
    if (next !== undefined) {
      event.preventDefault();
      activateTab(tabs[next]);
      tabs[next].focus();
    }
  });
});
activateTab(tabs[0]);
document.querySelectorAll('pre').forEach(block => {
  const button = document.createElement('button');
  button.className = 'copy';
  button.textContent = 'Copy';
  button.setAttribute('aria-label', 'Copy command');
  button.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(block.querySelector('code').textContent);
      button.textContent = 'Copied';
    } catch { button.textContent = 'Select text'; }
    setTimeout(() => { button.textContent = 'Copy'; }, 1800);
  });
  block.append(button);
});
const observer = new IntersectionObserver(entries => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      document.querySelectorAll('nav a').forEach(link => {
        link.classList.toggle('current', link.hash === `#${entry.target.id}`);
      });
    }
  });
}, { rootMargin: '-10% 0px -65% 0px' });
document.querySelectorAll('section').forEach(section => observer.observe(section));
