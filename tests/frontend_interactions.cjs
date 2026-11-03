// Offline event/DOM unit harness: node --test tests/frontend_interactions.cjs
// No browser, packages, server, network, payment, account or email access.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');

class Node {
  constructor(attributes = {}) {
    Object.assign(this, {dataset: {}, disabled: false, hidden: false, value: '', textContent: '', checked: false}, attributes);
    this.attributes = new Map(); this.queries = new Map(); this.parents = new Map(); this.selectors = new Set();
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) { return this.queries.get(selector) || []; }
  closest(selector) { return this.matches(selector) ? this : this.parents.get(selector) || null; }
  matches(selector) { return selector.split(',').some(part => this.selectors.has(part.trim())); }
  setAttribute(name, value) { this.attributes.set(name, value); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
}

function environment(checkout = false) {
  const listeners = new Map(), windowListeners = new Map(), timers = [], document = new Node();
  const fixture = {document, timers};
  const on = (collection, name, callback) => collection.set(name, [...(collection.get(name) || []), callback]);
  document.addEventListener = (name, callback) => on(listeners, name, callback);
  const window = {setTimeout: callback => timers.push(callback), confirm: () => true,
    addEventListener: (name, callback) => on(windowListeners, name, callback)};
  fixture.fire = (name, target, submitter) => {
    const event = {target, submitter, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }};
    for (const callback of listeners.get(name) || []) callback(event);
    return event;
  };
  fixture.fireWindow = name => (windowListeners.get(name) || []).forEach(callback => callback({persisted: true}));
  fixture.tick = () => { while (timers.length) timers.shift()(); };
  fixture.on = (name, callback) => on(listeners, name, callback);
  fixture.form = new Node({method: 'post', action: 'http://127.0.0.1/synthetic-save'});
  fixture.save = new Node({form: fixture.form, type: 'submit'});
  fixture.locked = new Node({disabled: true, form: fixture.form, type: 'submit'});
  if (checkout) {
    const root = new Node({dataset: {oneTime: '1000', demo: 'false', slug: 'synthetic'}});
    const form = fixture.form, coupon = new Node(), feedback = new Node();
    const billing = new Node({value: 'one_time', dataset: {basePrice: '1000'}});
    const option = new Node({value: 'additional', checked: false, dataset: {optionPrice: '500'}});
    const apply = new Node({form});
    apply.selectors.add('[data-apply-coupon]');
    apply.parents.set('[data-checkout]', root); apply.parents.set('form', form);
    coupon.selectors.add('[data-coupon]'); option.selectors.add('[data-option-price]');
    billing.selectors.add('[name="billing_type"]');
    [coupon, option, billing].forEach(input => input.parents.set('[data-checkout]', root));
    form.queries.set('[data-coupon]', [coupon]); form.queries.set('[data-coupon-feedback]', [feedback]);
    form.queries.set('[name="billing_type"]', [billing]); form.queries.set('[data-option-price]', [option]);
    const originalQuery = form.querySelectorAll.bind(form);
    form.querySelectorAll = selector => selector === '[data-option-price]:checked' ? (option.checked ? [option] : []) : originalQuery(selector);
    root.queries.set('[data-checkout-form]', [form]); document.queries.set('[data-checkout]', [root]);
    for (const name of ['summary-base', 'summary-options', 'options-row', 'summary-discount', 'discount-row', 'summary-total', 'base-label', 'total-label']) {
      root.queries.set(`[data-${name}]`, [new Node()]);
    }
    Object.assign(fixture, {root, coupon, feedback, billing, option, apply});
  }
  let formatterCount = 0;
  const context = {window, document, URL, URLSearchParams, location: {href: 'http://127.0.0.1/fixture', pathname: '/fixture'},
    history: {}, requestAnimationFrame: callback => callback(),
    Intl: {NumberFormat: function (...args) { formatterCount += 1; return new Intl.NumberFormat(...args); }},
    FormData: class { constructor() { this.fields = new Map(); } set(name, value) { this.fields.set(name, value); } get(name) { return this.fields.get(name); } },
    fetch: async () => { throw new Error('Network prohibited'); }};
  vm.createContext(context); vm.runInContext(source, context);
  fixture.context = context; fixture.window = window; fixture.formatterCount = () => formatterCount;
  return fixture;
}

test('a cancelled submit never leaves the save button disabled', () => {
  const f = environment(); f.on('submit', event => event.preventDefault());
  f.fire('submit', f.form, f.save); f.tick();
  assert.equal(f.save.disabled, false); assert.equal(f.save.getAttribute('aria-busy'), null);
});

test('pageshow restores only temporary disabled state, preserving permission-disabled controls', () => {
  const f = environment(); f.fire('submit', f.form, f.save); f.tick();
  assert.equal(f.save.disabled, true); f.fireWindow('pageshow');
  assert.equal(f.save.disabled, false); assert.equal(f.save.getAttribute('aria-busy'), null);
  assert.equal(f.locked.disabled, true);
});

