/* 表单与请求：动作集中在 ACTIONS 里，失败必须写到页面上 */
const ACTIONS = {
  async import() {
    return request('/api/schemas/import', {
      files: ['schema_v2026_06.sql', 'schema_v2026_09.sql'],
    });
  },

  async run(button) {
    const body = {};
    if (button.dataset.forceStale) {
      body.base_version_seq = 0;
    }
    const run = await request('/api/runs', body);
    return run.reused
      ? `命中已有批次 ${run.run_id}`
      : `新批次 ${run.run_id} 序号 ${run.version_seq}`;
  },

  async 'compare-types'() {
    const oldValue = document.getElementById('old-type');
    const newValue = document.getElementById('new-type');
    const result = await request('/api/types/compare', {
      old: oldValue ? oldValue.value : '',
      new: newValue ? newValue.value : '',
    });
    const target = document.querySelector('[data-compare-result]');
    if (target) {
      const detail = result.detail ? `/${result.detail}` : '';
      target.textContent = `${result.old.norm} → ${result.new.norm}　${result.change}${detail}　${result.breaking ? '破坏' : '兼容'}`;
    }
    return `比较完成 ${result.change}`;
  },

  async mergeCreate(button) {
    const form = button.closest('[data-merge-create]');
    const body = {};
    if (form) {
      for (const input of form.querySelectorAll('input[name]')) {
        if (input.value.trim()) body[input.name] = input.value.trim();
      }
    }
    const created = await request('/api/merges', body);
    window.location.href = `/merge?merge_id=${encodeURIComponent(created.merge_id)}`;
    return `会话 ${created.merge_id} 待裁定 ${created.counts.pending} 项`;
  },

  async mergeRule(button) {
    const detail = button.closest('[data-merge-detail]');
    const mergeId = detail && detail.dataset.mergeId;
    if (!mergeId) return '没有合并会话';
    const rulings = [];
    const reasons = {};
    for (const input of document.querySelectorAll('[data-ruling-reason]')) {
      const itemKey = input.dataset.item;
      reasons[itemKey] = input.value.trim();
      if (reasons[itemKey]) {
        const select = document.querySelector(`[data-ruling-choice][data-item="${CSS.escape(itemKey)}"]`);
        rulings.push({ item_key: itemKey, choice: select ? select.value : 'ours',
                       reason: reasons[itemKey] });
      }
    }
    if (!rulings.length) throw new Error('至少为一冲突项填写理由');
    const result = await request(`/api/merges/${encodeURIComponent(mergeId)}/rulings`,
                                 { rulings });
    window.location.reload();
    return `已存裁定 ${Object.keys(result.rulings || {}).length} 项，未决 ${result.counts.pending} 项`;
  },

  async mergeBatch(button) {
    const detail = button.closest('[data-merge-detail]');
    const mergeId = detail && detail.dataset.mergeId;
    if (!mergeId) return '没有合并会话';
    const result = await request(`/api/merges/${encodeURIComponent(mergeId)}/batch`, {});
    window.location.href = `/merge?merge_id=${encodeURIComponent(mergeId)}`;
    return `合并批次 ${result.run_id}`;
  },
};

async function request(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const message = (payload && (payload.message || payload.error)) || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return payload;
}

function flash(target, message, bad) {
  if (!target) return;
  target.textContent = message;
  target.classList.toggle('is-bad', Boolean(bad));
  target.classList.toggle('is-ok', !bad);
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const name = button.dataset.action;
  const handler = ACTIONS[name];
  if (!handler) return;
  const target = button.closest('[data-merge-detail]')
    ? button.closest('[data-merge-detail]').querySelector('[data-flash]')
    : document.querySelector('[data-flash]');
  button.disabled = true;
  flash(target, '…', false);
  try {
    const message = await handler(button);
    flash(target, message || '完成', false);
    if (name === 'run') {
      setTimeout(() => window.location.reload(), 400);
    }
  } catch (error) {
    flash(target, error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.addEventListener('submit', (event) => {
  const form = event.target.closest('[data-merge-create]');
  if (!form) return;
  event.preventDefault();
  const button = form.querySelector('button');
  if (button) button.click();
});
