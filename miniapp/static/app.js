const tg = window.Telegram?.WebApp;
function applyTheme() {
  const light = tg && tg.colorScheme === 'light';
  document.body.classList.toggle('tg-light', !!light);
  const color = light ? '#f4f6fb' : '#050713';
  try { tg?.setHeaderColor(color); tg?.setBackgroundColor(color); } catch (_) {}
}
if (tg) { tg.ready(); tg.expand(); applyTheme(); tg.onEvent?.('themeChanged', applyTheme); }

const initData = tg?.initData || '';
let config = null;
let state = null;
let countdownTimer = null;
let refreshTimer = null;
let gami = null;
let spinning = false;
let promoTimer = null;
let freeTimer = null;
let miniFingerprint = localStorage.getItem('vexvpn_fp') || '';
let lastQrValue = 'https://proxy.vexory.xyz';

const $ = (id) => document.getElementById(id);
// Экранирование перед вставкой в innerHTML: тарифы/промокоды/история приходят с сервера,
// но их текст редактируется в админке — без esc это путь к stored-XSS в кабинете.
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const toast = (msg) => {
  const el = $('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 3000);
};

async function ensureFingerprint() {
  if (miniFingerprint && miniFingerprint.length > 16) return miniFingerprint;
  const raw = [
    navigator.userAgent, navigator.language, screen.width + 'x' + screen.height,
    screen.colorDepth, new Date().getTimezoneOffset(), navigator.hardwareConcurrency || '',
    navigator.deviceMemory || '', navigator.platform || '', tg?.platform || 'web',
    localStorage.getItem('vexvpn_device_salt') || ''
  ].join('|');
  if (!localStorage.getItem('vexvpn_device_salt')) localStorage.setItem('vexvpn_device_salt', crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());
  const bytes = new TextEncoder().encode(raw + '|' + localStorage.getItem('vexvpn_device_salt'));
  const hash = await crypto.subtle.digest('SHA-256', bytes);
  miniFingerprint = [...new Uint8Array(hash)].map(b => b.toString(16).padStart(2, '0')).join('');
  localStorage.setItem('vexvpn_fp', miniFingerprint);
  return miniFingerprint;
}

async function api(path, opts = {}) {
  const fp = await ensureFingerprint();
  const res = await fetch(path, {
    ...opts,
    headers: { 'Content-Type': 'application/json', 'X-Telegram-Init-Data': initData, 'X-MiniApp-Fingerprint': fp, 'X-MiniApp-Platform': tg?.platform || navigator.platform || 'web', ...(opts.headers || {}) }
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) { msg = await res.text(); }
    throw new Error(msg || res.statusText);
  }
  return res.json();
}

function demoState() {
  const expire = new Date(Date.now() + 29 * 86400_000 + 4 * 3600_000 + 24 * 60_000);
  return {
    user: { first_name: 'Developer', username: 'telegram_user', active_promo_code: 'SALE30', trial_used: false },
    subscription: { active: true, plan: 'Unlimited 30 дней', expire: expire.toLocaleString('ru-RU'), expire_at: expire.toISOString(), server_now: new Date().toISOString(), left: '29 дн. 4 ч. 24 мин.', left_seconds: Math.floor((expire - Date.now()) / 1000), left_days: 29, traffic: '∞ безлимит', traffic_remaining_label: '∞ безлимит', subscription_url: 'Откройте Mini App внутри Telegram, чтобы увидеть личную ссылку', progress: 92 },
    payments: [
      { plan: 'Unlimited 30 дней', stars: 250, promo: 'SALE30', date: '30.06.2026 00:00', charge_id: 'demo', status: 'success' },
      { plan: 'Lite 7 дней', stars: 0, promo: 'FREE7', date: '28.06.2026 00:00', charge_id: 'demo', status: 'success' }
    ],
    promo_history: [{ code: 'FREE7', date: '28.06.2026 00:00', title: '7 дней бесплатно', status: 'used' }],
    tickets: [{ id: 1, topic: 'vpn', status: 'open', date: '29.06.2026 18:20' }],
    referral: { total: 2, rewarded: 1, bonus_days: 3 },
    daily_free: { available_today: true, claimed_today: false, pending: false, next_seconds: 13 * 3600 + 22 * 60, reward_days: 1, reward_traffic_gb: 100, device_count: 1, max_devices: 3 },
    referral_link: 'https://t.me/VexDevVPNbot?start=ref_demo',
    bot_links: { buy: 'https://t.me/VexDevVPNbot?start=buy', support: 'https://t.me/VexDevVPNbot' }
  };
}

function leftParts(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  return { d: Math.floor(seconds / 86400), h: Math.floor((seconds % 86400) / 3600), m: Math.floor((seconds % 3600) / 60), s: seconds % 60 };
}
function formatLeft(seconds) {
  const { d, h, m, s } = leftParts(seconds);
  if (d > 0) return `${d} дн. ${h} ч. ${m} мин.`;
  if (h > 0) return `${h} ч. ${m} мин. ${s} сек.`;
  return `${m} мин. ${s} сек.`;
}
function renderRingTime(seconds) {
  const { d, h, m, s } = leftParts(seconds);
  $('days-left').textContent = d;
  $('hero-days').textContent = d;
  $('time-left-parts').textContent = d > 0 ? `${h} ч · ${m} мин` : h > 0 ? `${h} ч · ${m} мин` : `${m} мин · ${s} сек`;
  $('left-full').textContent = formatLeft(seconds);
}
function ringColor(leftSeconds, progress) {
  if (leftSeconds <= 0) return '#fb7185';
  if (leftSeconds < 86400 || progress < 12) return '#fb7185';
  if (leftSeconds < 3 * 86400 || progress < 25) return '#fbbf24';
  return '#22c55e';
}
function startCountdown() {
  clearInterval(countdownTimer);
  const sub = state?.subscription;
  if (!sub?.expire_at) return;
  const expireMs = Date.parse(sub.expire_at);
  const serverNowMs = Date.parse(sub.server_now || new Date().toISOString());
  const clientStartMs = Date.now();
  const totalPlanSeconds = Math.max(1, Math.round((sub.left_seconds || 0) / Math.max(0.01, (sub.progress || 1) / 100)));
  const tick = () => {
    const syntheticNow = serverNowMs + (Date.now() - clientStartMs);
    const leftSeconds = Math.max(0, Math.floor((expireMs - syntheticNow) / 1000));
    renderRingTime(leftSeconds);
    const progress = Math.max(0, Math.min(100, Math.round(leftSeconds / totalPlanSeconds * 100)));
    const color = ringColor(leftSeconds, progress);
    $('ring').style.setProperty('--value', progress);
    $('ring').style.setProperty('--ring-color', color);
    const active = leftSeconds > 0;
    $('status-pill').textContent = active ? '● VPN Active' : '● Expired';
    $('status-pill').style.color = active ? color : '#fb7185';
    $('hero-status').textContent = active ? 'active' : 'expired';
    $('vpn-state').textContent = active ? 'Активна' : 'Истекла';
    const strip = $('status-strip');
    strip.classList.toggle('danger', leftSeconds < 86400);
    strip.classList.toggle('warn', leftSeconds >= 86400 && leftSeconds < 3 * 86400);
    $('countdown-warning').textContent = leftSeconds < 86400 ? 'Осталось меньше 24 часов — лучше продлить сейчас' : leftSeconds < 3 * 86400 ? 'Подписка скоро закончится' : 'Всё работает стабильно';
  };
  tick();
  countdownTimer = setInterval(tick, 1000);
}

async function buyPlan(planKey) {
  if (!initData) { toast('Покупка доступна внутри Telegram Mini App'); window.open(`https://t.me/${config.bot_username}?start=buy`, '_blank'); return; }
  try {
    toast('Готовлю счёт…');
    const res = await api('/api/invoice-link', { method: 'POST', body: JSON.stringify({ plan_key: planKey }) });
    if (tg?.openInvoice) {
      tg.openInvoice(res.invoice_link, async (status) => {
        if (status === 'paid') { toast('Оплата прошла, обновляю кабинет…'); await reloadProfile(); }
        else toast(status === 'cancelled' ? 'Оплата отменена' : 'Счёт закрыт');
      });
    } else window.location.href = res.invoice_link;
  } catch (e) { toast(e.message || 'Не удалось создать счёт'); }
}

function cleanLabel(value) {
  return String(value || '')
    .replace(/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}]/gu, '')
    .replace(/\s+/g, ' ')
    .trim();
}
function planLabel(p, i) {
  if (p.traffic_only) return 'пакет трафика';
  if (p.key?.includes('family')) return 'family access';
  if (p.unlimited || p.traffic === '∞ безлимит') return 'unlimited';
  if (i === 2 || /popular|популяр/i.test(p.badge || '')) return 'popular';
  return cleanLabel(p.badge) || 'vpn access';
}
function planTone(p, i) {
  if (p.traffic_only) return 'tone-slate wide-plan';
  if (p.key?.includes('family')) return 'tone-violet';
  if (p.unlimited || p.traffic === '∞ безлимит') return 'tone-red';
  return i % 3 === 0 ? 'tone-orange' : i % 3 === 1 ? 'tone-violet' : 'tone-red';
}
function renderPlans() {
  const wrap = $('plans');
  const plans = config.plans || [];
  wrap.innerHTML = plans.map((p, i) => `
    <article class="plan-tile ${planTone(p, i)}" id="${p.traffic_only ? 'traffic-plans' : ''}">
      <div class="tile-pattern" aria-hidden="true"></div>
      <div class="tile-mark">VexVPN</div>
      <div class="tile-core">
        <span class="tile-label">${esc(planLabel(p, i))}</span>
        <h3>${esc(p.title)}</h3>
        <p>${p.traffic_only ? 'Добавка к текущей подписке' : `${esc(p.days)} дней стабильного доступа`}</p>
      </div>
      <div class="tile-bottom">
        <div class="tile-price"><strong>${esc(p.stars)}</strong><span>Stars</span></div>
        <button class="tile-cta buy-plan" data-plan="${esc(p.key)}">${p.traffic_only ? 'Купить трафик' : 'В каталог'}</button>
      </div>
      <div class="tile-specs">
        <span>${esc(p.traffic)}</span>
        <span>${p.traffic_only ? 'дата не меняется' : esc(p.devices_label)}</span>
        <span>${p.traffic_only ? 'прибавляется к лимиту' : 'Hiddify / Happ / iOS / Android'}</span>
      </div>
    </article>`).join('');
  document.querySelectorAll('.buy-plan').forEach(btn => btn.addEventListener('click', () => buyPlan(btn.dataset.plan)));
  renderUpsell();
}
function renderUpsell() {
  const sub = state?.subscription;
  const payments = state?.payments || [];
  const card = $('upsell-card');
  if (!sub) card.textContent = 'Совет: начни с Lite 7 дней или сразу возьми Standard 30 дней — так выгоднее, чем часто продлевать короткий тариф.';
  else if (sub.traffic_limit && sub.traffic_remaining != null && sub.traffic_remaining / Math.max(1, sub.traffic_limit) < 0.15) card.textContent = 'Трафик почти закончился — лучше докупить пакет +100 ГБ или перейти на безлимит.';
  else if (payments[0]?.plan?.includes('7')) card.textContent = 'Ты покупал короткий тариф — 30 дней обычно выгоднее и спокойнее.';
  else card.textContent = 'One-click renew: продли текущий тариф заранее, чтобы VPN не отключился в дороге.';
}
function renderCoupons() {
  $('coupon-list').innerHTML = (config.promos || []).map(p => `<span class="coupon">${esc(p.code)} · ${esc(p.title)}</span>`).join('');
}
function renderPromoHistory() {
  const list = $('promo-history');
  const items = state?.promo_history || [];
  if (!items.length) { list.innerHTML = '<p class="muted">История промокодов появится после активации.</p>'; return; }
  list.innerHTML = items.map(p => `<div class="history-item"><div><b>${esc(p.code)}</b><br><small>${esc(p.date)} · ${esc(p.title || 'промокод')}</small></div><strong>${esc(p.status || 'used')}</strong></div>`).join('');
}
function renderReferral() {
  const ref = state?.referral || {};
  $('ref-link').textContent = state.referral_link || '—';
  $('ref-total').textContent = ref.total ?? 0;
  $('ref-rewarded').textContent = ref.rewarded ?? 0;
}

