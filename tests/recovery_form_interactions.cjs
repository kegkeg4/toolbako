// Executes the actual application JS offline with synthetic recovery DOM values.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const TOKEN = 'synthetic-token-' + 'x'.repeat(24);

function recovery({token = '', hash = '', serverError = false} = {}) {
  const tokenInput = {value: token};
  const passwordInputs = [{value: 'synthetic-password-one'}, {value: 'synthetic-password-two'}];
  const error = {hidden: true};
  const server = serverError ? {hidden: false} : null;
  const form = {hidden: false, querySelector: () => tokenInput,
    querySelectorAll: selector => selector === '[data-password-input]' ? passwordInputs : []};
  const nodes = new Map([['[data-recovery-form]', form], ['[data-recovery-error]', error],
    ['[data-recovery-server-error]', server]]);
  const document = {querySelector: selector => nodes.get(selector) || null,
    querySelectorAll: () => [], addEventListener() {}};
  Object.defineProperty(document, 'cookie', {get() { throw Error('Cookie access forbidden'); },
    set() { throw Error('Cookie writes forbidden'); }});
  const listeners = new Map(), rewritten = [];
  const location = {hash, pathname: '/auth/recovery', href: 'https://synthetic.invalid/auth/recovery'};
  const window = {addEventListener(name, handler) {
    listeners.set(name, [...(listeners.get(name) || []), handler]);
  }};
  const context = {document, window, location, URL, URLSearchParams,
    history: {replaceState(_state, _title, url) { rewritten.push(url); location.hash = ''; }},
    fetch() { throw Error('Network forbidden'); }};
  for (const name of ['localStorage', 'sessionStorage']) {
    Object.defineProperty(context, name, {get() { throw Error('Storage forbidden'); }});
  }
  vm.createContext(context);
  vm.runInContext(source, context);
  return {form, tokenInput, passwordInputs, error, server, location, rewritten,
    visibleErrors: () => Number(!error.hidden) + Number(server !== null && !server.hidden),
    emit: (name, event = {}) => (listeners.get(name) || []).forEach(handler => handler(event)),
    newHash(value) { location.hash = value; this.emit('hashchange'); }};
}

test('a server rejection without a token displays exactly one error', () => {
  const f = recovery({serverError: true});
  assert.equal(f.form.hidden, true);
  assert.equal(f.error.hidden, true);
  assert.equal(f.visibleErrors(), 1);
});

test('a correctable server rejection retains the private token and one error', () => {
  const f = recovery({serverError: true, token: TOKEN});
  assert.equal(f.form.hidden, false);
  assert.equal(f.tokenInput.value, TOKEN);
  assert.equal(f.visibleErrors(), 1);
  assert.ok(f.passwordInputs.every(input => input.value === ''));
});

test('a linkless entry displays one generic error', () => {
  const f = recovery();
  assert.equal(f.form.hidden, true);
  assert.equal(f.visibleErrors(), 1);
});

for (const length of [20, 4096]) {
  test(`a single legacy recovery fragment accepts bounded length ${length}`, () => {
    const token = 'x'.repeat(length);
    const f = recovery({hash: '#access_token=' + token + '&type=recovery'});
    assert.equal(f.form.hidden, false);
    assert.equal(f.visibleErrors(), 0);
    assert.equal(f.tokenInput.value, token);
    assert.deepEqual(f.rewritten, ['/auth/recovery']);
    assert.equal(f.location.hash, '');
  });
}

const invalidFragments = [
  '#access_token=' + 'x'.repeat(19) + '&type=recovery',
  '#access_token=' + 'x'.repeat(4097) + '&type=recovery',
  '#access_token=' + TOKEN + '&access_token=another-synthetic-token&type=recovery',
  '#access_token=' + TOKEN + '&type=recovery&type=recovery',
  '#access_token=' + TOKEN + '&type=signup',
  '#access_token=' + TOKEN + '&type=recovery&error=access_denied',
  '#access_token=' + TOKEN + '&type=recovery&error_code=synthetic_error',
  '#access_token=' + TOKEN + '&type=recovery&error_description=synthetic_error',
  '#token_hash=synthetic-hash&type=recovery',
];
invalidFragments.forEach((hash, index) => {
  test(`invalid or failed fragment ${index + 1} is cleared without retaining a token`, () => {
    const f = recovery({hash});
    assert.equal(f.form.hidden, true);
    assert.equal(f.visibleErrors(), 1);
    assert.equal(f.tokenInput.value, '');
    assert.equal(f.location.hash, '');
    assert.deepEqual(f.rewritten, ['/auth/recovery']);
  });
});

test('server-rendered token has precedence at initial load and the fragment is scrubbed', () => {
  const f = recovery({token: TOKEN, hash: '#error_description=synthetic-private-detail'});
  assert.equal(f.form.hidden, false);
  assert.equal(f.tokenInput.value, TOKEN);
  assert.equal(f.visibleErrors(), 0);
  assert.equal(f.location.hash, '');
});

test('a new valid fragment on the same document restores the form and hides a stale error', () => {
  const f = recovery({serverError: true});
  f.newHash('#access_token=' + TOKEN + '&type=recovery');
  assert.equal(f.form.hidden, false);
  assert.equal(f.tokenInput.value, TOKEN);
  assert.equal(f.visibleErrors(), 0);
  assert.equal(f.location.hash, '');
});

test('a new valid fragment replaces the old private token without storing either', () => {
  const f = recovery({token: TOKEN});
  const next = 'new-synthetic-token-' + 'y'.repeat(24);
  f.newHash('#access_token=' + next + '&type=recovery');
  assert.equal(f.form.hidden, false);
  assert.equal(f.tokenInput.value, next);
  assert.equal(f.visibleErrors(), 0);
});

test('non-auth anchors do not invalidate an existing token', () => {
  const f = recovery({token: TOKEN});
  f.newHash('#unrelated-section');
  assert.equal(f.form.hidden, false);
  assert.equal(f.tokenInput.value, TOKEN);
  assert.equal(f.visibleErrors(), 0);
});

test('back-forward restoration clears password fields without erasing the private recovery token', () => {
  const f = recovery({token: TOKEN});
  f.passwordInputs.forEach(input => { input.value = 'synthetic-restored-password'; });
  f.emit('pageshow', {persisted: true});
  assert.ok(f.passwordInputs.every(input => input.value === ''));
  assert.equal(f.tokenInput.value, TOKEN);
  assert.equal(f.form.hidden, false);
});
