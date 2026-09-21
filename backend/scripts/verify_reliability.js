const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {App, ctx, elements} = require('./app_vm')();
for (const name of ['collect', 'import', 'overview']) vm.runInContext(fs.readFileSync(path.resolve(__dirname, '../../frontend/js/' + name + '.js'), 'utf8'), ctx);
const Collect = vm.runInContext('CollectPage', ctx), Import = vm.runInContext('ImportPage', ctx), Overview = vm.runInContext('OverviewPage', ctx);
App._onExternalInput = App.refreshAllPages = () => {};
Collect.loadHistory = Collect.resetManual = () => {};
Import.loadLogs = Import.cancelImport = () => {};
let toast = '';
App.showToast = text => {toast = text;};
Object.assign(elements, {
    'manual-category': {value: 'ash_meter'}, 'manual-field-value': {value: '7.8'},
    'manual-field-belt': {value: '502'}, 'manual-time': {value: '2026-09-21T08:00'}, 'manual-remark': {value: ''},
    'import-category': {value: 'ash_density'}, 'import-dedup': {checked: false},
});
App.store.calcLogs = []; App.store.manualEntries = []; App.store.importLogs = [];
Collect.submitManual();
assert.equal(JSON.parse(App.store.calcLogs[0].input_json).value, 7.8);
assert.equal(App.latestBeltAsh('502'), 7.8);
elements['manual-field-value'].value = '7bad';
Collect.submitManual();
assert.equal(App.store.calcLogs.length, 1);
assert.match(toast, /有效数值/);
App.store.calcLogs = [];
function importBelts(rows) {
    Import.parsedData = {headers: ['时间', '系统', '皮带', '灰分', '密度'], rows, duplicates: [], errors: []};
    Import.confirmImport();
}
const ts = '2026-09-21 08:01:00';
importBelts([[ts, 'A', '501', '9.1', '1.47'], [ts, 'A', '502', '8.2', '1.48']]);
assert.equal(App.store.calcLogs.length, 2);
importBelts([[ts, 'A', '501', '9.2', '-']]);
assert.equal(App.store.calcLogs.length, 2);
const byBelt = Object.fromEntries(App.store.calcLogs.map(r => {const v = JSON.parse(r.input_json); return [v.belt, v];}));
assert.equal(byBelt['501'].density, 1.47);
assert.equal(byBelt['502'].ash_content, 8.2);
App.store.totalAshManualOn = true;
App.store.ashInputs = {totalAsh: {manual: 9.3, manualAt: 1790000000000}};
const facts = Overview._measurementFacts({scheme: 'total'}, 'merged');
assert.match(facts, /来源 人工输入/); assert.match(facts, /录入/); assert.match(facts, /测量时间未知/);
App.store.heavyAshManualOn = false; App.store.instrumentInputs = {};
const source = App.ashMeasurementInfo('heavy');
assert.equal(source.source, '502灰分密度导入');
assert.equal(source.sampleAt, new Date(ts.replace(' ', 'T')).getTime());

(async () => {
    // 启动拉取必须结束之后才能执行待发重试。
    ctx.window.location.protocol = 'http:';
    App.store._revision = 'base';
    App._syncReady = true;
    const slots = new Map();
    ctx.localStorage = {getItem: k => slots.get(k) || null, setItem: (k,v) => slots.set(k,v), removeItem: k => slots.delete(k)};
    slots.set('dmcs_store', JSON.stringify({_revision: 'persisted'}));
    App.loadStore(); assert.equal(App.store._revision, 'persisted');
    App.store._revision = 'base';
    let calls = [], release;
    ctx.window.Api = {putStateBody: body => {calls.push(JSON.parse(body)); return new Promise(r => {release = r;});}};
    App._scheduleMirror(JSON.stringify(App.store)); App._flushMirror(false);
    App.store.ashTarget = 8.6;
    App._scheduleMirror(JSON.stringify(App.store)); App._flushMirror(false);
    assert.equal(calls.length, 1, '同一浏览器只允许一个快照请求在途');
    release({ok: true, revision: 'next'});
    await new Promise(r => setTimeout(r, 0));
    assert.equal(calls.length, 2); assert.equal(calls[1]._revision, 'next');
    release({ok: false, conflict: true, reason: 'external change'});
    await new Promise(r => setTimeout(r, 0));
    assert.equal(App._mirrorConflict, true); assert.ok(App._readPendingSlot());
    App._flushMirror(false); assert.equal(calls.length, 2);
    App._mirrorConflict = false; App._mirrorPending = null; App._clearPendingSlot(null);
    App.store.heavySamples = [{timestamp: ts, rho: 1.47, ash_content: 8.2}];
    let key;
    ctx.window.Api.syncHeavySample = async sample => {key = sample.client_id; throw Error('offline');};
    await App.syncHeavySamples(); assert.ok(key); assert.ok(!App.store.heavySamples[0].synced);
    ctx.window.Api.syncHeavySample = async sample => {assert.equal(sample.client_id, key); return {id: 4, synced: true};};
    await App.syncHeavySamples(); assert.equal(App.store.heavySamples[0].synced, true);
    App._hadLocalStore = true; delete App.store._revision;
    ctx.window.Api.getState = async () => ({_revision: 'server', coarseCoal: [], heavySamples: []});
    await App.autoPullIfStale();
    assert.equal(App._mirrorConflict, true); assert.ok(App._readPendingSlot());
    assert.equal(App.store.heavySamples[0].id, 4, '升级时不得自动覆盖已有本地数据');
    console.log('PASS: 数值补录、皮带隔离、来源时间、串行镜像、冲突保留和采样重试');
})().catch(e => {console.error(e); process.exitCode = 1;});
