/* Crosshair acceptance: hover an ash point, both panes align and show values. */
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const {chromium} = require('playwright');

const url = process.env.DMCS_VERIFY_URL || 'http://127.0.0.1:8017';
const output = path.resolve(__dirname, '../../output/playwright/density-trend-crosshair-20261028');

(async () => {
    fs.mkdirSync(output, {recursive: true});
    const browser = await chromium.launch({channel: 'chrome', headless: true});
    const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
    const errors = [];
    page.on('pageerror', e => errors.push('pageerror: ' + e.message));
    await page.goto(url);
    await page.waitForFunction(() => App.__ready, null, {timeout: 60000});
    await page.locator('.nav-item[data-page="page-density-trend"]').click();
    await page.waitForFunction(() => DensityTrendPage.meta && !DensityTrendPage.loading);

    const target = await page.evaluate(() => {
        const ash = document.getElementById('density-trend-ash-canvas');
        const hits = ash._hits.filter(h => Number.isFinite(h.x));
        const pick = hits[Math.floor(hits.length * 0.7)];
        const rect = ash.getBoundingClientRect();
        return {clientX: rect.left + pick.x, clientY: rect.top + pick.y,
                t: pick.t, x: pick.x, count: hits.length};
    });
    await page.mouse.move(target.clientX, target.clientY, {steps: 4});
    await page.waitForTimeout(200);

    const state = await page.evaluate(() => {
        const page_ = DensityTrendPage;
        const overlay = document.getElementById('density-trend-crosshair');
        // Repaint the guide on a detached probe canvas to inspect drawn strokes.
        const chips = [];
        const ctx = overlay.getContext('2d');
        return {crosshair: page_.crosshair,
            overlaySize: [overlay.width, overlay.height],
            pixels: (() => {
                const data = ctx.getImageData(0, 0, overlay.width, overlay.height).data;
                let drawn = 0;
                for (let i = 3; i < data.length; i += 4) if (data[i] > 0) drawn++;
                return drawn;
            })()};
    });
    console.log(JSON.stringify({target: {t: target.t, x: target.x, count: target.count}, state, errors}));
    assert.ok(state.crosshair && Math.abs(state.crosshair.t - target.t) < 1,
        'crosshair must snap to the hovered ash point');
    assert.ok(state.pixels > 2000, 'overlay must actually draw the guide and chips');
    await page.screenshot({path: path.join(output, 'crosshair-ash-hover.png')});

    // Hover the lower pane: same behaviour from the density side.
    const lower = await page.evaluate(() => {
        const main = document.getElementById('density-trend-canvas');
        const hits = main._hits.filter(h => Number.isFinite(h.x));
        const pick = hits[Math.floor(hits.length * 0.5)];
        const rect = main.getBoundingClientRect();
        return {clientX: rect.left + pick.x, clientY: rect.top + pick.y, t: pick.t};
    });
    await page.mouse.move(lower.clientX, lower.clientY, {steps: 4});
    await page.waitForTimeout(200);
    const state2 = await page.evaluate(() => {
        const main = document.getElementById('density-trend-canvas');
        return {crosshair: DensityTrendPage.crosshair, hits: main._hits.length,
            rectLeft: main.getBoundingClientRect().left,
            label: main._adviceLabel && {left: main._adviceLabel.left, top: main._adviceLabel.top,
                width: main._adviceLabel.width, height: main._adviceLabel.height}};
    });
    console.log('lower diagnostics:', JSON.stringify({lower, state2}));
    assert.ok(state2.crosshair, 'density hover must set the crosshair');
    assert.ok(Math.abs(state2.crosshair.x - (lower.clientX - state2.rectLeft)) < 1.5,
        'crosshair must align to the hovered pixel column');
    await page.screenshot({path: path.join(output, 'crosshair-density-hover.png')});

    // Mouse leaves the chart: the guide disappears.
    await page.mouse.move(20, 400, {steps: 4});
    await page.waitForTimeout(150);
    assert.equal(await page.evaluate(() => DensityTrendPage.crosshair), null, 'guide must hide on leave');
    assert.deepEqual(errors, []);
    console.log('PASS: crosshair snaps in both panes, draws overlay, hides on leave.');
    await browser.close();
    process.exit(0);
})();