test('a duplicate POST is blocked before deferred disable runs', () => {
  const f = environment();
  assert.equal(f.fire('submit', f.form, f.save).defaultPrevented, false);
  assert.equal(f.fire('submit', f.form, f.save).defaultPrevented, true);
});

test('read-only GET filters do not get permanent submit locks', () => {
  const f = environment(); f.form.method = 'get'; f.fire('submit', f.form, f.save); f.tick();
  assert.equal(f.save.disabled, false);
});

test('normal named POST submitters remain successful controls until the request is captured', () => {
  const f = environment(); f.save.name = 'decision'; f.save.value = 'approve';
  assert.equal(f.fire('submit', f.form, f.save).defaultPrevented, false);
  assert.equal(f.save.disabled, false); assert.equal(f.save.name, 'decision'); assert.equal(f.save.value, 'approve');
  f.tick(); assert.equal(f.save.disabled, true); assert.equal(f.save.getAttribute('aria-busy'), 'true');
});

test('a later-cancelled POST can be retried without a reload', () => {
  const f = environment(); let cancel = true;
  f.on('submit', event => { if (cancel) event.preventDefault(); });
  f.fire('submit', f.form, f.save); f.tick(); cancel = false;
  assert.equal(f.fire('submit', f.form, f.save).defaultPrevented, false);
  f.tick(); assert.equal(f.save.disabled, true);
});

test('a cached page restores the original aria-busy value and permits a new submit', () => {
  const f = environment(); f.save.setAttribute('aria-busy', 'false');
  f.fire('submit', f.form, f.save); f.tick(); f.fireWindow('pageshow');
  assert.equal(f.save.getAttribute('aria-busy'), 'false');
  assert.equal(f.fire('submit', f.form, f.save).defaultPrevented, false);
});

test('a pageshow before a deferred timer does not disable the restored button later', () => {
  const f = environment(); f.fire('submit', f.form, f.save); f.fireWindow('pageshow'); f.tick();
  assert.equal(f.save.disabled, false);
});

test('POST without a submitter still rejects duplicate submissions', () => {
  const f = environment(); assert.equal(f.fire('submit', f.form).defaultPrevented, false);
  f.tick(); assert.equal(f.fire('submit', f.form).defaultPrevented, true);
  f.fireWindow('pageshow'); assert.equal(f.fire('submit', f.form).defaultPrevented, false);
});

test('explicit repeat controls preserve existing behavior', () => {
  const f = environment(); f.save.dataset.allowRepeat = 'true';
  assert.equal(f.fire('submit', f.form, f.save).defaultPrevented, false);
  assert.equal(f.fire('submit', f.form, f.save).defaultPrevented, false);
  f.tick(); assert.equal(f.save.disabled, false);
});

test('effective formmethod and formaction are respected', () => {
  const f = environment(); let confirmation;
  f.save.setAttribute('formmethod', 'get');
  f.save.setAttribute('formaction', '/orders/synthetic/actions/cancel_accept');
  f.window.confirm = message => { confirmation = message; return true; };
  f.fire('submit', f.form, f.save); f.tick();
  assert.equal(f.save.disabled, false); assert.match(confirmation, /運営.*確認.*承認/);
});

test('cancel agreement copy describes review and approval, and dismissed consent does not lock', () => {
  const f = environment(); let confirmation;
  f.form.action = 'http://127.0.0.1/orders/synthetic/actions/cancel_accept';
  f.window.confirm = message => { confirmation = message; return false; };
  assert.equal(f.fire('submit', f.form, f.save).defaultPrevented, true);
  f.tick(); assert.equal(f.save.disabled, false); assert.match(confirmation, /運営.*確認.*承認/);
});

const settled = () => new Promise(resolve => setImmediate(resolve));
async function applyCoupon(f) {
  f.context.fetch = async () => ({ok: true, json: async () => ({code: 'SAVE', discount: 100})});
  f.coupon.value = 'SAVE'; f.fire('input', f.coupon); f.fire('click', f.apply); await settled();
  assert.match(f.root.querySelector('[data-summary-total]').textContent, /900/);
}

test('coupon preview sends the API code field, independently of checkout coupon field', async () => {
  const f = environment(true); let payload;
  f.context.fetch = async (url, options) => {
    payload = options.body; assert.equal(url, '/api/coupons/preview');
    return {ok: true, json: async () => ({code: 'SAVE', discount: 100})};
  };
  f.coupon.value = ' save '; f.fire('click', f.apply); await settled();
  assert.equal(payload.get('code'), 'SAVE'); assert.equal(payload.get('slug'), 'synthetic');
});

