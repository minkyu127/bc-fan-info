// 즐겨찾기(브라우저에 저장) + 업데이트 진행 상황 표시
window.BC = {
  KEY: 'bc_favorites',
  favs() {
    try { return JSON.parse(localStorage.getItem(this.KEY)) || []; } catch (e) { return []; }
  },
  toggle(id) {
    const list = this.favs();
    const i = list.indexOf(id);
    if (i >= 0) list.splice(i, 1); else list.push(id);
    localStorage.setItem(this.KEY, JSON.stringify(list));
    document.dispatchEvent(new Event('favchange'));
  },
  esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  },
  paint(root) {
    const list = this.favs();
    (root || document).querySelectorAll('.fav').forEach(b => {
      const on = list.includes(b.dataset.id);
      b.classList.toggle('on', on);
      b.textContent = on ? '★' : '☆';
    });
  },
  bindFavs(root) {
    (root || document).querySelectorAll('.fav').forEach(b => {
      if (b.dataset.bound) return;
      b.dataset.bound = '1';
      b.addEventListener('click', e => { e.preventDefault(); e.stopPropagation(); BC.toggle(b.dataset.id); });
    });
    this.paint(root);
  },
};
document.addEventListener('favchange', () => BC.paint());
BC.bindFavs();

// 업데이트 진행 상황 (관리 화면 / 상단 바)
(function () {
  const bar = document.getElementById('updating-bar'), box = document.getElementById('status-box');
  if (!bar && !box) return;
  let wasRunning = !!bar;
  async function poll() {
    let s;
    try { s = await (await fetch('/api/status')).json(); } catch (e) { return setTimeout(poll, 3000); }
    const count = s.total ? ` (${s.done}/${s.total})` : '';
    const stepEl = document.getElementById('upd-step');
    if (stepEl) { stepEl.textContent = s.step; document.getElementById('upd-count').textContent = count; }
    if (box) {
      document.getElementById('st-state').textContent = s.running ? '업데이트 중 — ' + s.step + count
        : (s.error ? '오류: ' + s.error : (s.finished_at ? '완료 (' + s.finished_at + ')' : '대기'));
      document.getElementById('st-bar').style.width = s.running ? (s.total ? (s.done / s.total * 100) : 15) + '%' : (s.finished_at ? '100%' : '0%');
      const log = document.getElementById('st-log');
      log.textContent = s.log.join('\n');
      log.scrollTop = log.scrollHeight;
      document.querySelectorAll('.btn-row button').forEach(b => b.disabled = s.running);
    }
    if (s.running) { wasRunning = true; setTimeout(poll, 1500); }
    else if (wasRunning && bar) { location.reload(); }
    else if (wasRunning && box) { wasRunning = false; }
  }
  poll();
})();