function formatShortTime(seconds) {
  const { d, h, m, s } = leftParts(seconds);
  if (d > 0) return `${d}д ${h}ч ${m}м`;
  if (h > 0) return `${h}ч ${m}м`;
  return `${m}м ${s}с`;
}
function renderDailyFree() {
  const df = state?.daily_free || {};
  const btn = $('claim-free');
  const text = $('free-status-text');
  const available = $('free-available');
  if (!btn || !text) return;
  available.innerHTML = `Сегодня доступно: <b>+${esc(df.reward_days || 1)} день +${esc(df.reward_traffic_gb || 100)} ГБ</b>`;
  clearInterval(freeTimer);
  let left = Math.max(0, df.next_seconds || 0);
  const tick = () => {
    $('free-timer').textContent = df.claimed_today || df.pending ? `следующий бесплатный день через ${formatShortTime(left)}` : `до обновления лимита: ${formatShortTime(left)}`;
    left = Math.max(0, left - 1);
  };
  tick(); freeTimer = setInterval(tick, 1000);
  if (df.pending) {
    btn.disabled = true; btn.textContent = 'Выдача обрабатывается…'; text.textContent = 'Проверяю сервер VPN. Обычно ссылка обновится через минуту.'; return;
  }
  if (df.claimed_today) {
    btn.disabled = true; btn.textContent = 'Сегодня уже получено ✓'; text.textContent = 'Бесплатный лимит на сегодня использован. Если нужно больше — выбери платный тариф ниже.'; return;
  }
  if (df.blocked) {
    btn.disabled = true; btn.textContent = 'Free временно ограничен'; text.textContent = df.reason || 'Сработала антиабуз-защита. Напиши в поддержку, если это ошибка.'; return;
  }
  btn.disabled = false; btn.textContent = 'Получить бесплатный VPN сегодня';
  text.textContent = `Доступно на этом аккаунте. Устройств: ${df.device_count ?? 0}/${df.max_devices ?? 3}.`;
}
async function claimFreeVpn() {
  if (!initData) return toast('Бесплатная выдача доступна внутри Telegram Mini App');
  const btn = $('claim-free');
  try {
    btn.disabled = true; btn.textContent = 'Проверяю устройство…';
    const fp = await ensureFingerprint();
    const r = await api('/api/daily-free/claim', { method: 'POST', body: JSON.stringify({ fingerprint: fp, platform: tg?.platform || navigator.platform || 'web' }) });
    toast(r.message || 'Бесплатный VPN активирован'); tg?.HapticFeedback?.notificationOccurred('success');
    await reloadProfile();
  } catch (e) {
    toast(e.message || 'Не получилось получить бесплатный VPN');
    await reloadProfile();
  }
}