test('option changes invalidate the old coupon total without automatic POST', async () => {
  const f = environment(true); await applyCoupon(f);
  f.option.checked = true; f.fire('change', f.option);
  assert.match(f.root.querySelector('[data-summary-total]').textContent, /1,500/);
  assert.equal(f.root.querySelector('[data-discount-row]').hidden, true); assert.match(f.feedback.textContent, /もう一度/);
});

test('an old coupon reply cannot overwrite newer inputs', async () => {
  const f = environment(true); let finish;
  f.context.fetch = () => new Promise(resolve => { finish = resolve; });
  f.coupon.value = 'SAVE'; f.fire('click', f.apply);
  f.coupon.value = 'OTHER'; f.fire('input', f.coupon);
  finish({ok: true, json: async () => ({code: 'SAVE', discount: 100})}); await settled();
  assert.equal(f.coupon.value, 'OTHER');
  assert.match(f.root.querySelector('[data-summary-total]').textContent, /1,000/); assert.match(f.feedback.textContent, /もう一度/);
});

test('invalid API discount cannot show a negative payable total', async () => {
  const f = environment(true);
  f.context.fetch = async () => ({ok: true, json: async () => ({code: 'SAVE', discount: 9999})});
  f.coupon.value = 'SAVE'; f.fire('click', f.apply); await settled();
  assert.match(f.root.querySelector('[data-summary-total]').textContent, /1,000/);
  assert.equal(f.root.querySelector('[data-discount-row]').hidden, true);
});

for (const discount of [-1, 1.5, '100', null]) {
  test(`untrusted discount ${JSON.stringify(discount)} fails closed`, async () => {
    const f = environment(true);
    f.context.fetch = async () => ({ok: true, json: async () => ({code: 'SAVE', discount})});
    f.coupon.value = 'SAVE'; f.fire('click', f.apply); await settled();
    assert.match(f.root.querySelector('[data-summary-total]').textContent, /1,000/);
    assert.equal(f.root.querySelector('[data-discount-row]').hidden, true);
    assert.equal(f.apply.disabled, false);
  });
}

test('a mismatched coupon response cannot replace the requested code', async () => {
  const f = environment(true);
  f.context.fetch = async () => ({ok: true, json: async () => ({code: 'OTHER', discount: 100})});
  f.coupon.value = 'SAVE'; f.fire('click', f.apply); await settled();
  assert.equal(f.coupon.value, 'SAVE'); assert.match(f.root.querySelector('[data-summary-total]').textContent, /1,000/);
});

test('billing plan changes invalidate a previously accepted preview', async () => {
  const f = environment(true); await applyCoupon(f);
  f.billing.value = 'subscription'; f.billing.dataset.basePrice = '2000'; f.fire('change', f.billing);
  assert.match(f.root.querySelector('[data-summary-total]').textContent, /2,000/);
  assert.equal(f.root.querySelector('[data-discount-row]').hidden, true); assert.match(f.feedback.textContent, /もう一度/);
});

test('coupon preview failure unlocks retry without submitting checkout', async () => {
  const f = environment(true); let count = 0;
  f.context.fetch = async url => { count += 1; assert.equal(url, '/api/coupons/preview'); throw new Error('connection failed'); };
  f.coupon.value = 'SAVE'; f.fire('click', f.apply); await settled();
  assert.equal(f.apply.disabled, false); assert.equal(count, 1);
  assert.match(f.root.querySelector('[data-summary-total]').textContent, /1,000/);
  await applyCoupon(f);
});

test('demo coupons require successful preview and free purchases retain nonnegative totals', async () => {
  const f = environment(true); f.root.dataset.demo = 'true'; f.coupon.value = 'WELCOME10'; f.fire('input', f.coupon);
  assert.match(f.root.querySelector('[data-summary-total]').textContent, /1,000/);
  f.context.fetch = async () => ({ok: true, json: async () => ({code: 'WELCOME10', discount: 100})});
  f.fire('click', f.apply); await settled();
  assert.match(f.root.querySelector('[data-summary-total]').textContent, /900/);
  f.billing.dataset.basePrice = '0'; f.fire('change', f.billing);
  assert.equal(f.root.querySelector('[data-summary-total]').textContent, '¥0');
});

test('a rejected demo coupon does not leave an unverified discount visible', async () => {
  const f = environment(true); f.root.dataset.demo = 'true'; f.coupon.value = 'WELCOME10';
  f.context.fetch = async () => ({ok: false, json: async () => ({detail: 'already used'})});
  f.fire('click', f.apply); await settled();
  assert.match(f.root.querySelector('[data-summary-total]').textContent, /1,000/);
  assert.equal(f.root.querySelector('[data-discount-row]').hidden, true);
});

test('amount updates reuse Intl.NumberFormat after initial rendering', () => {
  const f = environment(true), count = f.formatterCount();
  f.coupon.value = 'SAMPLE'; f.fire('input', f.coupon); f.option.checked = true; f.fire('change', f.option);
  assert.equal(f.formatterCount(), count);
});
