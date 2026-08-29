document.getElementById('f').addEventListener('submit', async ev => {
  ev.preventDefault();
  const e = document.getElementById('e');
  e.textContent = '';
  try {
    const r = await fetch('/api/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({key: document.getElementById('k').value.trim()})
    });
    if (r.ok) { location.href = '/'; return; }
    e.textContent = r.status === 429 ? 'Too many attempts, wait a minute.'
                  : r.status === 403 ? 'Wrong key.'
                  : 'Login failed (' + r.status + ').';
  } catch { e.textContent = 'Cockpit unreachable.'; }
});