function renderProfile() {
  const sub = state.subscription;
  $('renew-link').href = '#plans-section';
  $('support-link').href = state.bot_links.support;
  const happ = $('open-happ'), hid = $('open-hiddify'), bar = $('traffic-bar');
  if (!sub) {
    $('plan-name').textContent = 'Нет подписки'; $('mini-plan').textContent = 'нет'; $('mini-traffic').textContent = '—';
    renderRingTime(0); $('expire-at').textContent = '—'; $('traffic-limit').textContent = '—'; $('sub-url').textContent = 'Купи тариф — ссылка появится здесь';
    $('ring').style.setProperty('--value', 0); $('ring').style.setProperty('--ring-color', '#fb7185');
    $('status-pill').textContent = '● No subscription'; $('status-pill').style.color = '#fb7185'; $('vpn-state').textContent = 'Нет'; $('hero-status').textContent = 'no sub';
    $('countdown-warning').textContent = 'Выбери тариф ниже и оплати через Telegram Stars';
    if (bar) bar.classList.add('hidden'); if (happ) happ.style.display = 'none'; if (hid) hid.style.display = 'none';
    $('usage-spark')?.classList.add('hidden');
    return;
  }
  $('plan-name').textContent = sub.plan; $('mini-plan').textContent = sub.plan.replace(' дней',''); $('expire-at').textContent = sub.expire;
  $('traffic-limit').textContent = sub.traffic_remaining_label && sub.traffic_remaining != null ? `${sub.traffic_remaining_label} из ${sub.traffic}` : sub.traffic_used_label ? `${sub.traffic_used_label} · ${sub.traffic}` : sub.traffic;
  $('mini-traffic').textContent = sub.traffic_remaining_label || sub.traffic;
  const fill = $('traffic-bar-fill');
  if (bar && fill) {
    if (sub.traffic_limit && sub.traffic_used != null) { const pct = Math.max(0, Math.min(100, Math.round(sub.traffic_used / sub.traffic_limit * 100))); fill.style.width = pct + '%'; bar.classList.toggle('warn', pct >= 90); bar.classList.remove('hidden'); }
    else bar.classList.add('hidden');
  }
  const su = sub.subscription_url || ''; const validUrl = su && !su.startsWith('Откройте');
  if (happ) { if (validUrl) { happ.href = 'happ://add/' + su; happ.style.display = ''; } else happ.style.display = 'none'; }
  if (hid) { if (validUrl) { hid.href = 'hiddify://import/' + su; hid.style.display = ''; } else hid.style.display = 'none'; }
  $('sub-url').textContent = sub.subscription_url; lastQrValue = validUrl ? su : 'https://proxy.vexory.xyz'; $('qr').src = `/api/qr.svg?data=${encodeURIComponent(lastQrValue)}`;
  $('ring').style.setProperty('--value', sub.progress || 0); startCountdown(); renderSparkline(sub);
}
async function loadSecurity() {
  const box = $('security-box');
  if (!box || !initData) return;
  try {
    const sec = await api('/api/security');
    const warn = (sec.warnings || []).length;
    $('security-status').textContent = warn ? 'Check' : 'Protected';
    $('security-status').style.color = warn ? '#fbbf24' : '#22c55e';
    $('security-text').textContent = warn
      ? sec.warnings.join(' ')
      : `Защита активна: ${sec.device_count}/${sec.device_limit} Mini App devices, сбросов ссылки: ${sec.reset_count}.`;
    const events = (sec.events || []).slice(0, 3).map(e => `<span class="security-event ${esc(e.severity)}">${esc(e.title)}</span>`).join('');
    $('security-list').innerHTML = [
      ...(sec.protections || []).slice(0, 4).map(x => `<span>✓ ${esc(x)}</span>`),
      events
    ].join('');
  } catch (_) {
    $('security-text').textContent = 'Security Center доступен внутри Telegram Mini App.';
  }
}

