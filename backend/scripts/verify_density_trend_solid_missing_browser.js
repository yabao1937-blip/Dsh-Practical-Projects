/* Solid advice line with red setpoint fallback for hours lacking advice. */
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const {chromium} = require(process.env.DMCS_PLAYWRIGHT_MODULE || 'playwright');

const url = process.env.DMCS_VERIFY_URL || 'http://127.0.0.1:8000';
const output = path.resolve(__dirname, '../../output/playwright/density-trend-solid-missing-20261028');

(async () => {
    fs.mkdirSync(output, {recursive: true});
    const browser = await chromium.launch({channel: 'chrome', headless: true});
    const report = {passed: false, url, live: null, synthetic: null, errors: []};
    try {
        const context = await browser.newContext({viewport: {width: 1500, height: 1000}});
        const page = await context.newPage();
        page.on('pageerror', error => report.errors.push('pageerror: ' + error.message));
        page.on('console', message => { if (message.type() === 'error') report.errors.push('console: ' + message.text()); });
        await page.goto(url);
        await page.waitForFunction(() => App.__ready, null, {timeout: 60000});
        await page.locator('.nav-item[data-page="page-density-trend"]').click();
        await page.waitForFunction(() => DensityTrendPage.meta && !DensityTrendPage.loading);
        report.live = await page.evaluate(() => {
            const advice = DensityTrendPage.series.advice;
            const canvas = document.getElementById('density-trend-canvas');
            const ashCanvas = document.getElementById('density-trend-ash-canvas');
            return {real: advice.filter(p => !p.missing && Number.isFinite(p.value)).length,
                fallback: advice.filter(p => p.missing).length,
                gaps: advice.filter(p => p.value === null).length,
                label: canvas._adviceLabel && canvas._adviceLabel.text,
                legend: document.querySelector('.density-trend-missing')?.textContent.trim(),
                ash: (DensityTrendPage.series.ash || []).length,
                ashHits: (ashCanvas._hits || []).length};
        });
        assert.ok(report.live.real >= 2, 'real archived advice must still be plotted');
        assert.ok(report.live.fallback >= 2, 'anchored hours without advice must fall back in red');
        assert.ok(report.live.ash >= 24, 'settled hourly ash must be returned for the window');
        assert.ok(report.live.ashHits >= 24, 'the ash companion chart must render hover points');
        assert.match(report.live.legend || '', /无建议小时/);
        await page.screenshot({path: path.join(output, 'live-7-days.png')});

        report.synthetic = await page.evaluate(() => {
            const chart = DensityTrendPage, saved = {series: chart.series, meta: chart.meta, visible: chart.visible};
            const t = Date.parse('2026-10-24T01:00:00Z'), hour = 3600000;
            const stamp = minutes => new Date(t + minutes*60000).toISOString();
            const anchor = (h, value) => ({at: stamp(h*60), t: t+h*hour, value, key: 'A304', origin: 'anchor',
                apply_start: stamp(h*60), break_before: false});
            const advice = (h, value, extra={}) => ({at: stamp(h*60+2), t: t+h*hour+120000, value, key: 'advice',
                apply_start: stamp(h*60), basis: {channels: ['A304'], value: 1.48}, ...extra});
            const data = {period: {start: stamp(-60), end: stamp(5*60)},
                channels: {A304: [anchor(0, 1.48), anchor(1, 1.48), anchor(2, 1.48), anchor(3, 1.49), anchor(4, 1.50)]},
                suggestions: [advice(0, 1.52), advice(1, null, {reason: '小时灰分覆盖不足'}),
                    /* hour 2 has no archived row at all */ advice(3, 1.53), advice(4, 1.53)],
                ash: [{at: stamp(0), value: 8.4, provisional: true}, {at: stamp(60), value: 8.6, provisional: false},
                    {at: stamp(180), value: 9.0, provisional: false}]};
            const series = chart.points(data);
            const result = {advice: series.advice.map(p => ({h: Math.round((p.t-t)/hour), value: p.value, missing: !!p.missing}))};
            chart.meta = data; chart.series = series; chart.visible = {A304: true, advice: true};
            const canvas = document.getElementById('density-trend-canvas'), ctx = canvas.getContext('2d');
            const ashCanvas = document.getElementById('density-trend-ash-canvas');
            const segments = [], dashes = [], radii = [];
            const stroke = ctx.stroke, dash = ctx.setLineDash, move = ctx.moveTo, line = ctx.lineTo, arc = ctx.arc;
            const palette = chart.palette();
            let current = null;
            ctx.setLineDash = function(pattern) {dashes.push(pattern); return dash.call(this, pattern);};
            ctx.moveTo = function(...xy) {current = [xy]; return move.apply(this, xy);};
            ctx.lineTo = function(...xy) {current = (current || []).concat([xy]); return line.apply(this, xy);};
            ctx.arc = function(x, y, radius, ...rest) {radii.push(radius); return arc.apply(this, [x, y, radius, ...rest]);};
            ctx.stroke = function() {
                if (current && current.length === 2 && (this.strokeStyle === palette.advice || this.strokeStyle === palette.missingAdvice))
                    segments.push({color: this.strokeStyle, lineWidth: this.lineWidth});
                current = null; return stroke.apply(this);
            };
            try { chart.draw(); } finally { ctx.stroke = stroke; ctx.setLineDash = dash; ctx.moveTo = move; ctx.lineTo = line; ctx.arc = arc; }
            result.segments = segments;
            result.dashed = dashes.some(d => d.length === 2 && d[0] === 6);
            result.dotRadii = radii;
            result.ashHits = (ashCanvas._hits || []).map(h => ({h: Math.round((h.t-t)/hour), text: h.text}));
            result.marks = (() => {  // dots appear only where the level changes
                const local = series.advice;
                return local.map((p, i) => chart.showPoint(i ? local[i-1] : null, p));
            })();
            result.hits = canvas._hits.filter(p => p.key === 'advice').map(p => ({h: Math.round((p.t-t)/hour), missing: !!p.missing, text: p.text}));
            result.label = canvas._adviceLabel && canvas._adviceLabel.text;
            result.shadowAreas = (() => {  // a flat fallback stretch equals its setpoint: no shadow there
                const flat = chart.points({period: data.period,
                    channels: {A304: [anchor(5, 1.55), anchor(6, 1.55), anchor(7, 1.55)]},
                    suggestions: [advice(5, null, {reason: '覆盖不足'}), advice(6, null, {reason: '覆盖不足'})]});
                const savedSeries = chart.series;
                chart.series = flat;
                const areas = chart.differenceAreas().filter(a => a.start >= t+5*hour);
                chart.series = savedSeries;
                return areas.length;
            })();
            result.nullWithoutAnchor = (() => {  // a null row whose hour has no setpoint stays a break
                const local = chart.points({period: data.period,
                    channels: {A304: [anchor(0, 1.48), anchor(2, 1.48)]},
                    suggestions: [advice(0, 1.5), advice(1, null), advice(2, 1.51)]}).advice;
                return local.map(p => p.value);
            })();
            window._restoreDensityTrend = () => { Object.assign(chart, saved); chart.draw(); };
            return result;
        });
        const s = report.synthetic;
        // Hour 1 (blocked row) and hour 2 (no row) both fall back to that hour's setpoint.
        assert.deepEqual(s.advice.map(p => [p.h, p.value, p.missing]),
            [[0, 1.52, false], [1, 1.48, true], [2, 1.48, true], [3, 1.53, false], [4, 1.53, false]]);
        const colors = s.segments.map(x => x.color), palette = await page.evaluate(() => DensityTrendPage.palette());
        assert.deepEqual(colors, [palette.missingAdvice, palette.missingAdvice, palette.missingAdvice, palette.advice]);
        assert.ok(s.segments.every(x => x.lineWidth === 3));
        assert.equal(s.dashed, false, 'advice must be solid, not dashed');
        assert.deepEqual(s.marks, [false, true, false, true, false],
            'equal consecutive fallback hours draw a plain red line with no dot');
        assert.ok(s.dotRadii.length > 0 && s.dotRadii.every(r => r <= 2.5), 'dots must stay small');
        assert.ok(s.hits.some(p => p.missing && p.h === 1 && /缺失必要计算数据/.test(p.text) && /覆盖不足/.test(p.text)));
        assert.ok(s.hits.some(p => p.missing && p.h === 2 && /缺失必要计算数据/.test(p.text)));
        assert.match(s.label, /1\.530/);  // the label follows the latest real advice, never a fallback
        // The aligned ash chart renders its own hover points, provisional marked as estimates.
        assert.deepEqual(s.ashHits.map(h => h.h), [0, 1, 3]);
        assert.match(s.ashHits[0].text, /小时结算总灰分\s+8\.40\s*%/);
        assert.match(s.ashHits[0].text, /暂估/);
        assert.ok(!/暂估/.test(s.ashHits[1].text));
        assert.ok(s.hits.some(p => p.missing && p.h === 1 && /缺失必要计算数据/.test(p.text) && /覆盖不足/.test(p.text)));
        assert.ok(s.hits.some(p => p.missing && p.h === 2 && /缺失必要计算数据/.test(p.text)));
        assert.match(s.label, /1\.530/);  // the label follows the latest real advice, never a fallback
        // Fallback hours equal the setpoint: no difference shadow appears there.
        assert.equal(s.shadowAreas, 0);
        // A null advice row in an hour without any setpoint anchor stays an honest break.
        assert.deepEqual(s.nullWithoutAnchor, [1.5, null, 1.51]);
        await page.evaluate(() => window._restoreDensityTrend());
        await page.locator('#density-trend-theme').click();
        await page.waitForTimeout(250);
        await page.screenshot({path: path.join(output, 'live-dark.png')});
        await context.close();
        assert.deepEqual(report.errors, []);
        report.passed = true;
    } finally {
        await browser.close();
        fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2));
        console.log(JSON.stringify(report));
    }
    process.exit(report.passed ? 0 : 1);
})();
