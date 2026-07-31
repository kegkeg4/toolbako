document.addEventListener('click', async (event) => {
  const navToggle = event.target.closest('[data-nav-toggle]');
  if (navToggle) {
    const header = navToggle.closest('.site-header');
    const open = header.classList.toggle('nav-open');
    navToggle.setAttribute('aria-expanded', String(open));
    navToggle.textContent = open ? '×' : '☰';
    return;
  }
  const button = event.target.closest('[data-like]');
  if (!button) return;
  button.disabled = true;
  try {
    const response = await fetch(`/api/tools/${button.dataset.like}/like`, {method:'POST'});
    if (response.status === 401) { location.href = `/login?next=${encodeURIComponent(location.pathname)}`; return; }
    if (!response.ok) throw new Error('like_failed');
    const data = await response.json();
    button.classList.toggle('liked', data.liked);
    button.setAttribute('aria-pressed', String(data.liked));
    button.querySelector('b').textContent = data.liked ? 'いいね済み' : 'いいね';
    button.querySelector('em').textContent = data.count;
  } catch (_) {
    button.setAttribute('title', '通信に失敗しました。もう一度お試しください。');
  } finally {
    button.disabled = false;
  }
});

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  const header = document.querySelector('.site-header.nav-open');
  if (!header) return;
  header.classList.remove('nav-open');
  const button = header.querySelector('[data-nav-toggle]');
  button?.setAttribute('aria-expanded', 'false');
  if (button) button.textContent = '☰';
});

document.addEventListener('submit', (event) => {
  const form = event.target;
  const actionPath = new URL(form.action || location.href, location.href).pathname;
  const automaticConfirmations = [
    [/\/actions\/accept$/, '納品を承諾して取引を完了します。内容を十分に確認しましたか？'],
    [/\/actions\/cancel_accept$/, 'キャンセルに合意し、対象の決済を返金します。続けますか？'],
    [/\/subscriptions\/[^/]+\/cancel$/, '次回の定期更新を停止します。続けますか？'],
    [/\/payouts$/, '表示された金額で振込を申請します。金額を確認しましたか？'],
    [/\/applications\/[^/]+\/select$/, 'この提案を採用し、表示金額の決済へ進みます。続けますか？'],
    [/\/extra-payment$/, '表示金額を追加で支払います。続けますか？'],
  ];
  const inferred = automaticConfirmations.find(([pattern]) => pattern.test(actionPath))?.[1];
  const confirmation = event.submitter?.dataset.confirm || form.dataset.confirm || inferred;
  if (confirmation && !window.confirm(confirmation)) {
    event.preventDefault();
    return;
  }
  if (form.matches('.compare-picker')) {
    form.querySelector('[name=slugs]').value = [...form.querySelectorAll('[name=pick]')].map((x) => x.value).filter(Boolean).join(',');
  }
  const submitter = event.submitter;
  if (submitter && !submitter.dataset.allowRepeat) {
    window.setTimeout(() => { submitter.disabled = true; submitter.setAttribute('aria-busy', 'true'); }, 0);
  }
});

document.addEventListener('click', (event) => {
  if (event.target.closest('[data-print]')) window.print();
});

document.addEventListener('change', (event) => {
  if (event.target.closest('[data-auto-submit]')) event.target.form?.requestSubmit();
  if (event.target.matches('[data-password-toggle]')) {
    document.querySelectorAll('[data-password-input]').forEach((input) => { input.type = event.target.checked ? 'text' : 'password'; });
  }
  if (event.target.matches('[data-price-type]')) updatePriceField(event.target.closest('form'));
});

function updatePriceField(form) {
  const type = form?.querySelector('[data-price-type]');
  const field = form?.querySelector('[data-price-field]');
  const input = field?.querySelector('input');
  if (!type || !field || !input) return;
  const paid = type.value === 'paid';
  field.hidden = !paid;
  input.disabled = !paid;
  input.required = paid;
}

document.querySelectorAll('[data-tool-form]').forEach(updatePriceField);

