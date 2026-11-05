// Recovery-only MFA browser-state checks; executes real JS with no network.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');

function page() {
  const code = {value: '123456'};
  const form = {querySelector(selector) { assert.equal(selector, '[data-recovery-mfa-code]'); return code; }};
  const documentListeners = new Map(), windowListeners = new Map();
  const document = {querySelector: selector => selector === '[data-recovery-mfa-form]' ? form : null,
    querySelectorAll: () => [], addEventListener(name, handler) {
      documentListeners.set(name, [...(documentListeners.get(name) || []), handler]);
    }};
  const window = {addEventListener(name, handler) {
    windowListeners.set(name, [...(windowListeners.get(name) || []), handler]);
  }};
  const context = {document, window, URL, URLSearchParams,
    location: {href: 'https://synthetic.invalid/auth/recovery/mfa', pathname: '/auth/recovery/mfa', hash: ''},
    fetch() { throw Error('Network forbidden'); }};
  vm.createContext(context);
  vm.runInContext(source, context);
  return {code, show: persisted => (windowListeners.get('pageshow') || []).forEach(handler => handler({persisted}))};
}

test('recovery MFA keeps autofill on normal pageshow and never becomes a password form', () => {
  const f = page();
  assert.equal(f.code.value, '123456');
  f.show(false);
  assert.equal(f.code.value, '123456');
});

test('recovery MFA clears a stale code after back-forward cache restoration', () => {
  const f = page();
  f.show(true);
  assert.equal(f.code.value, '');
});
