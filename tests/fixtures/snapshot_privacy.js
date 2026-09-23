const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync(process.argv[2], 'utf8');
const sentinel = 'profile-sentinel-48293@example.test';

function run(privateValues) {
  const body = {innerText: `Account ${sentinel}`};
  const input = {
    tagName: 'INPUT', type: 'text', value: sentinel, labels: [], parentElement: body,
    isConnected: true, readOnly: false, disabled: false, form: null,
    getAttribute(key) { return key === 'aria-label' ? 'Email' : null; },
    closest() { return null; }, matches() { return false; },
    checkVisibility() { return true; },
    getBoundingClientRect() { return {x: 1, y: 1, width: 180, height: 24}; },
  };
  const document = {
    body, title: 'Account', activeElement: input,
    documentElement: {scrollHeight: 600},
    querySelectorAll(selector) {
      if (selector === 'input,textarea,select' || selector === 'input') return [input];
      if (selector.includes('a[href],button,input')) return [input];
      return [];
    },
    querySelector() { return null; },
    getElementById() { return null; },
    createTreeWalker() { return {nextNode() { return null; }}; },
    createRange() { return {}; },
  };
  const context = {
    document, window: {}, location: {href: 'https://example.test/account'},
    performance: {timeOrigin: 1}, NodeFilter: {SHOW_TEXT: 4},
    innerWidth: 800, innerHeight: 600, scrollX: 0, scrollY: 0, privateValues,
  };
  return vm.runInNewContext(source, context);
}

const unprotected = run([]);
const protectedPage = run([sentinel]);
if (!JSON.stringify(unprotected).includes(sentinel)) throw Error('fixture did not expose sentinel');
if (JSON.stringify(protectedPage).includes(sentinel)) throw Error('private literal leaked from snapshot');
if (protectedPage.actions[0].value !== '[private]') throw Error('input value was not masked');
process.stdout.write('snapshot privacy ok\n');
