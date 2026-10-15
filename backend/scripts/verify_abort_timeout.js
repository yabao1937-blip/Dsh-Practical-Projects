// Regression for older Windows browsers: requests must still time out.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const code = fs.readFileSync(path.join(__dirname, '../../frontend/js/api.js'), 'utf8');

(async () => {
    const timers = [];
    const legacySignal = {};
    const context = vm.createContext({
        window: {}, AbortSignal: legacySignal, AbortController,
        setTimeout: (callback, delay) => timers.push({callback, delay}),
    });
    vm.runInContext(code, context);
    let requested;
    context.fetch = (_url, init) => new Promise((_resolve, reject) => {
        requested = init.signal;
        init.signal.addEventListener('abort', () => reject(new Error('request aborted')));
    });
    const pending = context.window.Api.getHealth();
    assert.equal(requested.aborted, false);
    assert.equal(timers[0].delay, 5000);
    timers[0].callback();
    await assert.rejects(pending, /request aborted/);
    assert.equal(requested.aborted, true);
    const another = legacySignal.timeout(15000);
    assert.equal(another.aborted, false, 'each request gets its own signal');
    assert.equal(timers[1].delay, 15000);
    timers[1].callback();
    assert.equal(another.aborted, true);

    const native = () => 'native';
    const modern = vm.createContext({window: {}, AbortSignal: {timeout: native}, AbortController,
        setTimeout: () => {throw Error('native support must not create fallback timers');}});
    vm.runInContext(code, modern);
    assert.equal(modern.AbortSignal.timeout, native);
    vm.runInContext(code, vm.createContext({window: {}}));
    console.log('Browser timeout regression: legacy request cancellation, independent signals, native preservation and safe loading passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