function renderPayments() {
  const list = $('payments');
  if (!state.payments?.length) { list.innerHTML = '<p class="muted">История появится после первой оплаты.</p>'; return; }
  list.innerHTML = state.payments.map(p => `<div class="payment"><div><b>${esc(p.plan)}</b><br><small>${esc(p.date)}${p.promo ? ' · ' + esc(p.promo) : ''}${p.charge_id ? ' · ' + esc(p.charge_id) : ''}</small></div><strong>${esc(p.status || 'success')} · ${esc(p.stars)} Stars</strong></div>`).join('');
}
let selectedTopic = 'payment';
let supportTickets = [];
let currentTicket = null;
const ticketBadge = s => s === 'answered' ? '🟢 ответ' : s === 'closed' ? '⚪️ закрыт' : '🟡 открыт';
async function loadSupport() {
  try { const r = await api('/api/support/tickets'); supportTickets = r.items || []; renderSupportTickets(); } catch (_) {}
}
function renderSupportTickets() {
  const list = $('ticket-history');
  $('tickets-count').textContent = supportTickets.length ? `${supportTickets.length} шт.` : 'Tickets';
  if (!supportTickets.length) { list.innerHTML = '<p class="muted">Обращений нет. Создай новое слева — ответим здесь и в боте.</p>'; return; }
  list.innerHTML = supportTickets.map(t => `<div class="history-item ticket-row" data-id="${t.id}"><div><b>#${esc(t.id)} · ${esc(t.topic_title)}</b><br><small>${esc(t.preview || '')}</small></div><strong>${ticketBadge(t.status)}</strong></div>`).join('');
  document.querySelectorAll('.ticket-row').forEach(el => el.addEventListener('click', () => openTicket(+el.dataset.id)));
}
async function createTicket() {
  const text = $('ticket-text').value.trim();
  if (text.length < 3) return toast('Опиши проблему чуть подробнее');
  try {
    await api('/api/support/ticket', { method: 'POST', body: JSON.stringify({ topic: selectedTopic, message: text }) });
    $('ticket-text').value = ''; toast('Обращение создано — ответим здесь и в боте');
    tg?.HapticFeedback?.notificationOccurred('success'); await loadSupport();
  } catch (e) { toast(e.message || 'Не удалось создать обращение'); }
}
function renderThread(t) {
  $('modal-thread').innerHTML = (t.messages || []).map(m => `<div class="msg ${m.sender === 'user' ? 'me' : 'support'}"><span class="msg-who">${m.sender === 'user' ? 'Ты' : 'Поддержка'} · ${esc(m.date || '')}</span><div>${esc(m.text)}</div></div>`).join('');
  const th = $('modal-thread'); th.scrollTop = th.scrollHeight;
}
async function openTicket(id) {
  try {
    const t = await api(`/api/support/ticket/${id}`); currentTicket = t;
    $('modal-title').textContent = `Заявка #${t.id} · ${t.topic_title}`;
    renderThread(t);
    const closed = t.status === 'closed';
    $('modal-close-ticket').style.display = closed ? 'none' : '';
    $('modal-text').placeholder = closed ? 'Напиши, чтобы открыть заявку снова…' : 'Ответить…';
    $('ticket-modal').classList.remove('hidden');
  } catch (e) { toast(e.message || 'Не удалось открыть заявку'); }
}
async function sendTicketReply() {
  if (!currentTicket) return;
  const text = $('modal-text').value.trim(); if (!text) return;
  try {
    const t = await api(`/api/support/ticket/${currentTicket.id}/message`, { method: 'POST', body: JSON.stringify({ text }) });
    currentTicket = t; $('modal-text').value = ''; renderThread(t); $('modal-close-ticket').style.display = ''; await loadSupport();
  } catch (e) { toast(e.message || 'Сообщение не отправлено'); }
}
async function closeTicketUi() {
  if (!currentTicket) return;
  try { await api(`/api/support/ticket/${currentTicket.id}/close`, { method: 'POST' }); toast('Заявка закрыта'); $('ticket-modal').classList.add('hidden'); await loadSupport(); }
  catch (e) { toast(e.message || 'Не удалось закрыть'); }
}

