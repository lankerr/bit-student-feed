'use strict';
const $ = id => document.getElementById(id);
const REPO = 'lankerr/bit-student-feed';
const WD_NAMES = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
const CAT_LABELS = {academic: '学术讲座', competition: '竞赛科创', career: '升学就业', volunteer: '实践志愿', campus_service: '校园服务', other: '综合通知'};

const state = {
  enabled: true,
  every_n_weeks: 2,
  push_weekday: 2,
  push_time: '09:00',
  anchor_date: '2026-11-24',
  start_weekday: 5,
  span_days: 15,
  title_prefix: '北理双周活动汇总（学术先锋号）'
};

let allArticles = [];
const TOKEN_KEY = 'bitfeed_gh_pat';

function shanghaiToday() {
  return new Intl.DateTimeFormat('en-CA', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit'}).format(new Date());
}

function parseISODate(iso) {
  const [y, m, d] = String(iso || '').split('-').map(Number);
  return new Date(Date.UTC(y, (m || 1) - 1, d || 1));
}

function toISODate(dt) {
  return dt.toISOString().slice(0, 10);
}

function addDays(iso, n) {
  const dt = parseISODate(iso);
  dt.setUTCDate(dt.getUTCDate() + n);
  return toISODate(dt);
}

function isoWeekday(iso) {
  const day = parseISODate(iso).getUTCDay();
  return day === 0 ? 7 : day; // 1=Mon .. 7=Sun
}

function mondayOf(iso) {
  return addDays(iso, -(isoWeekday(iso) - 1));
}

function computeWindow(forDayIso, cfg) {
  const offset = (cfg.start_weekday - isoWeekday(forDayIso) + 7) % 7;
  const start = addDays(forDayIso, offset);
  const end = addDays(start, cfg.span_days - 1);
  return {start, end};
}

function computeUpcoming(cfg, count = 4) {
  if (!cfg.enabled) return [];
  const today = shanghaiToday();
  const anchorMon = mondayOf(cfg.anchor_date);
  const runs = [];
  let cursor = today;
  for (let i = 0; i < 400 && runs.length < count; i++) {
    const curMon = mondayOf(cursor);
    const weeks = Math.round((parseISODate(curMon) - parseISODate(anchorMon)) / (7 * 86400 * 1000));
    if (isoWeekday(cursor) === cfg.push_weekday && weeks >= 0 && weeks % cfg.every_n_weeks === 0) {
      const win = computeWindow(cursor, cfg);
      runs.push({
        pushDate: cursor,
        pushWd: WD_NAMES[isoWeekday(cursor) - 1],
        pushTime: cfg.push_time,
        winStart: win.start,
        winStartWd: WD_NAMES[isoWeekday(win.start) - 1],
        winEnd: win.end,
        winEndWd: WD_NAMES[isoWeekday(win.end) - 1]
      });
    }
    cursor = addDays(cursor, 1);
  }
  return runs;
}

function buildToml(cfg) {
  const safeTitle = String(cfg.title_prefix || '北理双周活动汇总（学术先锋号）').replace(/["\\\r\n]/g, '').trim().slice(0, 40) || '北理双周活动汇总（学术先锋号）';
  return [
    '# =====================================================================',
    '# 学术先锋号 · 群机器人推送设置',
    '# 修改方法：可在可视化工作台 https://lankerr.github.io/bit-student-feed/manage.html 调整，',
    '# 或在 GitHub 网页点右上角铅笔图标 ✏️ 修改后点绿色 Commit changes 保存。',
    '# =====================================================================',
    '',
    '# 1. 是否开启自动定时推送（true = 开启；寒暑假暂停推送请改成 false）',
    `enabled = ${cfg.enabled ? 'true' : 'false'}`,
    '',
    '# 2. 推送间隔周数（2 = 每双周推一次；每周推一次填 1）',
    `every_n_weeks = ${cfg.every_n_weeks}`,
    '',
    '# 3. 星期几推送（1=周一, 2=周二, 3=周三, 4=周四, 5=周五, 6=周六, 7=周日）',
    `push_weekday = ${cfg.push_weekday}`,
    '',
    '# 4. 推送时间（北京时间 24 小时制，例如 "09:00"）',
    `push_time = "${cfg.push_time}"`,
    '',
    '# 5. 双周起算基准日（格式 "YYYY-MM-DD"）',
    `anchor_date = "${cfg.anchor_date}"`,
    '',
    '# 6. 活动统计范围：start_weekday=5 表示从本周五起，连续统计 span_days 天（15=至下下个周五）',
    `start_weekday = ${cfg.start_weekday}`,
    `span_days = ${cfg.span_days}`,
    '',
    '# 7. 飞书卡片与 Word 文档标题前缀',
    `title_prefix = "${safeTitle}"`,
    ''
  ].join('\n');
}

function parseToml(text) {
  const out = {};
  for (const raw of String(text || '').split(/\r?\n/)) {
    const line = raw.replace(/#.*$/, '').trim();
    if (!line || !line.includes('=')) continue;
    const idx = line.indexOf('=');
    const k = line.slice(0, idx).trim();
    const v = line.slice(idx + 1).trim();
    if (v === 'true' || v === 'false') out[k] = v === 'true';
    else if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) out[k] = v.slice(1, -1);
    else if (/^\d+$/.test(v)) out[k] = Number(v);
  }
  return out;
}

function showStatus(msg, isError = false) {
  const box = $('action-status');
  box.textContent = msg;
  box.className = 'status-msg show' + (isError ? ' err' : '');
}

function syncFormFromState() {
  $('btn-enable').className = state.enabled ? 'active-on' : '';
  $('btn-disable').className = !state.enabled ? 'active-off' : '';
  $('every-weeks').value = String(state.every_n_weeks);
  document.querySelectorAll('#push-weekday-group .wd-btn').forEach(b => {
    b.classList.toggle('active', Number(b.dataset.wd) === state.push_weekday);
  });
  $('push-time').value = state.push_time;
  $('anchor-date').value = state.anchor_date;
  $('start-weekday').value = String(state.start_weekday);
  $('span-days').value = String(state.span_days);
  $('title-prefix').value = state.title_prefix;

  const presetKey = `${state.start_weekday}:${state.span_days}`;
  if (['5:15', '5:8', '1:14'].includes(presetKey)) {
    $('window-preset').value = presetKey;
    $('custom-window-box').style.display = 'none';
  } else {
    $('window-preset').value = 'custom';
    $('custom-window-box').style.display = 'grid';
  }
}

function updateSchedulePreview(resetFilterDates = false) {
  $('toml-preview').textContent = buildToml(state);
  const badge = $('schedule-badge');
  const freq = state.every_n_weeks === 1 ? '每周' : state.every_n_weeks === 2 ? '每双周' : `每 ${state.every_n_weeks} 周`;
  const wd = WD_NAMES[state.push_weekday - 1];
  const startWd = WD_NAMES[state.start_weekday - 1];
  const endWd = WD_NAMES[(state.start_weekday - 1 + state.span_days - 1) % 7];

  if (!state.enabled) {
    badge.textContent = '⏸️ 自动推送已暂停（寒暑假模式）';
    badge.className = 'badge badge-pause';
  } else {
    badge.textContent = `✅ ${freq}${wd} ${state.push_time} 推送 · 统计本${startWd}至${endWd}（共 ${state.span_days} 天）`;
    badge.className = 'badge';
  }

  const tbody = $('upcoming-rows');
  tbody.replaceChildren();
  const runs = computeUpcoming(state, 4);
  if (!runs.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 3;
    td.textContent = '当前已暂停自动定时推送；开启后自动显示未来 4 期排期。';
    tr.appendChild(td);
    tbody.appendChild(tr);
  } else {
    runs.forEach((r, idx) => {
      const tr = document.createElement('tr');
      const td1 = document.createElement('td'); td1.textContent = `第 ${idx + 1} 期`;
      const td2 = document.createElement('td'); td2.textContent = `${r.pushDate}（${r.pushWd}）${r.pushTime}`;
      const td3 = document.createElement('td'); td3.textContent = `${r.winStart}（${r.winStartWd}）→ ${r.winEnd}（${r.winEndWd}）`;
      tr.append(td1, td2, td3);
      tbody.appendChild(tr);
    });
  }

  if (resetFilterDates) {
    const curWin = computeWindow(shanghaiToday(), state);
    $('filter-start').value = curWin.start;
    $('filter-end').value = curWin.end;
  }
  renderWindowArticles();
}

function shanghaiDayFromTs(seconds) {
  if (!seconds) return '';
  const d = new Date(seconds * 1000);
  return Number.isNaN(d.getTime()) ? '' : new Intl.DateTimeFormat('en-CA', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit'}).format(d);
}

function activeActionDay(a) {
  const today = shanghaiToday();
  const c = a.classification;
  if (!c || !c.verified_activity || c.event_status !== 'actionable') return null;
  const days = [c.deadline, c.event_date, shanghaiDayFromTs(a.source_expiry)].filter(Boolean);
  if (!days.length || days.some(d => d < today)) return null;
  return days.slice().sort()[0];
}

function getFilteredArticles() {
  const s = $('filter-start').value;
  const e = $('filter-end').value;
  return allArticles
    .map(a => ({a, due: activeActionDay(a)}))
    .filter(x => x.due && (!s || x.due >= s) && (!e || x.due <= e))
    .sort((x, y) => x.due.localeCompare(y.due) || (x.a.title || '').localeCompare(y.a.title || '', 'zh'));
}

function renderWindowArticles() {
  const box = $('window-articles');
  box.replaceChildren();
  if (!allArticles.length) {
    const p = document.createElement('p');
    p.className = 'empty';
    p.textContent = '正在加载或暂无公开活动数据…';
    box.appendChild(p);
    return;
  }
  const matched = getFilteredArticles();
  $('window-count').textContent = `（共 ${matched.length} 条已核验活动）`;
  if (!matched.length) {
    const p = document.createElement('p');
    p.className = 'empty';
    p.textContent = '所选日期区间内暂无通过日期核验的活动（可查看完整版 Word 含就业网宣讲会及远期预告）。';
    box.appendChild(p);
    return;
  }
  for (const {a, due} of matched) {
    const c = a.classification || {};
    const wrap = document.createElement('div');
    wrap.className = 'mini-item';
    const left = document.createElement('div');
    const h4 = document.createElement('h4');
    const link = document.createElement('a');
    link.textContent = a.title;
    link.href = a.url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    h4.appendChild(link);
    const meta = document.createElement('div');
    meta.className = 'mini-meta';
    const tag = document.createElement('span');
    tag.className = 'tag';
    tag.textContent = CAT_LABELS[c.category] || '综合通知';
    const src = document.createElement('span');
    src.textContent = a.source;
    const when = document.createElement('span');
    when.textContent = [c.event_date ? `活动 ${c.event_date}` : '', c.deadline ? `截止 ${c.deadline}` : ''].filter(Boolean).join(' · ') || `节点 ${due}`;
    meta.append(tag, src, when);
    left.append(h4, meta);
    wrap.appendChild(left);
    box.appendChild(wrap);
  }
}

$('btn-enable').onclick = () => { state.enabled = true; syncFormFromState(); updateSchedulePreview(false); };
$('btn-disable').onclick = () => { state.enabled = false; syncFormFromState(); updateSchedulePreview(false); };
$('every-weeks').onchange = e => { state.every_n_weeks = Number(e.target.value) || 2; updateSchedulePreview(false); };
document.querySelectorAll('#push-weekday-group .wd-btn').forEach(b => {
  b.onclick = () => { state.push_weekday = Number(b.dataset.wd); syncFormFromState(); updateSchedulePreview(false); };
});
$('push-time').onchange = e => { if (e.target.value) state.push_time = e.target.value.slice(0, 5); updateSchedulePreview(false); };
$('anchor-date').onchange = e => { if (e.target.value) state.anchor_date = e.target.value; updateSchedulePreview(false); };
$('window-preset').onchange = e => {
  const v = e.target.value;
  if (v === 'custom') {
    $('custom-window-box').style.display = 'grid';
  } else {
    $('custom-window-box').style.display = 'none';
    const [sw, sd] = v.split(':').map(Number);
    state.start_weekday = sw;
    state.span_days = sd;
    $('start-weekday').value = String(sw);
    $('span-days').value = String(sd);
    updateSchedulePreview(true);
  }
};
$('start-weekday').onchange = e => { state.start_weekday = Number(e.target.value) || 5; updateSchedulePreview(true); };
$('span-days').onchange = e => { state.span_days = Math.max(1, Math.min(42, Number(e.target.value) || 15)); e.target.value = String(state.span_days); updateSchedulePreview(true); };
$('title-prefix').oninput = e => { state.title_prefix = e.target.value.slice(0, 40); updateSchedulePreview(false); };
$('filter-start').onchange = renderWindowArticles;
$('filter-end').onchange = renderWindowArticles;
$('reset-window-btn').onclick = () => updateSchedulePreview(true);

$('copy-list-btn').onclick = async () => {
  const matched = getFilteredArticles();
  const s = $('filter-start').value, e = $('filter-end').value;
  const lines = [`# ${state.title_prefix}（${s} 至 ${e}）`, ''];
  matched.forEach(({a}, i) => {
    const c = a.classification || {};
    const cat = CAT_LABELS[c.category] || '综合通知';
    const when = [c.event_date ? `活动 ${c.event_date}` : '', c.deadline ? `截止 ${c.deadline}` : ''].filter(Boolean).join('；');
    lines.push(`${i + 1}. 【${cat}｜${a.source}】${a.title}${when ? '（' + when + '）' : ''}\n   原文链接：${a.url}`);
  });
  try {
    await navigator.clipboard.writeText(lines.join('\n'));
    showStatus(`✅ 已复制 ${matched.length} 条活动清单到剪贴板，可直接粘贴到微信/飞书！`);
  } catch {
    showStatus('浏览器限制了自动复制，请手动选择页面内容复制。', true);
  }
};

$('copy-and-open-gh').onclick = async () => {
  const toml = buildToml(state);
  try {
    await navigator.clipboard.writeText(toml);
    showStatus('✅ 新配置已自动复制到剪贴板！正在打开 GitHub 编辑页：按 Ctrl+A 全选、Ctrl+V 粘贴，再点右上角绿色 Commit changes 即可。');
  } catch {
    showStatus('请展开右侧「查看自动生成的配置文件原文」手动复制后，粘贴到打开的 GitHub 页面中保存。');
  }
  window.open(`https://github.com/${REPO}/edit/main/config/push.toml`, '_blank', 'noopener');
};

function getToken() {
  const inputVal = $('gh-token').value.trim();
  if (inputVal) localStorage.setItem(TOKEN_KEY, inputVal);
  return inputVal || localStorage.getItem(TOKEN_KEY) || '';
}

$('api-clear-token').onclick = () => {
  localStorage.removeItem(TOKEN_KEY);
  $('gh-token').value = '';
  showStatus('已清除保存在本浏览器的 GitHub 口令。');
};

$('api-save-btn').onclick = async () => {
  const token = getToken();
  if (!token) return showStatus('请先在上方输入框粘贴 GitHub Personal Access Token（或者直接用左边免口令跳转按钮）。', true);
  showStatus('正在直接写入 GitHub 仓库 config/push.toml…');
  try {
    const headers = {'Authorization': `Bearer ${token}`, 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'};
    const metaResp = await fetch(`https://api.github.com/repos/${REPO}/contents/config/push.toml`, {headers});
    if (!metaResp.ok) throw new Error(`读取文件失败（HTTP ${metaResp.status}，请确认口令有 Contents 读写权限）`);
    const meta = await metaResp.json();
    const contentB64 = btoa(unescape(encodeURIComponent(buildToml(state))));
    const putResp = await fetch(`https://api.github.com/repos/${REPO}/contents/config/push.toml`, {
      method: 'PUT',
      headers: {...headers, 'Content-Type': 'application/json'},
      body: JSON.stringify({message: '通过可视化工作台更新推送排期 (config/push.toml)', content: contentB64, sha: meta.sha})
    });
    if (!putResp.ok) throw new Error(`保存失败（HTTP ${putResp.status}）`);
    showStatus('✅ 保存成功！GitHub 与线上服务器已同步生效，无需其他操作。');
  } catch (err) {
    showStatus(err.message || '保存失败，请改用左侧「复制配置并去 GitHub 粘贴保存」。', true);
  }
};

$('api-push-btn').onclick = async () => {
  const token = getToken();
  if (!token) return showStatus('请先粘贴 GitHub Personal Access Token（需具备 Actions 读写权限），或点击「打开手动推送页」在 GitHub 运行。', true);
  showStatus('正在通知 GitHub Actions 立即往飞书群发送推送…');
  try {
    const resp = await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/pioneer-push.yml/dispatches`, {
      method: 'POST',
      headers: {'Authorization': `Bearer ${token}`, 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json'},
      body: JSON.stringify({ref: 'main', inputs: {action: 'send_feishu', start_date: $('filter-start').value || '', end_date: $('filter-end').value || ''}})
    });
    if (!resp.ok) throw new Error(`触发失败（HTTP ${resp.status}）`);
    showStatus('✅ 已成功触发！约 20 秒后飞书群机器人将发出本期汇总卡片。');
  } catch (err) {
    showStatus(err.message || '触发失败，请点击「打开手动推送页」在 GitHub 页面运行。', true);
  }
};

(async () => {
  const saved = localStorage.getItem(TOKEN_KEY);
  if (saved) $('gh-token').value = saved;

  // Load live push.toml (try GitHub raw first for instant post-commit freshness, fallback to local ./push.toml)
  let loadedSource = '默认配置';
  for (const [url, label] of [
    [`https://raw.githubusercontent.com/${REPO}/main/config/push.toml?t=${Date.now()}`, 'GitHub 最新配置'],
    ['push.toml', '站点同步配置']
  ]) {
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 3500);
      const r = await fetch(url, {signal: ctrl.signal, cache: 'no-store'});
      clearTimeout(timer);
      if (r.ok) {
        const parsed = parseToml(await r.text());
        Object.assign(state, parsed);
        loadedSource = label;
        break;
      }
    } catch {}
  }
  $('live-source-note').textContent = `已加载当前线上配置（来源：${loadedSource}）`;
  syncFormFromState();
  updateSchedulePreview(true);

  try {
    const r = await fetch('data.json', {cache: 'no-cache'});
    if (r.ok) {
      const d = await r.json();
      if (Array.isArray(d.articles)) allArticles = d.articles;
      if (d.weekly_preview?.docx_url) $('download-word').href = d.weekly_preview.docx_url;
      renderWindowArticles();
    }
  } catch {}
})();
