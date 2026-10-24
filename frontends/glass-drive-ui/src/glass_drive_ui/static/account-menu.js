/* Present the existing authenticated session without adding an auth flow. */
export function renderAccountIdentity(identity = {}, authenticated = false, root = document) {
  const control = root.getElementById('current-user-control');
  const label = root.getElementById('current-user-label');
  const status = root.getElementById('profile-menu-status');
  if (label) {
    label.textContent = authenticated
      ? [identity.display_name, identity.email].filter(Boolean).join(' · ') || 'Signed in'
      : 'Local access';
    label.title = label.textContent;
  }
  if (control) control.hidden = false;
  const switchAccount = root.getElementById('switch-account');
  const signOut = root.getElementById('local-sign-out');
  if (switchAccount) switchAccount.hidden = !authenticated || identity.provider_switch_visible !== true;
  if (signOut) signOut.hidden = !authenticated;
  if (status) {
    status.hidden = authenticated;
    status.textContent = authenticated ? '' : 'This host does not require sign-in.';
  }
}
