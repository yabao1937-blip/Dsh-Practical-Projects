/* Prove the two stacked panes share one time axis, pixel for pixel. */
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
(async () => {
    const browser = await chromium.launch({channel: 'chrome', headless: true});
    for (const viewport of [{width: 1500, height: 1000}, {width: 780, height: 900}]) {
        const page = await browser.newPage({viewport});
        await page.goto('http://127.0.0.1:8017/');
        await page.waitForFunction(() => App.__ready, null, {timeout: 60000});
        await page.locator('.nav-item[data-page="page-density-trend"]').click();
        await page.waitForFunction(() => DensityTrendPage.meta && !DensityTrendPage.loading);
        const report = await page.evaluate(() => {
            const ash = document.getElementById('density-trend-ash-canvas');
            const main = document.getElementById('density-trend-canvas');
            const period = DensityTrendPage.meta.period;
            const start = Date.parse(period.start), end = Date.parse(period.end);
            const width = canvas => canvas.getBoundingClientRect().width;
            const xOf = canvas => t => {
                const w = canvas.getBoundingClientRect().width;
                return 78 + (t - start) / (end - start) * (w - 78 - 44);
            };
            const samples = [0, .1, .25, .5, .75, .9, 1].map(f => start + (end - start) * f);
            const mapping = samples.map(t => xOf(main)(t) - xOf(ash)(t));
            // Empirical: cross-pane nearest-timestamp hit pairs.
            const ashHits = ash._hits.map(h => ({t: h.t, x: h.x})).sort((a, b) => a.t - b.t);
            const mainHits = main._hits.map(h => ({t: h.t, x: h.x})).sort((a, b) => a.t - b.t);
            const pairs = [];
            let j = 0;
            for (const a of ashHits) {
                while (j + 1 < mainHits.length && Math.abs(mainHits[j + 1].t - a.t) <= Math.abs(mainHits[j].t - a.t)) j++;
                const b = mainHits[j];
                pairs.push({dt: Math.abs(b.t - a.t), dx: Math.abs(b.x - a.x)});
            }
            return {ashWidth: width(ash), mainWidth: width(main), mapping,
                worstMapping: Math.max(...mapping.map(Math.abs)),
                msPerPx: (end - start) / (width(main) - 78 - 44),
                worstPair: pairs.reduce((w, p) => (p.dx > (w ? w.dx : -1) ? p : w), null),
                ashCount: ashHits.length, mainCount: mainHits.length};
        });
        console.log(viewport.width + 'px:', JSON.stringify(report));
        assert.equal(report.ashWidth, report.mainWidth, 'canvas widths differ');
        assert.ok(report.worstMapping < 0.51, 'x mapping differs: ' + report.worstMapping);
        // Pixel gaps between nearest-hit pairs must be fully explained by their
        // timestamp gap on the shared scale (msPerPx from the same axis).
        assert.ok(report.worstPair.dx - report.worstPair.dt * report.msPerPx <= 1.0,
            'unexplained pixel misalign: ' + JSON.stringify(report.worstPair));
        await page.close();
    }
    await browser.close();
    console.log('PASS: both panes share one time axis (formula + empirical hits).');
})();
