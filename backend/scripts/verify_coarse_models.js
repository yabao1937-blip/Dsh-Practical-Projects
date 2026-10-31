// GPT 页面缓存、旧模型隔离和训练同步的无网络回归；只训练内存夹具，不保存模型。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {App, ctx, elements} = require('./app_vm')();
const seed = JSON.parse(fs.readFileSync(path.join(__dirname, '../data/train_js.json'), 'utf8'));
App.store.coarseCoal = structuredClone(seed.coarseCoal);
App.store.coarseTrainRange = 'all';
App.store.coarseTolerance = .8;
App.trainCoarseModel('all', 'gpt');
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../frontend/js/coarse.js'), 'utf8'), ctx);
const page = vm.runInContext('CoarsePage', ctx);
elements['coarse-view-mode'] = {value: 'day'};
const first = page._ensureDailyModel();
assert(first);
assert.equal(page._ensureDailyModel(), first);
App.store.coarseCoal[0].ash_content += .5;
const changed = page._ensureDailyModel();
assert.notEqual(changed, first);
assert.notEqual(changed.days[0].ash_content, first.days[0].ash_content);
page.viewModel = 'mlr';
assert.equal(page._dailyPred(changed.days[0], changed), App._coarsePredict(changed.days[0], changed.mlr));
page.viewModel = 'pls';
assert.equal(page._dailyPred(changed.days[0], changed), App._coarsePredict(changed.days[0], changed.pls));
const outside = {...changed.days[0], raw_ash: 99, level: -100};
for (const method of ['mlr', 'pls']) {
    page.viewModel = method;
    assert.equal(page._dailyPred(outside, changed), App._coarsePredict(outside, changed[method]));
    assert(App._coarseCoverage(outside, changed[method]).some(x => x.feature === 'raw_ash'));
}
const sampleModel = App.store.coarseModel.mlr;
assert.equal(App.predictCoarseAsh(outside, 'mlr'), App._coarsePredict(outside, sampleModel));
assert.equal(outside.raw_ash, 99);
assert.equal(App._coarseCoverage(outside, seed.coarseModel.mlr).length, 0);
App.store.coarseTrainRange = '30d';
const limited = page._ensureDailyModel();
assert(limited);
assert(limited.n < changed.n);
App.store.coarseCoal = App.store.coarseCoal.slice(0, 4);
assert.equal(page._ensureDailyModel(), null); // 不残留之前范围的模型
const rows = [{timestamp: '2026-02-30 10:00:00', ash_content: 12},
    {timestamp: '2026-01-01 10:00:00', ash_content: Infinity}];
assert.equal(App._coarseRows(rows).length, 0);

// 无 GPT 时详情不崩溃；缺失预测不能按 0 计入累计灰分。
const beforeDetail = {model: App.store.coarseModel, bank: App.store.coarseModelVariants,
    rows: App.store.coarseCoal, open: App.openModal, pred: page._pred};
App.store.coarseModel = null;
App.store.coarseModelVariants = {};
App.store.coarseCoal = [{timestamp: '2026-10-31 10:00:00', ash_content: 12, coal_amount: 100, level: 50},
    {timestamp: '2026-10-31 10:01:00', ash_content: 13, coal_amount: 200, level: 50}];
elements['coarse-view-mode'].value = 'sample';
let detail;
App.openModal = (_title, html) => { detail = html; };
page.showCardDetail('predict');
assert(detail.includes('模型不可用'));
page.showCardDetail('cumulative');
assert(detail.includes('<strong>—</strong>'));
page._pred = row => row.coal_amount === 100 ? 12 : null;
page.showCardDetail('cumulative');
assert(detail.includes('<strong>12.00%</strong>'));
page._pred = beforeDetail.pred;
App.openModal = beforeDetail.open;
App.store.coarseModel = beforeDetail.model;
App.store.coarseModelVariants = beforeDetail.bank;
App.store.coarseCoal = beforeDetail.rows;

