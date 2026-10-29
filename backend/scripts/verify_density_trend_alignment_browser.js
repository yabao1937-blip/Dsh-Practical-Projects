// Read-only browser acceptance: original plot round-trip and archived-input alignment.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const {chromium} = require(process.env.DMCS_PLAYWRIGHT_MODULE || 'playwright');
const url = process.env.DMCS_VERIFY_URL || 'http://127.0.0.1:8000';
const output = path.resolve(__dirname, '../../output/playwright/density-trend-alignment-20261029');

(async () => {
    fs.mkdirSync(output, {recursive: true});
    const browser = await chromium.launch({channel: 'chrome', headless: true});
    const report = {passed: false, mutations: 0, errors: []};
    try {
        const context = await browser.newContext({viewport: {width: 1500, height: 1100}});
        await context.route('**/api/**', route => {
            if (!['GET', 'HEAD', 'OPTIONS'].includes(route.request().method())) {
                report.mutations++; return route.abort();
            }
            return route.continue();
        });
        const page = await context.newPage();
        page.on('pageerror', error => report.errors.push(error.message));
        await page.goto(url);
        await page.waitForFunction(() => App.__ready, null, {timeout: 60000});
        await page.locator('.nav-item[data-page="page-density-trend"]').click();
        await page.waitForFunction(() => DensityTrendPage.meta && !DensityTrendPage.loading);
        await page.evaluate(async () => { await document.fonts.ready; clearInterval(DensityTrendPage.timer); });
        const toggle = page.locator('#density-trend-alignment');
        assert.equal(await toggle.getAttribute('aria-pressed'), 'false');
        const before = await page.evaluate(() => {
            const chart = DensityTrendPage;
            window._alignmentBefore = {meta: JSON.stringify(chart.meta), series: JSON.stringify(chart.series),
                ash: document.getElementById('density-trend-ash-canvas').toDataURL(),
                density: document.getElementById('density-trend-canvas').toDataURL(),
                actions: document.getElementById('density-trend-actions').innerHTML};
            return {ash: chart.series.ash.length, advice: chart.series.advice.length};
        });
        await page.locator('#page-density-trend').screenshot({path: path.join(output, 'original.png')});
        await toggle.click();
        report.live = await page.evaluate(() => {
            const chart = DensityTrendPage, valid = chart.series.ash.filter(p => p.value !== null);
            return {aligned: chart.aligned, points: valid.length,
                paired: valid.every(p => chart.series.advice.some(a => a.at === p.at && !a.missing &&
                    a.ash?.value === p.value && a.archived === p.archived && a.ash?.revision === p.revision)),
                unchangedDensity: document.getElementById('density-trend-canvas').toDataURL() === _alignmentBefore.density,
                unchangedActions: document.getElementById('density-trend-actions').innerHTML === _alignmentBefore.actions,
                unchangedData: JSON.stringify(chart.meta) === _alignmentBefore.meta,
                range: chart.meta.period};
        });
        assert.ok(before.ash > 0 && report.live.points > 0, 'live data must exercise both modes');
        assert.ok(report.live.aligned && report.live.paired && report.live.unchangedDensity &&
            report.live.unchangedActions && report.live.unchangedData, JSON.stringify(report.live));
        await page.locator('#page-density-trend').screenshot({path: path.join(output, 'aligned.png')});
        await toggle.click();
        assert.ok(await page.evaluate(() => JSON.stringify(DensityTrendPage.series) === _alignmentBefore.series &&
            document.getElementById('density-trend-ash-canvas').toDataURL() === _alignmentBefore.ash &&
            document.getElementById('density-trend-canvas').toDataURL() === _alignmentBefore.density),
        'switching back must exactly restore original series and both canvas images');

        await toggle.click();
        await page.locator('[data-t-range="720"]').click();
        await page.waitForFunction(() => !DensityTrendPage.loading && DensityTrendPage.hours === 720);
        assert.equal(await toggle.getAttribute('aria-pressed'), 'true');
        assert.ok(await page.evaluate(() => DensityTrendPage.aligned && DensityTrendPage.series.ash.every(p => p.aligned)));
        await page.locator('#density-trend-custom').click();
        const custom = await page.evaluate(() => {
            const end = Date.parse(DensityTrendPage.meta.period.end) - 3600000;
            const local = t => new Date(t + 8*3600000).toISOString().slice(0,16);
            return {start: local(end - 25*3600000), end: local(end)};
        });
        await page.locator('#density-trend-start').fill(custom.start);
        await page.locator('#density-trend-end').fill(custom.end);
        await page.locator('#density-trend-query').click();
        await page.waitForFunction(() => !DensityTrendPage.loading && !!DensityTrendPage.range);
        assert.ok(await page.evaluate(() => DensityTrendPage.aligned && DensityTrendPage.series.ash.every(p =>
            p.t >= Date.parse(DensityTrendPage.meta.period.start) && p.t <= Date.parse(DensityTrendPage.meta.period.end))));
        await page.locator('#density-trend-refresh').click();
        await page.waitForFunction(() => !DensityTrendPage.loading);
        assert.equal(await toggle.getAttribute('aria-pressed'), 'true');
        await page.locator('#density-trend-theme').click();
        await page.locator('#page-density-trend').screenshot({path: path.join(output, 'aligned-dark.png')});
        await page.locator('#density-trend-theme').click();

        report.cases = await page.evaluate(() => {
            const chart = DensityTrendPage, base = Date.parse('2026-10-28T15:00:00Z'), hour = 3600000;
            const stamp = h => new Date(base+h*hour).toISOString();
            const ash = (h, value) => ({start: stamp(h-2), end: stamp(h-1), value,
                revision: 'version-'+h, provisional: true});
            const advice = (h, value, input, minute=2) => ({apply_start: stamp(h), at: stamp(h+minute/60), value,
                basis: {channels: ['A304'], value: 1.5}, ash: input});
            const data = {period: {start: stamp(-.5), end: stamp(7.5)}, actions: [],
                channels: {A304: Array.from({length: 8}, (_, h) => ({at: stamp(h), apply_start: stamp(h),
                    value: 1.5, origin: 'anchor'}))},
                suggestions: [advice(-1, 1.5, ash(-1, 8.1)),
                    advice(0, 1.5, ash(0, 8.7)), advice(0, 1.48, {...ash(0, 9.2), revision: 'latest'}, 23),
                    advice(1, 1.49, ash(1, 8.9)), advice(1, null, ash(1, 9.8), 24),
                    /* hour 2: only the red setpoint fallback, no archive */
                    advice(3, 1.5, null),
                    advice(4, 1.5, {...ash(4, 8.8), end: stamp(6)}), // impossible future source
                    advice(6, 1.49, {start: stamp(3), end: stamp(4), value: 9.6, revision: 'actual-source'}),
                    advice(7, 1.48, ash(7, 9.8))],
                ash: Array.from({length: 8}, (_, h) => ({at: stamp(h-2), value: 9.99, provisional: false}))};
            const original = JSON.stringify(data);
            chart.meta = data; chart.range = {start: stamp(-.5), end: stamp(7.5)};
            chart.setAligned(true);
            const valid = chart.series.ash.filter(p => p.value !== null);
            const result = {points: valid.map(p => ({hour: (p.t-base)/hour, value: p.value,
                source: p.source_start, revision: p.revision, text: chart.ashDetail(p)})),
                fallbackHours: chart.series.advice.filter(p => p.missing).map(p => (p.t-base)/hour),
                noBridge: !chart.connected(valid[0], valid[1]),
                sourceUnchanged: JSON.stringify(data) === original};
            const chips = [], realChip = chart.chip;
            chart.chip = function(ctx, x, y, lines) { chips.push(...lines); };
            try {
                chart.crosshair = {t: base+2*hour, x: 150}; chart.drawCrosshair();
                result.missingHourChips = chips.splice(0);
                chart.crosshair = {t: base+50*60000, x: 150}; chart.drawCrosshair();
                result.sameHourChips = chips.splice(0);
            } finally { chart.chip = realChip; chart.hideCrosshair(); }
            result.clipped = chart.alignedAsh(chart.hourlyAdvice(data.suggestions.map(p => ({...p, t: Date.parse(p.at)}))),
                {start: stamp(.25), end: stamp(6.25)}).filter(p => p.value !== null).map(p => (p.t-base)/hour);
            chart.setAligned(false);
            result.originalRestored = chart.series.ash.every(p => p.value === 9.99) && chart.series.ash[0].at === stamp(-2);
            chart.setAligned(true);
            document.getElementById('density-trend-caption').textContent = '合成验收数据 · 跨日 / 同小时修订 / 缺档（非生产数据）';
            return result;
        });
        assert.deepEqual(report.cases.points.map(p => [p.hour, p.value]), [[0, 9.2], [6, 9.6], [7, 9.8]]);
        assert.equal(report.cases.points[0].revision, 'latest');
        assert.match(report.cases.points[0].text, /原生产时段.*10\/28.*21:00.*22:00/);
        assert.match(report.cases.points[0].text, /对应建议小时.*23:00/);
        assert.match(report.cases.points[0].text, /建议存档于.*23:23/);
        assert.equal(report.cases.points[1].source, '2026-10-28T18:00:00.000Z', 'source metadata must not be fabricated by subtracting 2h');
        assert.deepEqual(report.cases.fallbackHours, [1, 2, 5]);
        assert.deepEqual(report.cases.clipped, [6]);
        assert.ok(report.cases.noBridge && report.cases.sourceUnchanged && report.cases.originalRestored);
        assert.ok(report.cases.missingHourChips.includes('建议依据灰分 —'), 'do not borrow a neighbour at a missing hour');
        assert.ok(report.cases.sameHourChips.includes('建议依据灰分 9.20%（暂估）'), 'crosshair stays in the same application hour');
        await page.locator('#density-trend-ash-canvas').focus();
        await page.keyboard.press('Home');
        assert.match(await page.locator('#density-trend-ash-plot .density-trend-tooltip').textContent(), /原生产时段/);
        await toggle.click();
        assert.ok(await page.locator('#density-trend-ash-plot .density-trend-tooltip').isHidden());
        assert.ok(await page.evaluate(() => DensityTrendPage.crosshair === null));
        await toggle.click();
        await page.locator('#page-density-trend').screenshot({path: path.join(output, 'synthetic-aligned.png')});
        await page.setViewportSize({width: 720, height: 1100});
        await page.locator('#page-density-trend').screenshot({path: path.join(output, 'aligned-narrow.png')});
        const layout = await toggle.boundingBox();
        assert.ok(layout && layout.x >= 0 && layout.x+layout.width <= 720, 'toggle must remain reachable on narrow screens');
        assert.equal(report.errors.length, 0, report.errors.join('\n'));
        assert.equal(report.mutations, 0);
        report.passed = true;
    } finally {
        fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2));
        await browser.close();
    }
    console.log(JSON.stringify(report));
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
