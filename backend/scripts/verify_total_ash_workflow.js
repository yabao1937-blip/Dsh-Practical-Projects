// 无浏览器、无持久化：回归用户的「人工总灰分 → 建议 → 调密 → 新采样」操作链。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

let now = 1_790_000_000_000;
class TestDate extends Date { static now() { return now; } }
const elements = {
    'collect-tbody': { innerHTML: '' },
    'totalash-source': { value: 'manual' },
    'manual-category': { value: 'heavy_ash_sample' },
    'manual-field-value': { value: '8.2' },
    'manual-field-density': { value: '1.47' },
    'manual-time': { value: '2026-09-21T09:00' },
    'manual-remark': { value: '工作流回归' },
    'manual-history-filter': { value: 'all' },
    'manual-history-tbody': { innerHTML: '' },
};
const ctx = vm.createContext({
    Date: TestDate, console, assert, elements, window: {location: {protocol: "file:"}},
    document: { addEventListener() {}, getElementById: id => elements[id] || null,
        querySelectorAll: () => [] },
});
for (const file of ['app.js', 'collect.js', 'overview.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../../frontend/js', file), 'utf8'), ctx);
}
vm.runInContext(`
    App.saveStore = () => {};
    App._logDensityDecision = () => {};
    let lastToast = '';
    App.showToast = message => { lastToast = message; };
    let detail = '';
    App.openModal = (title, html) => { detail = html; };
    App.store.guideScheme = 'total';
    App.store.ashTarget = 8.5;
    App.store.ashTargetTol = 0.1;
    App.setAmountInput('denseAmount', { manual: 400 });
    App.setAmountInput('floatAmount', { manual: 30 });
    App.setAmountInput('coarseAmount', { manual: 40 });
    App.setFloatAshInput({ manual: 9.5 });
    App.setCoarseAshInput({ manual: 13 });
    App.setHeavyAshInput({ manual: 8 });
    App.setInstrumentInput('density', { manual: 1.49 });
    App.store.densityActionLatch = null;
    App.store.densityLastMoveAt = 0;
    App.setAshInput('totalAsh', { manual: 9.3 });
    const manualAt = App.store.ashInputs.totalAsh.manualAt;
    const guide = App.computeDensityGuidance();
    assert.equal(guide.rhoCur, 1.49);
    assert.equal(guide.rhoNew, 1.47);
    assert.equal(guide.actualTotal, 9.3);
    assert.equal(App.backCalcHeavyAsh(), 8.915);
    assert.equal(App.backCalcHeavyAsh(8.5), 7.975);
    App.setInstrumentInput('density', { manual: guide.rhoNew });
    assert.equal(App.computeDensityGuidance().hold, true);
    assert.equal(App.backCalcHeavyAsh(), 8.915);
    assert.equal(App.backCalcHeavyAsh(8.5), 7.975);
    assert.equal(App.getHeavyAsh(), 8);
    CollectPage.renderTable();
    const heavyRow = elements['collect-tbody'].innerHTML.split('</tr>').find(row => row.includes('<td>重介精煤灰分</td>'));
    assert.match(heavyRow, /目标重介灰分 7.975%/);
    assert.doesNotMatch(heavyRow, /计算 [0-9]/);
    assert.ok(!elements['collect-tbody'].innerHTML.includes('<td>重介灰分反推值</td>'));
    assert.ok(!elements['collect-tbody'].innerHTML.includes('<td>目标重介灰分</td>'));
    assert.match(elements['collect-tbody'].innerHTML, /heavyash-source/);
    assert.doesNotMatch(elements['collect-tbody'].innerHTML, /冻结/);
    OverviewPage.showCardDetail('total');
    assert.match(detail, /8.915/);
    assert.match(detail, /7.975/);
    assert.match(detail, /等待下一次人工采样/);
    assert.doesNotMatch(detail, /重介灰分预测\(调密后\)/);
`, ctx);
now += 1_000;
vm.runInContext(`
    // 新采样不能被人工总灰分开关拦住，也不能覆盖这份总灰分或解开同数据动作闩锁。
    CollectPage.submitManual();
    assert.equal(App.store.heavySamples.length, 1);
    assert.equal(App.getHeavyAsh(), 8.2);
    assert.equal(App.resolveTotalAsh(), 9.3);
    assert.equal(App.store.ashInputs.totalAsh.manualAt, manualAt);
    assert.equal(App.computeDensityGuidance().hold, true);
    assert.equal(App.formulaTotalAsh(), 8.6915);
    assert.match(lastToast, /公式核算总灰分/);
    assert.doesNotMatch(lastToast, /实际总灰分/);
    // 组分更新只改核算值，显式人工来源不能被自动层接管。
    App.setFloatAshInput({ manual: 10 });
    App.setCoarseAshInput({ manual: 12 });
    App.setAmountInput('floatAmount', { manual: 35 });
    App.setInstrumentInput('scale_501', { manual: 280 });
    assert.equal(App.resolveFloatAsh(), 10);
    assert.equal(App.resolveCoarseAsh(), 12);
    assert.equal(App.resolveAmount('floatAmount'), 35);
    assert.equal(App.resolveInstrument('scale_501'), 280);
    App.store.autoState.totalAsh = { v: 8.1, t: Date.now() };
    assert.equal(App.resolveTotalAsh(), 9.3);
    elements['totalash-source'].value = 'calc';
    CollectPage.onTotalAshSourceChange();
    assert.equal(App.resolveTotalAshEx().source, 'formula');
    assert.equal(App.store.ashInputs.totalAsh.manual, 9.3);
    assert.equal(App.store.ashInputs.totalAsh.manualAt, manualAt);
    assert.match(elements['collect-tbody'].innerHTML, /已保存人工值：9.30/);
    // 模拟保存/重新装载，来源与人工值均保留，再切回时不伪造新的化验时间。
    App.store = JSON.parse(JSON.stringify(App.store));
    App.toggleTotalAshManual();
    assert.equal(App.resolveTotalAsh(), 9.3);
    assert.equal(App.totalInputLayer('totalAsh'), '手动');
    assert.equal(App.store.ashInputs.totalAsh.manualAt, manualAt);
    assert.equal(App.computeDensityGuidance().hold, true);
    // 缺数据时不得复用历史反推值，反推也不再被旧缓存或显示次数影响。
    App.store.heavyAshBackcalc = 7;
    assert.equal(App.backCalcHeavyAsh(), App.backCalcHeavyAsh());
    App.setAmountInput('denseAmount', { manual: 0 });
    assert.equal(App.backCalcHeavyAsh(), null);
    assert.match(elements['collect-tbody'].innerHTML, /目标重介灰分 —/);
    App.setAshInput('totalAsh', { manual: null });
    assert.equal(App.store.ashInputs.totalAsh.manual, null);
    assert.equal(App.store.totalAshManualOn, false);
`, ctx);
console.log('PASS: 人工总灰分、调密保持、新采样、来源往返、组分核算及缺数据处理');