function renderGamification(g) {
  gami = g; if (!g) return;
  const c = g.checkin || {}, goal = c.goal || 3;
  $('streak-count').textContent = c.streak ?? 0;
  $('streak-text').textContent = g.eligible ? `Заходи ${goal} дня подряд — получи +${c.reward_days || 1} день` : (g.reason || 'Оформи подписку для бонусов');
  const fill = c.claimed_today ? (c.progress === 0 ? goal : c.progress) : (c.progress || 0);
  $('streak-dots').innerHTML = Array.from({ length: goal }, (_, i) => `<span class="dot ${i < fill ? 'on' : ''}"></span>`).join('');
  const dailyBtn = $('daily-bonus');
  dailyBtn.disabled = !c.can_claim;
  dailyBtn.textContent = !g.eligible ? 'Нужна активная подписка' : c.claimed_today ? 'Уже забрано сегодня ✓' : 'Забрать ежедневный бонус';
  const w = g.wheel || {};
  $('wheel-segments').innerHTML = (w.segments || []).map(s => `<span class="wheel-seg" data-key="${esc(s.key)}">${esc(s.label)}</span>`).join('');
  const spinBtn = $('spin-wheel');
  spinBtn.disabled = !w.can_spin || spinning;
  spinBtn.textContent = !g.eligible ? 'Нужна активная подписка' : w.spun_today ? 'Сегодня уже крутили ✓' : 'Крутить колесо';
  $('wheel-status').textContent = w.spun_today ? 'завтра снова' : '1 спин в день';
  const a = g.achievements || [];
  $('ach-count').textContent = `${a.filter(x => x.earned).length}/${a.length}`;
  $('ach-grid').innerHTML = a.map(x => `<div class="achievement ${x.earned ? 'done' : ''}"><b>${esc(x.title)}</b><span>${esc(x.desc)}</span></div>`).join('');
}
async function loadGamification() { try { renderGamification(await api('/api/gamification')); } catch (_) {} }
async function claimDaily() {
  try { const r = await api('/api/bonus/daily/claim', { method: 'POST' }); toast(r.message || 'Готово'); tg?.HapticFeedback?.notificationOccurred('success'); await Promise.all([loadGamification(), reloadProfile()]); }
  catch (e) { toast(e.message || 'Не получилось'); }
}
function animateWheel(winnerKey) {
  return new Promise(resolve => {
    const chips = [...document.querySelectorAll('#wheel-segments .wheel-seg')];
    if (!chips.length) return resolve();
    const winIndex = Math.max(0, chips.findIndex(c => c.dataset.key === winnerKey));
    const totalTicks = chips.length * 3 + winIndex; // несколько кругов + докрутка до приза
    let i = 0;
    const step = () => {
      chips.forEach(c => c.classList.remove('active', 'won'));
      chips[i % chips.length].classList.add('active');
      if (i >= totalTicks) {
        chips.forEach(c => c.classList.remove('active'));
        chips[winIndex].classList.add('won');
        return resolve();
      }
      i++;
      setTimeout(step, 70 + Math.max(0, i - totalTicks + 8) * 30); // замедление к финишу
    };
    step();
  });
}
async function spinWheel() {
  if (spinning) return;
  const btn = $('spin-wheel');
  try {
    spinning = true; btn.disabled = true; btn.textContent = 'Крутим…';
    const r = await api('/api/wheel/spin', { method: 'POST' });
    await animateWheel(r.segment.key);
    $('wheel-result').textContent = r.message || '';
    toast(r.message || 'Готово'); tg?.HapticFeedback?.notificationOccurred(r.won ? 'success' : 'warning');
    await Promise.all([loadGamification(), reloadProfile()]);
  } catch (e) { toast(e.message || 'Не получилось'); }
  finally { spinning = false; await loadGamification(); }
}

