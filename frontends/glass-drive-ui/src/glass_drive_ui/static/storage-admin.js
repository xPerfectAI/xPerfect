const GB = 1_000_000_000;

function byteLimit(value, optional = false) {
  const raw = String(value || '').trim();
  if (optional && !raw) return null;
  const gb = Number(raw);
  if (!raw || !Number.isFinite(gb) || gb < 0 || !Number.isSafeInteger(Math.round(gb * GB))) {
    throw new Error('Enter a valid limit in GB.');
  }
  return Math.round(gb * GB);
}

export async function initializeStorageAdmin({ identity, getJson, patchJson }) {
  const card = document.getElementById('storage-admin-card');
  if (!card || identity?.role !== 'tenant_admin') return;
  const form = document.getElementById('storage-admin-form');
  const user = document.getElementById('storage-admin-user');
  const limit = document.getElementById('storage-admin-limit');
  const fileLimit = document.getElementById('storage-admin-file-limit');
  const usage = document.getElementById('storage-admin-usage');
  const status = document.getElementById('storage-admin-status');
  const save = document.getElementById('storage-admin-save');
  const reset = document.getElementById('storage-admin-reset');
  card.hidden = false;
  form.hidden = true;
  status.textContent = 'Loading people…';
  let generation = 0;
  let loadedOwner = null;
  const selectedPath = () => `/api/admin/users/${encodeURIComponent(user.value)}/storage`;
  const busy = (value) => {
    user.disabled = value;
    limit.disabled = value;
    fileLimit.disabled = value;
    save.disabled = value || loadedOwner !== user.value;
    reset.disabled = value || loadedOwner !== user.value;
  };
  const show = (policy) => {
    limit.value = policy.storage_limit_bytes == null ? '' : String(policy.storage_limit_bytes / GB);
    fileLimit.value = policy.max_file_bytes == null ? '' : String(policy.max_file_bytes / GB);
    const used = Number(policy.used_logical_bytes || 0);
    usage.textContent = `${(used / GB).toFixed(2)} GB used · ${policy.storage_limit_bytes == null ? 'no storage limit' : policy.native_hard_enforcement ? 'storage enforced' : 'limit tracked'}`;
    status.textContent = '';
  };
  async function load() {
    const current = ++generation;
    if (!user.value) return;
    loadedOwner = null;
    limit.value = '';
    fileLimit.value = '';
    usage.textContent = '';
    busy(true);
    status.textContent = 'Loading storage…';
    try {
      const policy = await getJson(selectedPath(), 'Could not load storage');
      if (current === generation) {
        loadedOwner = user.value;
        show(policy);
      }
    } catch (error) {
      if (current === generation) status.textContent = error.message;
    } finally {
      if (current === generation) busy(false);
    }
  }
  try {
    const result = await getJson('/api/admin/users?limit=500', 'Could not load people');
    const people = result.items || [];
    user.replaceChildren(...people.map((person) => new Option(
      `${person.display_name || person.email || person.user_id}${person.disabled ? ' (disabled)' : ''}`,
      person.user_id,
    )));
    if (!people.length) {
      status.textContent = 'No people are available yet.';
      return;
    }
    form.hidden = false;
    user.addEventListener('change', load);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      try {
        const payload = {
          storage_limit_bytes: byteLimit(limit.value, true),
          max_file_bytes: byteLimit(fileLimit.value, true),
        };
        busy(true);
        status.textContent = 'Saving limits…';
        show(await patchJson(selectedPath(), payload));
      } catch (error) {
        status.textContent = error.message;
      } finally {
        busy(false);
      }
    });
    reset.addEventListener('click', async () => {
      busy(true);
      status.textContent = 'Restoring default…';
      try {
        show(await patchJson(selectedPath(), { inherit: true }));
      } catch (error) {
        status.textContent = error.message;
      } finally {
        busy(false);
      }
    });
    await load();
  } catch (error) {
    status.textContent = error.message;
  }
}