function updateCheckout(root) {
  const form = root?.querySelector('[data-checkout-form]');
  if (!form) return;
  const selectedPlan = form.querySelector('[name="billing_type"]:checked') || form.querySelector('[name="billing_type"]');
  const base = Number(selectedPlan?.dataset.basePrice || root.dataset.oneTime || 0);
  const options = [...form.querySelectorAll('[data-option-price]:checked')].reduce((sum, item) => sum + Number(item.dataset.optionPrice || 0), 0);
  const subtotal = base + options;
  const coupon = form.querySelector('[data-coupon]')?.value.trim().toUpperCase() || '';
  const localDemoDiscount = root.dataset.demo === 'true' && coupon === 'WELCOME10' ? Math.min(Math.round(subtotal * .1), 1000) : 0;
  const discount = localDemoDiscount || (coupon && coupon === root.dataset.couponCode ? Number(root.dataset.couponDiscount || 0) : 0);
  const yen = (value) => `¥${new Intl.NumberFormat('ja-JP').format(value)}`;
  root.querySelector('[data-summary-base]').textContent = yen(base);
  root.querySelector('[data-summary-options]').textContent = yen(options);
  root.querySelector('[data-options-row]').hidden = options === 0;
  root.querySelector('[data-summary-discount]').textContent = `−${yen(discount)}`;
  root.querySelector('[data-discount-row]').hidden = discount === 0;
  root.querySelector('[data-summary-total]').textContent = yen(subtotal - discount);
  const monthly = selectedPlan?.value === 'subscription';
  root.querySelector('[data-base-label]').textContent = monthly ? '月額価格' : '商品価格';
  root.querySelector('[data-total-label]').textContent = monthly ? '今回のお支払い（月額）' : '合計';
}

document.querySelectorAll('[data-checkout]').forEach(updateCheckout);
document.addEventListener('input', (event) => { const root = event.target.closest('[data-checkout]'); if (root) updateCheckout(root); });
document.addEventListener('change', (event) => { const root = event.target.closest('[data-checkout]'); if (root) updateCheckout(root); });
document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-apply-coupon]');
  if (!button) return;
  const root = button.closest('[data-checkout]');
  const form = button.closest('form');
  const input = form?.querySelector('[data-coupon]');
  const feedback = form?.querySelector('[data-coupon-feedback]');
  const code = input?.value.trim().toUpperCase() || '';
  if (!root || !form || !input || !feedback || !code) {
    if (feedback) feedback.textContent = 'クーポンコードを入力してください。';
    return;
  }
  button.disabled = true;
  feedback.textContent = 'クーポンを確認しています…';
  const payload = new FormData(form);
  payload.set('slug', root.dataset.slug || '');
  try {
    const response = await fetch('/api/coupons/preview', {method:'POST', body:payload, credentials:'same-origin'});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'クーポンを確認できません');
    root.dataset.couponCode = result.code;
    root.dataset.couponDiscount = String(result.discount);
    input.value = result.code;
    feedback.textContent = result.discount ? `クーポンを適用しました（¥${new Intl.NumberFormat('ja-JP').format(result.discount)}割引）` : 'このコードには割引がありません。';
  } catch (error) {
    delete root.dataset.couponCode;
    delete root.dataset.couponDiscount;
    feedback.textContent = error.message || 'クーポンを確認できません。';
  } finally {
    button.disabled = false;
    updateCheckout(root);
  }
});

const recoveryForm = document.querySelector('[data-recovery-form]');
if (recoveryForm) {
  const hash = new URLSearchParams(location.hash.slice(1));
  const token = hash.get('access_token');
  const recoveryType = hash.get('type');
  const error = document.querySelector('[data-recovery-error]');
  if (token && recoveryType === 'recovery') {
    recoveryForm.querySelector('[data-recovery-token]').value = token;
    history.replaceState(null, '', location.pathname);
  } else {
    recoveryForm.hidden = true;
    error.hidden = false;
  }
}

const chatMessages = document.querySelector('[data-chat-messages]');
if (chatMessages) chatMessages.scrollTop = chatMessages.scrollHeight;
