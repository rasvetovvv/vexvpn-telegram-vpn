const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const initData = tg?.initData || '';
const $ = id => document.getElementById(id);
const toast = msg => { const t = $('toast'); t.textContent = msg; t.classList.add('show'); setTimeout(()=>t.classList.remove('show'),2600); };

async function api(path, opts={}) {
  const res = await fetch(path, { ...opts, headers: { 'Content-Type':'application/json', 'X-Telegram-Init-Data': initData, ...(opts.headers||{}) }});
  if (!res.ok) { let msg=res.statusText; try{msg=(await res.json()).detail||msg}catch(_){msg=await res.text()} throw new Error(msg); }
  if (res.headers.get('content-type')?.includes('json')) return res.json();
  return res.text();
}
function esc(v){return String(v ?? '').replace(/[&<>"]/g, s=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[s]));}
function statusClass(s){return s==='success'||s==='manual'?'ok':s==='marzban_error'?'bad':'warn'}

let IS_SUPER = false;
async function loadSummary(){
  const s = await api('/api/admin/summary');
  IS_SUPER = !!s.is_super;
  $('metrics').innerHTML = [
    ['Пользователи', s.users], ['Активные подписки', s.active_subscriptions], ['Серверов онлайн', s.servers_online], ['Платежи', s.payments],
    ['Проблемные оплаты', s.problem_payments], ['Stars 7 дней', s.revenue_week], ['Stars 30 дней', s.revenue_month], ['Start → Pay', s.conversion.start_to_pay + '%']
  ].map(([k,v])=>`<div class="metric"><span>${k}</span><strong>${v}</strong></div>`).join('');
  $('conversion').innerHTML = [
    ['Starts', s.conversion.starts, '100%'],
    ['Выбор тарифа', s.conversion.plan_choices, s.conversion.start_to_choice + '% от /start'],
    ['Оплаты', s.conversion.payments, s.conversion.choice_to_pay + '% от выбора'],
  ].map(([k,v,h])=>`<div class="conv"><span class="muted">${k}</span><strong>${v}</strong><small class="muted">${h}</small></div>`).join('');
}
async function loadUsers(){
  const q = encodeURIComponent($('user-search').value.trim());
  const data = await api(`/api/admin/users?q=${q}&limit=80`);
  $('users').innerHTML = data.items.map(u=>{
    const id = u.telegram_id;
    const copyBtn = u.subscription_url ? `<button class="ghost" data-copy="${esc(u.subscription_url)}">copy</button>` : '';
    const del = IS_SUPER ? `<button class="ghost bad" data-act="delete" data-uid="${id}">🗑 удалить</button>` : '';
    return `<tr>
    <td><b>${esc(id)}</b><br><span class="muted">@${esc(u.username||'—')}</span><br><span class="pill">${esc(u.marzban_username||'no marzban')}</span></td>
    <td>${esc(u.plan||'—')}<br><span class="${u.active?'ok':'bad'}">${u.active?'active':'inactive'}</span></td>
    <td>${esc(u.expire_at||'—')}</td><td>${esc(u.traffic||'—')}</td>
    <td class="user-actions">${copyBtn}
      <button class="ghost" data-act="usage" data-uid="${id}">📊 usage</button>
      <button class="ghost" data-act="reset-traffic" data-uid="${id}">♻️ трафик</button>
      <button class="ghost" data-act="disable" data-uid="${id}">⏸ выкл</button>
      <button class="ghost" data-act="enable" data-uid="${id}">▶️ вкл</button>
      ${del}
    </td>
  </tr>`;
  }).join('') || '<tr><td colspan="5">Нет данных</td></tr>';
  document.querySelectorAll('[data-copy]').forEach(b=>b.onclick=()=>navigator.clipboard.writeText(b.dataset.copy).then(()=>toast('Скопировано')));
  document.querySelectorAll('#users [data-act]').forEach(b=>b.onclick=()=>userAction(b.dataset.act, b.dataset.uid));
}
async function userAction(act, uid){
  if (act === 'usage'){
    try{
      const u = await api(`/api/admin/user/usage?telegram_id=${encodeURIComponent(uid)}`);
      toast(u.ok ? `${uid}: ${u.used_label} из ${u.limit_label}, статус ${u.status}` : (u.message || 'Нет данных'));
    }catch(e){ toast(e.message || 'Ошибка'); }
    return;
  }
  if (act === 'delete' && !window.confirm(`Удалить ${uid} в Marzban и локально? Действие необратимо.`)) return;
  if ((act === 'reset-traffic' || act === 'disable') && !window.confirm(`Подтвердить «${act}» для ${uid}?`)) return;
  try{
    await api(`/api/admin/user/${act}`, {method:'POST', body:JSON.stringify({telegram_id:+uid, confirm:true})});
    toast('Готово');
    await Promise.all([loadUsers(), loadSummary()]);
  }catch(e){ toast(e.message || 'Не удалось'); }
}
async function loadPayments(){
  const q = encodeURIComponent($('payment-search').value.trim());
  const problem = $('problem-only').checked ? 'true' : 'false';
  const data = await api(`/api/admin/payments?q=${q}&problem_only=${problem}&limit=100`);
  $('payments').innerHTML = data.items.map(p=>{
    const refundable = p.stars > 0 && p.status !== 'refunded' && !/^(PROMO|TEST|ADMIN)-/.test(p.charge_id||'');
    const refundBtn = refundable ? `<br><button class="ghost" data-refund="${esc(p.charge_id)}" data-uid="${esc(p.telegram_id)}">↩︎ refund</button>` : '';
    return `<tr>
    <td>${esc(p.date)}</td><td><b>${esc(p.telegram_id)}</b><br><span class="muted">@${esc(p.username||'—')}</span></td>
    <td>${esc(p.plan)}<br><small class="muted">${esc(p.charge_id)}</small>${p.error?`<br><small class="bad">${esc(p.error)}</small>`:''}</td>
    <td>${esc(p.stars)}</td><td class="${statusClass(p.status)}">${esc(p.status)}${refundBtn}</td>
  </tr>`;
  }).join('') || '<tr><td colspan="5">Нет платежей</td></tr>';
  document.querySelectorAll('[data-refund]').forEach(b=>b.onclick=()=>refundPayment(b.dataset.refund, b.dataset.uid));
}
async function refundPayment(charge, uid){
  if (!window.confirm(`Вернуть Stars за платёж ${charge} пользователю ${uid}?`)) return;
  try{
    const r = await api('/api/admin/refund', {method:'POST', body:JSON.stringify({charge_id: charge, confirm:true})});
    toast(`Возвращено ⭐ ${r.stars}`);
    await Promise.all([loadPayments(), loadSummary()]);
  }catch(e){ toast(e.message || 'Не удалось вернуть Stars'); }
}
async function loadLogs(){
  const data = await api('/api/admin/marzban-logs?limit=120');
  $('marzban-logs').innerHTML = data.items.map(r=>`<tr><td>${esc(r.date)}</td><td>${esc(r.telegram_id||'—')}</td><td>${esc(r.action)}</td><td class="${r.status==='success'?'ok':'bad'}">${esc(r.status)}</td><td>${esc(r.message||'')}</td></tr>`).join('') || '<tr><td colspan="5">Пока логов нет</td></tr>';
}
async function loadAuditLog(){
  const action = encodeURIComponent(($('audit-filter')?.value || '').trim());
  const data = await api(`/api/admin/audit-log?limit=120&action=${action}`);
  $('audit-log').innerHTML = data.items.map(r=>`<tr><td>${esc(r.date)}</td><td>${esc(r.admin_id)}</td><td>${esc(r.action)}</td><td>${esc(r.target||'—')}</td><td>${esc(r.details||'')}</td></tr>`).join('') || '<tr><td colspan="5">Пока действий нет</td></tr>';
}
async function loadHealth(){
  const h = await api('/api/admin/health');
  const checks = Object.entries(h.checks || {}).map(([k,v])=>`<div class="conv"><span class="muted">${esc(k)}</span><strong class="${v.ok?'ok':'bad'}">${v.ok?'OK':'ERROR'}</strong><small class="muted">${esc(v.latency_ms ?? '—')} ms${v.status?(' / '+esc(v.status)):''}</small></div>`).join('');
  $('health').innerHTML = checks + `<div class="conv"><span class="muted">Errors 24h</span><strong>${esc(h.errors_24h?.marzban||0)} M / ${esc(h.errors_24h?.payments||0)} P</strong><small class="muted">Queue: ${esc(JSON.stringify(h.grant_queue||{}))}</small></div>`;
}
async function loadGrantQueue(){
  const data = await api('/api/admin/grant-queue?limit=80');
  $('grant-queue').innerHTML = data.items.map(r=>`<tr><td>${esc(r.created_at)}</td><td>${esc(r.telegram_id)}</td><td>${esc(r.plan)}<br><small class="muted">${esc(r.charge_id)}</small></td><td class="${r.status==='done'?'ok':r.status==='failed'?'bad':'warn'}">${esc(r.status)}<br><small>${esc(r.attempts)} tries</small></td><td>${esc(r.last_error||'')}</td></tr>`).join('') || '<tr><td colspan="5">Очередь пустая</td></tr>';
}
async function loadAbuseFlags(){
  const data = await api('/api/admin/abuse-flags?limit=100');
  $('abuse-flags').innerHTML = data.items.map(r=>`<tr><td>${esc(r.created_at)}</td><td>${esc(r.telegram_id||'—')}</td><td>${esc(r.kind)}<br><small class="muted">fp ${esc(r.fingerprint||'—')} / ip ${esc(r.ip||'—')}</small></td><td class="${r.severity==='block'?'bad':r.severity==='warn'?'warn':'ok'}">${esc(r.severity)}</td><td>${esc(r.details||'')}</td></tr>`).join('') || '<tr><td colspan="5">Флагов нет</td></tr>';
}
async function runPaymentsCheck(){
  try{
    const r = await api('/api/admin/payments-check');
    toast(`Без подписки: ${r.success_without_subscription.length}; проблемные: ${r.problem_payments.length}; дубли: ${r.duplicate_charge_ids.length}; без charge_id: ${r.missing_charge_id.length}; странные суммы: ${r.strange_amounts.length}`);
  }catch(e){ toast(e.message || 'payments_check failed'); }
}
async function loadTariffs(){
  const data = await api('/api/admin/tariffs');
  $('tariffs').innerHTML = data.items.map(t=>`<div class="tariff" data-key="${esc(t.key)}">
    <h3>${esc(t.key)}</h3>
    <input data-f="title" value="${esc(t.title)}">
    <div class="row"><input data-f="days" type="number" value="${t.days}"><input data-f="stars" type="number" value="${t.stars}"></div>
    <div class="row"><input data-f="traffic_gb" type="number" value="${t.traffic_gb}"><input data-f="devices" type="number" value="${t.devices}"></div>
    <input data-f="badge" value="${esc(t.badge||'')}" placeholder="badge">
    <label class="check"><input data-f="visible" type="checkbox" ${t.visible?'checked':''}> visible</label>
    <label class="check"><input data-f="is_trial" type="checkbox" ${t.is_trial?'checked':''}> trial</label>
    <label class="check"><input data-f="traffic_only" type="checkbox" ${t.traffic_only?'checked':''}> traffic only</label>
    <label class="check"><input data-f="unlimited" type="checkbox" ${t.unlimited?'checked':''}> unlimited</label>
    <button class="primary save-tariff">Сохранить</button>
  </div>`).join('');
  document.querySelectorAll('.save-tariff').forEach(btn=>btn.onclick=saveTariff);
}
async function saveTariff(e){
  const box = e.target.closest('.tariff');
  const key = box.dataset.key;
  const val = f => box.querySelector(`[data-f="${f}"]`);
  const body = {
    title: val('title').value, days: +val('days').value, stars: +val('stars').value,
    traffic_gb: +val('traffic_gb').value, devices: +val('devices').value, badge: val('badge').value,
    visible: val('visible').checked, is_trial: val('is_trial').checked, traffic_only: val('traffic_only').checked,
    unlimited: val('unlimited').checked
  };
  await api(`/api/admin/tariffs/${encodeURIComponent(key)}`, {method:'PUT', body:JSON.stringify(body)});
  toast('Тариф сохранён');
}
async function grant(){
  const body = { telegram_id:+$('grant-id').value, days:+$('grant-days').value, traffic_gb:+$('grant-gb').value, reason:$('grant-reason').value || 'manual' };
  if (!body.telegram_id) return toast('Укажи Telegram ID');
  const r = await api('/api/admin/grant', {method:'POST', body:JSON.stringify(body)});
  toast(`Выдано: ${r.expire_at}, ${r.traffic}`);
  await refreshAll();
}
async function downloadCsvFrom(path, filename){
  try{
    const text = await api(path);
    const blob = new Blob([text], {type:'text/csv;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = filename; a.click();
    URL.revokeObjectURL(url);
  }catch(e){toast(e.message || 'CSV не скачался')}
}
const downloadCsv = () => downloadCsvFrom('/api/admin/payments.csv', 'vexvpn_payments.csv');
const downloadUsersCsv = () => downloadCsvFrom('/api/admin/users.csv', 'vexvpn_users.csv');
let currentTicketId = null;
async function loadTickets(){
  const status = $('ticket-filter')?.value || 'active';
  const data = await api(`/api/admin/tickets?status=${status}&limit=80`);
  $('tickets-open-badge').textContent = data.open_count ? `${data.open_count} открытых` : '';
  const badge = s => s==='answered' ? '<span class="ok">🟢 ответ</span>' : s==='closed' ? '<span class="muted">⚪️ закрыт</span>' : '<span class="warn">🟡 ждёт</span>';
  $('admin-tickets').innerHTML = (data.items||[]).map(t=>`<tr class="ticket-row" data-id="${t.id}"><td><b>#${esc(t.id)}</b></td><td>${esc(t.telegram_id)}</td><td>${esc(t.topic_title)}<br><small class="muted">${esc(t.preview||'')}</small></td><td>${badge(t.status)}</td><td>${esc(t.updated||'')}</td></tr>`).join('') || '<tr><td colspan="5">Тикетов нет</td></tr>';
  document.querySelectorAll('#admin-tickets .ticket-row').forEach(r=>r.onclick=()=>openTicket(+r.dataset.id));
}
function renderAdminThread(t){
  $('amodal-thread').innerHTML = (t.messages||[]).map(m=>`<div class="amsg ${m.sender==='admin'?'admin':'user'}"><span>${m.sender==='admin'?'Поддержка':'User'} · ${esc(m.date||'')}</span><div>${esc(m.text)}</div></div>`).join('') || '<p class="muted">Сообщений нет</p>';
  const th=$('amodal-thread'); th.scrollTop=th.scrollHeight;
}
async function openTicket(id){
  try{
    currentTicketId = id;
    const t = await api(`/api/admin/ticket/${id}`);
    $('amodal-title').textContent = `Тикет #${t.id} · ${t.topic_title} · ${t.telegram_id}`;
    renderAdminThread(t);
    $('amodal-closeticket').style.display = t.status==='closed' ? 'none' : '';
    $('ticket-modal').classList.remove('hidden');
  }catch(e){ toast(e.message || 'Не удалось открыть тикет'); }
}
async function replyTicket(){
  if(!currentTicketId) return;
  const text = $('amodal-text').value.trim(); if(!text) return toast('Пустой ответ');
  try{ const t = await api(`/api/admin/ticket/${currentTicketId}/reply`, {method:'POST', body:JSON.stringify({text})}); $('amodal-text').value=''; renderAdminThread(t); $('amodal-closeticket').style.display=''; toast('Ответ отправлен'); await loadTickets(); }
  catch(e){ toast(e.message || 'Не отправилось'); }
}
async function closeTicketAdmin(){
  if(!currentTicketId) return;
  try{ await api(`/api/admin/ticket/${currentTicketId}/close`, {method:'POST'}); toast('Тикет закрыт'); $('ticket-modal').classList.add('hidden'); await loadTickets(); }
  catch(e){ toast(e.message || 'Не удалось закрыть'); }
}
async function refreshAll(){
  try{
    await loadSummary();  // выставляет IS_SUPER до отрисовки кнопок пользователей
    await Promise.all([loadUsers(), loadAbuseFlags(), loadPayments(), loadLogs(), loadAuditLog(), loadTariffs(), loadHealth(), loadGrantQueue(), loadTickets()]);
  }
  catch(e){ $('auth-warning').classList.remove('hidden'); toast(e.message || 'Ошибка админки'); }
}
['user-search','payment-search'].forEach(id=>$(id).addEventListener('input',()=>setTimeout(id==='user-search'?loadUsers:loadPayments,200)));
$('problem-only').onchange=loadPayments;
$('audit-filter')?.addEventListener('input',()=>setTimeout(loadAuditLog,250));
$('refresh').onclick=refreshAll;
$('download-csv').onclick=downloadCsv;
$('download-users-csv').onclick=downloadUsersCsv;
$('reload-tariffs').onclick=loadTariffs;
$('reload-abuse').onclick=loadAbuseFlags;
$('payments-check').onclick=runPaymentsCheck;
$('grant-btn').onclick=grant;
$('ticket-filter')?.addEventListener('change', loadTickets);
$('amodal-close')?.addEventListener('click', ()=>$('ticket-modal').classList.add('hidden'));
$('amodal-send')?.addEventListener('click', replyTicket);
$('amodal-closeticket')?.addEventListener('click', closeTicketAdmin);
$('ticket-modal')?.addEventListener('click', e=>{ if(e.target===$('ticket-modal')) $('ticket-modal').classList.add('hidden'); });
if (!initData) $('auth-warning').classList.remove('hidden');
refreshAll();