function fmtBytes(n) { n = Math.max(0, n || 0); const gb = n / 1073741824; return gb >= 1 ? gb.toFixed(1) + ' ГБ' : Math.round(n / 1048576) + ' МБ'; }
function confirmAction(msg) {
  return new Promise(resolve => { if (tg?.showConfirm) tg.showConfirm(msg, ok => resolve(!!ok)); else resolve(window.confirm(msg)); });
}
function renderPromoBanner() {
  const b = config?.promo_banner; const el = $('promo-banner');
  if (!el) return;
  if (!b || !b.enabled) { el.classList.add('hidden'); clearInterval(promoTimer); return; }
  el.innerHTML = `<div class="pb-text"><b>${esc(b.title || 'Акция')}</b><span>${esc(b.subtitle || '')}</span></div>${b.until ? '<span class="pb-timer" id="pb-timer"></span>' : ''}`;
  el.classList.remove('hidden');
  clearInterval(promoTimer);
  if (b.until) {
    const end = Date.parse(b.until);
    const tick = () => {
      const t = $('pb-timer'); if (!t) return;
      if (isNaN(end)) { t.textContent = ''; return; }
      const left = end - Date.now();
      if (left <= 0) { t.textContent = 'Завершено'; clearInterval(promoTimer); return; }
      const d = Math.floor(left / 86400000), h = Math.floor(left % 86400000 / 3600000), m = Math.floor(left % 3600000 / 60000), s = Math.floor(left % 60000 / 1000);
      t.textContent = d > 0 ? `${d}д ${h}ч ${m}м` : `${h}ч ${m}м ${s}с`;
    };
    tick(); promoTimer = setInterval(tick, 1000);
  }
}
function renewablePlanKey() {
  const key = state?.subscription?.plan_key;
  if (!key) return null;
  const p = (config?.plans || []).find(x => x.key === key);
  return (p && !p.traffic_only && !p.is_trial) ? key : null;
}
function renderSparkline(sub) {
  const el = $('usage-spark'), bars = $('spark-bars'); if (!el || !bars) return;
  const hist = sub?.usage_history || [];
  if (hist.length < 2) { el.classList.add('hidden'); return; }
  const deltas = [];
  for (let i = 1; i < hist.length; i++) deltas.push(Math.max(0, (hist[i].used || 0) - (hist[i - 1].used || 0)));
  const max = Math.max(1, ...deltas), total = deltas.reduce((a, b) => a + b, 0);
  bars.innerHTML = deltas.map(d => `<span class="bar${d === max && d > 0 ? ' peak' : ''}" style="height:${Math.max(6, Math.round(d / max * 100))}%" title="${esc(fmtBytes(d))}"></span>`).join('');
  $('spark-total').textContent = `${fmtBytes(total)} за ${deltas.length} дн.`;
  el.classList.remove('hidden');
}
async function resetDevice() {
  if (!state?.subscription) return toast('Нужна активная подписка');
  if (!(await confirmAction('Сбросить профиль? Старые конфиги на других устройствах перестанут работать — нужно будет заново импортировать ссылку.'))) return;
  try {
    await api('/api/device/reset', { method: 'POST' });
    toast('Профиль сброшен. Импортируй ссылку заново на нужном устройстве.');
    tg?.HapticFeedback?.notificationOccurred('success');
    await reloadProfile();
  } catch (e) { toast(e.message || 'Не удалось сбросить профиль'); }
}