(async () => {

    // 旧存档仍保留，但不能再参与预测或触发旧训练分支。
    const gpt = App.store.coarseModel;
    const preserved = JSON.stringify(gpt), old = structuredClone(seed.coarseModel);
    App.store.coarseModel = old;
    App.store.coarseModelVariants = {gpt, ds: old};
    assert.equal(App.coarseEngine(), 'unavailable');
    assert.equal(App.getCoarseModel('mlr'), gpt.mlr);
    const example = seed.coarseCoal[0];
    assert.equal(App.predictCoarseAsh(example, 'mlr'), App._coarsePredict(example, gpt.mlr));
    assert.equal(App.trainCoarseModel('all', 'ds'), false);
    assert.equal(await App.retrainCoarseModelAsync('all', 'ds'), false);
    assert.equal(App.coarseEngine({...old, engine: 'gpt'}), 'unavailable', 'A name cannot upgrade old parameters');
    assert.equal(App.coarseEngine({...gpt, engine: 'ds'}), 'unavailable', 'Explicit retired engine is rejected');
    for (const name of ['switchCoarseEngine', '_trainCoarseDs', 'trainMlr', 'trainPls', 'DEFAULT_COARSE_MODEL'])
        assert.equal(typeof App[name], 'undefined', name + ' is retired');
    for (const name of ['switchEngine', '_trainDailyDs', 'loadCoarseForward'])
        assert.equal(typeof page[name], 'undefined', name + ' is retired');
    delete App.store.coarseModelVariants.gpt;
    assert.equal(App.getCoarseModel(), null);
    assert.equal(App.predictCoarseAsh(example), null, 'No zero-valued or factory fallback');
    App.store.coarseCoal = [structuredClone(example)];
    App.refreshCoarsePredictions();
    assert.equal(App.store.coarseCoal[0].predicted_ash, null);
    App.store.coarseModel = gpt;
    assert.equal(JSON.stringify(gpt), preserved);
    assert.equal(JSON.stringify(old), JSON.stringify(seed.coarseModel), 'Historic snapshot was not modified');
    const html = fs.readFileSync(path.join(__dirname, '../../frontend/index.html'), 'utf8');
    assert(!html.includes('coarse-engine-ds') && !html.includes('CoarsePage.switchEngine'));
    ctx.window.location.protocol = 'http:';
    App.store._revision = 'before';
    App.store.coarseCoal = seed.coarseCoal;
    const result = App._trainCoarseRows(App.store.coarseCoal);
    App.flushMirrorNow = async () => ({ok: true, revision: 'before'});
    let skipMirror = null, called = 0;
    App.saveStore = skip => { skipMirror = skip; };
    ctx.window.Api = {retrainCoarseModel: async (range, revision, engine) => {
        assert.equal(engine, 'gpt');
        assert.equal(revision, 'before'); called++;
        return {ok: true, revision: 'after', coarseModel: result};
    }};
    assert.equal(await App.retrainCoarseModelAsync('all', 'gpt'), true);
    assert.equal(App.store._revision, 'after');
    assert.equal(skipMirror, true);
    App.flushMirrorNow = async () => ({ok: false});
    assert.equal(await App.retrainCoarseModelAsync('all'), false);
    assert.equal(called, 1);
    App.flushMirrorNow = async () => ({ok: true, revision: 'after'});
    ctx.window.Api.retrainCoarseModel = async () => ({ok: true, coarseModel: result});
    assert.equal(await App.retrainCoarseModelAsync('all', 'ds'), false); // 旧后端不能把 GPT 冒充 DS。
    assert.equal(App.coarseEngine(), 'gpt');
    assert.equal(App.store._revision, 'after');
    ctx.window.Api.retrainCoarseModel = async () => {
        App.store.coarseCoal[0].ash_content += 1;
        return {ok: true, revision: 'unexpected', coarseModel: result};
    };
    assert.equal(await App.retrainCoarseModelAsync('all'), false);
    assert.equal(App.store._revision, 'after');
    assert.equal(App._mirrorConflict, true);
    console.log('GPT: compatible predictions, retired-model isolation, daily cache, range and training sync passed');
})().catch(e => { console.error(e); process.exitCode = 1; });
