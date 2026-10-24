/* Apply the saved appearance before styles paint. No account or runtime state. */
(() => {
  const key = 'xperfect.appearance';
  const allowed = new Set(['system', 'light', 'dark']);
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  let preference = 'system';
  try { const saved = localStorage.getItem(key); if (allowed.has(saved)) preference = saved; } catch { /* Browser storage is optional. */ }
  function apply() {
    document.documentElement.dataset.theme = preference === 'system' ? (media.matches ? 'dark' : 'light') : preference;
    document.querySelectorAll('[data-theme-picker]').forEach((picker) => { picker.checked = picker.value === preference; });
  }
  apply();
  document.addEventListener('DOMContentLoaded', () => {
    apply();
    const profile = document.getElementById('profile-menu');
    document.addEventListener('click', (event) => {
      if (profile?.open && !profile.contains(event.target)) profile.open = false;
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && profile?.open) {
        profile.open = false;
        profile.querySelector('summary')?.focus();
      }
    });
    profile?.querySelectorAll('a, .account-action').forEach((item) => item.addEventListener('click', () => { profile.open = false; }));
    document.querySelectorAll('[data-theme-picker]').forEach((picker) => picker.addEventListener('change', () => {
      if (!picker.checked || !allowed.has(picker.value)) return;
      preference = picker.value;
      try { localStorage.setItem(key, preference); } catch { /* Keep the choice for this page. */ }
      apply();
    }));
  });
  media.addEventListener('change', () => { if (preference === 'system') apply(); });
  window.addEventListener('storage', (event) => {
    if (event.key !== key && event.key !== null) return;
    preference = allowed.has(event.newValue) ? event.newValue : 'system';
    apply();
  });
})();
