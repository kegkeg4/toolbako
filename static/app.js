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
  if (button) {
    button.textContent = '☰';
    button.focus();
  }
});

// Reveal invalid fields before the browser tries to focus them.
document.addEventListener('invalid', (event) => {
  if (!event.target.closest('.manage-form, [data-tool-form]')) return;
  let details = event.target.closest('details');
  while (details) {
    details.open = true;
    details = details.parentElement?.closest('details');
  }
}, true);

const pendingSubmissions = new Map();
window.addEventListener('pageshow', () => {
  // Browser back/forward restores the previous DOM, including disabled controls.
  for (const pending of pendingSubmissions.values()) {
    if (!pending.disabledByUs) continue;
    pending.submitter.disabled = pending.wasDisabled;
    if (pending.wasBusy === null) pending.submitter.removeAttribute('aria-busy');
    else pending.submitter.setAttribute('aria-busy', pending.wasBusy);
  }
  pendingSubmissions.clear();
});

document.addEventListener('submit', (event) => {
  if (event.defaultPrevented) return;
  const form = event.target;
  const previous = pendingSubmissions.get(form);
  if (previous && !previous.event.defaultPrevented) {
    event.preventDefault();
    return;
  }
  pendingSubmissions.delete(form);
  const submitter = event.submitter;
  const action = submitter?.getAttribute('formaction') || form.action || location.href;
  const actionPath = new URL(action, location.href).pathname;
  const automaticConfirmations = [
    [/\/actions\/accept$/, '納品を承諾して取引を完了します。内容を十分に確認しましたか？'],
    [/\/actions\/cancel_accept$/, 'キャンセルに合意します。返金は運営の確認・承認後に実行されます。続けますか？'],
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
  const method = (submitter?.getAttribute('formmethod') || form.method || 'get').toLowerCase();
  if (method !== 'post' || submitter?.dataset.allowRepeat) return;
  const pending = {event, submitter, wasDisabled: submitter?.disabled,
    wasBusy: submitter?.getAttribute('aria-busy'), disabledByUs: false};
  pendingSubmissions.set(form, pending);
  // Defer disabling so a named submit button is still included in the request.
  window.setTimeout(() => {
    if (pendingSubmissions.get(form) !== pending) return;
    if (event.defaultPrevented) { pendingSubmissions.delete(form); return; }
    if (submitter) {
      submitter.disabled = true;
      submitter.setAttribute('aria-busy', 'true');
      pending.disabledByUs = true;
    }
  }, 0);
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

let yenFormatter;
function yen(value) {
  yenFormatter ||= new Intl.NumberFormat('ja-JP');
  return `¥${yenFormatter.format(value)}`;
}

function checkoutSelection(root) {
  const form = root?.querySelector('[data-checkout-form]');
  if (!form) return null;
  const selectedPlan = form.querySelector('[name="billing_type"]:checked') || form.querySelector('[name="billing_type"]');
  const base = Number(selectedPlan?.dataset.basePrice || root.dataset.oneTime || 0);
  const selectedOptions = [...form.querySelectorAll('[data-option-price]:checked')];
  const options = selectedOptions.reduce((sum, item) => sum + Number(item.dataset.optionPrice || 0), 0);
  const subtotal = base + options;
  const coupon = form.querySelector('[data-coupon]')?.value.trim().toUpperCase() || '';
  const fingerprint = JSON.stringify([selectedPlan?.value || '', base, coupon,
    selectedOptions.map(item => [item.value, item.dataset.optionPrice]).sort((a, b) => a[0].localeCompare(b[0]))]);
  return {form, selectedPlan, base, options, subtotal, coupon, fingerprint};
}

function clearCoupon(root) {
  delete root.dataset.couponCode;
  delete root.dataset.couponDiscount;
  delete root.dataset.couponFingerprint;
}

const couponChangedMessage = '購入内容を変更したため、クーポンをもう一度適用してください。';
function updateCheckout(root) {
  const selection = checkoutSelection(root);
  if (!selection) return;
  const {form, selectedPlan, base, options, subtotal, coupon, fingerprint} = selection;
  if (root.dataset.couponFingerprint && root.dataset.couponFingerprint !== fingerprint) {
    clearCoupon(root);
    const feedback = form.querySelector('[data-coupon-feedback]');
    if (feedback) feedback.textContent = couponChangedMessage;
  }
  const discount = coupon && coupon === root.dataset.couponCode && fingerprint === root.dataset.couponFingerprint ? Number(root.dataset.couponDiscount || 0) : 0;
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
document.addEventListener('input', (event) => {
  if (event.target.matches('[data-coupon]')) updateCheckout(event.target.closest('[data-checkout]'));
});
document.addEventListener('change', (event) => {
  if (event.target.matches('[name="billing_type"], [data-option-price], [data-coupon]')) updateCheckout(event.target.closest('[data-checkout]'));
});
document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-apply-coupon]');
  if (!button || button.disabled) return;
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
  const selection = checkoutSelection(root);
  const payload = new FormData(form);
  payload.set('slug', root.dataset.slug || '');
  payload.set('code', code);
  try {
    const response = await fetch('/api/coupons/preview', {method:'POST', body:payload, credentials:'same-origin'});
    const result = await response.json();
    if (checkoutSelection(root)?.fingerprint !== selection.fingerprint) {
      feedback.textContent = couponChangedMessage;
      return;
    }
    if (!response.ok) throw new Error(result.detail || 'クーポンを確認できません');
    if (result.code !== code || !Number.isSafeInteger(result.discount) || result.discount < 0 || result.discount > selection.subtotal) {
      throw new Error('クーポンを確認できません。もう一度お試しください。');
    }
    root.dataset.couponCode = result.code;
    root.dataset.couponDiscount = String(result.discount);
    root.dataset.couponFingerprint = selection.fingerprint;
    input.value = result.code;
    feedback.textContent = result.discount ? `クーポンを適用しました（${yen(result.discount)}割引）` : 'このコードには割引がありません。';
  } catch (error) {
    clearCoupon(root);
    feedback.textContent = checkoutSelection(root)?.fingerprint !== selection.fingerprint ? couponChangedMessage : error.message || 'クーポンを確認できません。';
  } finally {
    button.disabled = false;
    updateCheckout(root);
  }
});

const confirmationForm = document.querySelector('[data-email-confirmation-form]');
if (confirmationForm) {
  const loadConfirmationLink = () => {
    const fragment = new URLSearchParams(location.hash.slice(1));
    const token = fragment.get('token_hash') || '';
    const type = fragment.get('type');
    history.replaceState(null, '', location.pathname);
    confirmationForm.hidden = true;
    confirmationForm.querySelector('[data-confirmation-token]').value = '';
    confirmationForm.querySelector('[data-confirmation-type]').value = '';
    document.querySelector('[data-confirmation-error]').hidden = true;
    if (/^[A-Za-z0-9_-]{32,256}$/.test(token) && ['signup', 'recovery'].includes(type)
        && fragment.getAll('token_hash').length === 1 && fragment.getAll('type').length === 1) {
      confirmationForm.querySelector('[data-confirmation-token]').value = token;
      confirmationForm.querySelector('[data-confirmation-type]').value = type;
      confirmationForm.hidden = false;
      document.querySelector('[data-confirmation-title]').textContent = type === 'recovery' ? 'パスワードの再設定' : 'メールアドレスの確認';
      document.querySelector('[data-confirmation-submit]').textContent = type === 'recovery' ? '新しいパスワードの入力へ' : 'メールアドレスを確認する';
    } else {
      document.querySelector('[data-confirmation-error]').hidden = false;
    }
  };
  loadConfirmationLink();
  window.addEventListener('hashchange', () => {
    const fragment = new URLSearchParams(location.hash.slice(1));
    if (fragment.has('token_hash') || fragment.has('type')) loadConfirmationLink();
  });
}

const recoveryForm = document.querySelector('[data-recovery-form]');
if (recoveryForm) {
  const tokenInput = recoveryForm.querySelector('[data-recovery-token]');
  const error = document.querySelector('[data-recovery-error]');
  const serverError = document.querySelector('[data-recovery-server-error]');
  const validToken = token => typeof token === 'string' && token.length >= 20 && token.length <= 4096;
  const clearPasswords = () => recoveryForm.querySelectorAll('[data-password-input]').forEach(input => { input.value = ''; });
  const loadRecoveryLink = (useSuppliedToken = false) => {
    const hash = new URLSearchParams(location.hash.slice(1));
    const suppliedToken = useSuppliedToken ? tokenInput.value : '';
    const fragmentToken = hash.get('access_token');
    const validFragment = hash.getAll('access_token').length === 1 && hash.getAll('type').length === 1
      && hash.get('type') === 'recovery' && validToken(fragmentToken)
      && !['error', 'error_code', 'error_description', 'token_hash'].some(key => hash.has(key));
    const token = validToken(suppliedToken) ? suppliedToken : validFragment ? fragmentToken : '';
    // Strip successful and failed fragments without copying any credential to storage.
    history.replaceState(null, '', location.pathname);
    clearPasswords();
    if (!useSuppliedToken && serverError) serverError.hidden = true;
    tokenInput.value = token;
    recoveryForm.hidden = !token;
    // The server's classified error already explains this attempt.
    error.hidden = Boolean(token || (serverError && !serverError.hidden));
  };
  loadRecoveryLink(true);
  window.addEventListener('hashchange', () => {
    const hash = new URLSearchParams(location.hash.slice(1));
    if (['access_token', 'type', 'token_hash', 'error', 'error_code', 'error_description'].some(key => hash.has(key))) {
      loadRecoveryLink();
    } else if (location.hash) {
      history.replaceState(null, '', location.pathname);
    }
  });
  window.addEventListener('pageshow', event => { if (event.persisted) clearPasswords(); });
}

const recoveryMfaForm = document.querySelector('[data-recovery-mfa-form]');
if (recoveryMfaForm) {
  // Leave one-time-code autofill intact; clear only stale back/forward state.
  window.addEventListener('pageshow', event => {
    if (event.persisted) recoveryMfaForm.querySelector('[data-recovery-mfa-code]').value = '';
  });
}

const chatMessages = document.querySelector('[data-chat-messages]');
if (chatMessages) chatMessages.scrollTop = chatMessages.scrollHeight;
// Keep save feedback in view instead of leaving the user at an empty upload input.
const saveFeedback = document.querySelector('[data-save-feedback]');
if (saveFeedback) {
  const revealSaveFeedback = () => requestAnimationFrame(() => {
    saveFeedback.focus({preventScroll: true});
    saveFeedback.scrollIntoView({block: 'start', behavior: 'instant'});
  });
  // pageshow runs after the browser restores the previous page's scroll position.
  if (document.readyState === 'complete') revealSaveFeedback();
  else window.addEventListener('pageshow', revealSaveFeedback, {once: true});
}
// Instant fulfillment requires a private artifact before a listing can be submitted.
document.querySelectorAll('[data-instant-fields]').forEach((panel) => {
  const form = panel.closest('form');
  const mode = form.querySelector('[name="fulfillment_type"]');
  const priceType = form.querySelector('[name="price_type"]');
  const file = panel.querySelector('[data-instant-file]');
  const guide = panel.querySelector('[data-instant-guide]');
  const inputs = panel.querySelector('[data-instant-inputs]');
  if (!mode || !file || !guide) return;
  const update = () => {
    const instant = mode.value === 'instant' && priceType?.value !== 'consultation';
    inputs.hidden = !instant;
    file.disabled = guide.disabled = !instant;
    file.required = instant && panel.dataset.artifactReady !== 'true';
    guide.required = instant && (file.required || file.files.length > 0);
    file.setCustomValidity(file.files[0]?.size > 50000000 ? '納品ファイルは50MB以下にしてください。' : '');
  };
  mode.addEventListener('change', update);
  priceType?.addEventListener('change', update);
  file.addEventListener('change', update);
  update();
});