const deviceData = {
  ios: { title: 'iPhone', clients: [['Hiddify','https://apps.apple.com/app/hiddify-proxy-vpn/id6596777532'], ['Streisand','https://apps.apple.com/app/streisand/id6450534064'], ['Shadowrocket','https://apps.apple.com/app/shadowrocket/id932747118']], steps: ['Установи Hiddify или Streisand из App Store.', 'Скопируй ссылку или отсканируй QR-код.', 'Нажми Import / Add profile и подключись к VexVPN.'] },
  android: { title: 'Android', clients: [['Hiddify','https://play.google.com/store/search?q=hiddify&c=apps'], ['v2rayNG','https://play.google.com/store/search?q=v2rayNG&c=apps'], ['Happ','https://play.google.com/store/search?q=happ%20vpn&c=apps']], steps: ['Установи Hiddify, Happ или v2rayNG.', 'Скопируй подписку из кабинета.', 'Импортируй из буфера обмена и нажми Connect.'] },
  windows: { title: 'Windows', clients: [['Hiddify','https://hiddify.com/'], ['Nekoray','https://github.com/MatsuriDayo/nekoray/releases']], steps: ['Установи Hiddify Desktop.', 'Скопируй ссылку подписки.', 'Add profile → Import from clipboard → Connect.'] },
  macos: { title: 'macOS', clients: [['Hiddify','https://hiddify.com/'], ['Streisand','https://apps.apple.com/app/streisand/id6450534064']], steps: ['Установи Hiddify или Streisand.', 'Скопируй ссылку / открой deeplink.', 'Импортируй профиль и выбери сервер VexVPN.'] }
};
function renderDevice(device='ios') {
  const data = deviceData[device]; $('device-title').textContent = data.title;
  $('device-steps').innerHTML = data.steps.map(s => `<li><b>${s.split('.')[0]}</b><span>${s}</span></li>`).join('');
  $('client-list').innerHTML = data.clients.map(([name, url]) => `<div class="client-item"><div><b>${name}</b><br><span>${data.title}</span></div><a href="${url}" target="_blank">Открыть</a></div>`).join('');
  document.querySelectorAll('#device-tabs button').forEach(b => b.classList.toggle('active', b.dataset.device === device));
}
async function reloadProfile() { try { state = await api('/api/me'); renderProfile(); renderDailyFree(); loadSecurity(); renderPayments(); renderPromoHistory(); renderReferral(); loadSupport(); renderUpsell(); loadGamification(); } catch (_) {} }
async function load() {
  config = await api('/api/config');
  try { state = await api('/api/me'); } catch (e) { state = demoState(); toast('Демо-режим: откройте страницу из Telegram для личного кабинета'); }
  renderPlans(); renderCoupons(); renderPromoBanner(); renderProfile(); renderDailyFree(); loadSecurity(); renderPayments(); renderPromoHistory(); renderReferral(); loadSupport(); renderDevice('ios');
  loadGamification(); clearInterval(refreshTimer); refreshTimer = setInterval(reloadProfile, 60_000);
}
async function copySub() { const url = state?.subscription?.subscription_url; if (!url || url.startsWith('Откройте')) return toast('Личная ссылка доступна внутри Telegram'); await navigator.clipboard.writeText(url); tg?.HapticFeedback?.notificationOccurred('success'); toast('Ссылка подписки скопирована'); }
$('copy-sub').addEventListener('click', copySub); $('copy-sub-2').addEventListener('click', copySub);
$('download-qr').addEventListener('click', () => { window.open(`/api/qr.png?data=${encodeURIComponent(lastQrValue)}`, '_blank'); });
$('renew-link').addEventListener('click', (e) => { const k = renewablePlanKey(); if (k) { e.preventDefault(); buyPlan(k); } });
$('device-reset').addEventListener('click', resetDevice);
$('hero-buy').addEventListener('click', () => document.querySelector('#plans-section')?.scrollIntoView({ behavior: 'smooth' }));
$('copy-ref').addEventListener('click', async () => { await navigator.clipboard.writeText(state.referral_link); toast('Реферальная ссылка скопирована'); });
$('share-ref').addEventListener('click', () => { const link = state?.referral_link; if (!link) return toast('Ссылка появится после входа в Telegram'); const text = 'Подключайся к VexVPN ⚡ быстрый VPN на Xray/Reality'; const url = `https://t.me/share/url?url=${encodeURIComponent(link)}&text=${encodeURIComponent(text)}`; if (tg?.openTelegramLink) tg.openTelegramLink(url); else window.open(url, '_blank'); });
$('apply-promo').addEventListener('click', async () => { const code = $('promo-input').value.trim(); if (!code) return toast('Введите промокод'); try { const r = await api('/api/promo', { method: 'POST', body: JSON.stringify({ code }) }); toast(r.granted ? `Промокод ${r.code} выдал дни и трафик` : `Промокод ${r.code} применён к следующей покупке`); await reloadProfile(); } catch (e) { toast(e.message || 'Промокод не применён'); } });
$('check-vpn').addEventListener('click', async () => { try { $('check-vpn').textContent = 'Проверяю…'; const r = await api('/api/check-vpn'); toast(r.message || (r.ok ? 'VPN подписка доступна' : 'Проверка не прошла')); } catch (e) { toast('Откройте Mini App внутри Telegram для проверки'); } finally { $('check-vpn').textContent = 'Проверить VPN'; } });
document.querySelectorAll('#device-tabs button').forEach(btn => btn.addEventListener('click', () => renderDevice(btn.dataset.device)));
document.querySelectorAll('.topic-chip').forEach(btn => btn.addEventListener('click', () => { document.querySelectorAll('.topic-chip').forEach(x => x.classList.remove('active')); btn.classList.add('active'); selectedTopic = btn.dataset.topic; }));
$('ticket-send').addEventListener('click', createTicket);
$('modal-send').addEventListener('click', sendTicketReply);
$('modal-close').addEventListener('click', () => $('ticket-modal').classList.add('hidden'));
$('modal-close-ticket').addEventListener('click', closeTicketUi);
$('ticket-modal').addEventListener('click', (e) => { if (e.target === $('ticket-modal')) $('ticket-modal').classList.add('hidden'); });
$('daily-bonus').addEventListener('click', claimDaily);
$('spin-wheel').addEventListener('click', spinWheel);
$('claim-free').addEventListener('click', claimFreeVpn);
load().catch(err => { console.error(err); toast('Ошибка загрузки кабинета'); });
