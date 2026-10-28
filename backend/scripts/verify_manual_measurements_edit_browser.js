/* End-to-end acceptance for manual measurement edit/delete and the one-month window.
 * Requires a local writable server (isolated database) at DMCS_VERIFY_URL. */
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const {chromium} = require(process.env.DMCS_PLAYWRIGHT_MODULE || 'playwright');

const url = process.env.DMCS_VERIFY_URL || 'http://127.0.0.1:8017';
const api = url + '/api/v1/manual-measurements';
const output = path.resolve(__dirname, '../../output/playwright/manual-measurements-edit-20261028');

async function call(method, path_, body) {
    const [route, query] = path_.split('?');
    const search = new URLSearchParams(query || '');
    const response = await fetch(api + route + (query ? '?' + search : ''), {method,
        headers: body ? {'Content-Type': 'application/json'} : {},
        body: body ? JSON.stringify(body) : undefined});
    return {status: response.status, data: await response.json().catch(() => null)};
}

(async () => {
    fs.mkdirSync(output, {recursive: true});
    const report = {passed: false, url, steps: [], errors: []};
    const browser = await chromium.launch({channel: 'chrome', headless: true});
    try {
        // Start from a clean table so reruns against the same isolated DB stay valid.
        for (let guard = 0; guard < 50; guard++) {
            const rows = (await call('GET', '?limit=200')).data.items || [];
            if (!rows.length) break;
            for (const row of rows) await call('DELETE', '/' + row.id);
        }
        assert.equal((await call('GET', '?limit=200')).data.total, 0);
        // Seed one record far outside the rolling one-month window.
        const oldTime = '2026-08-20T10:00+08:00';
        const seeded = await call('POST', '', {metric: 'raw_coal_slime_content', value: 15, observed_at: oldTime});
        assert.equal(seeded.status, 201);
        assert.equal((await call('GET', '?limit=200')).data.total, 1);
        assert.equal((await call('GET', '?limit=200&start=2026-10-01T00:00Z')).data.total, 0);

        const context = await browser.newContext({viewport: {width: 1500, height: 1000}});
        const page = await context.newPage();
        page.on('pageerror', error => report.errors.push('pageerror: ' + error.message));
        page.on('console', message => { if (message.type() === 'error') report.errors.push('console: ' + message.text()); });
        page.on('dialog', dialog => dialog.accept());
        await page.goto(url);
        await page.waitForSelector('#decision-manual-open', {state: 'visible'});
        await page.click('#decision-manual-open');
        await page.waitForSelector('#decision-manual-dialog[open]');
        try {
            await page.waitForFunction(() =>
                document.querySelector('#decision-measurement-count')?.textContent.includes('近1个月共 0 条'), null, {timeout: 60000});
        } catch (error) {
            await page.screenshot({path: path.join(output, 'window-wait-timeout.png'), fullPage: true});
            console.error('history:', await page.textContent('#decision-measurement-history').catch(() => 'missing'));
            console.error('count:', await page.textContent('#decision-measurement-count').catch(() => 'missing'));
            throw error;
        }
        assert(!(await page.textContent('#decision-measurement-history')).includes('原煤煤泥含量'),
            'the two-month-old record must stay outside the one-month window');
        report.steps.push('window hides the seeded two-month-old record');
        await page.screenshot({path: path.join(output, 'after-open.png')});

        await page.selectOption('#decision-measurement-metric', 'heavy_clean_coal_ash');
        await page.fill('#decision-measurement-value', '8.5');
        await page.click('#decision-measurement-submit');
        await page.waitForFunction(() => (document.querySelector('#decision-measurement-message') || {}).textContent?.includes('已保存到数据库'));
        await page.waitForFunction(() => document.querySelector('#decision-measurement-history')?.textContent.includes('8.5'));
        assert((await page.textContent('#decision-measurement-count')).includes('近1个月共 1 条'));
        report.steps.push('new record saved and visible in the one-month list');
        await page.screenshot({path: path.join(output, 'after-save.png')});

        await page.click('#decision-measurement-history button[data-action="edit"]');
        await page.waitForSelector('#decision-manual-edit-dialog[open]');
        assert((await page.textContent('#decision-manual-edit-summary')).includes('重介精煤灰分'));
        await page.selectOption('#decision-manual-edit-metric', 'float_clean_coal_ash');
        await page.fill('#decision-manual-edit-value', '10.5');
        await page.screenshot({path: path.join(output, 'edit-dialog.png')});
        await page.click('#decision-manual-edit-save');
        await page.waitForFunction(() => !document.querySelector('#decision-manual-edit-dialog')?.open);
        await page.waitForFunction(() => document.querySelector('#decision-measurement-history')?.textContent.includes('浮精灰分'));
        const edited = (await call('GET', '?limit=200')).data.items.find(item => item.id > 1);
        assert.equal(edited.metric, 'float_clean_coal_ash');
        assert.equal(edited.value, 10.5);
        assert.equal(edited.name, '浮精灰分');
        report.steps.push('edit changed metric and value in the database');
        await page.screenshot({path: path.join(output, 'after-edit.png')});

        await page.click('#decision-measurement-history button[data-action="delete"]');
        await page.waitForFunction(() => document.querySelector('#decision-measurement-count')?.textContent.includes('近1个月共 0 条'));
        assert.equal((await call('GET', '?limit=200')).data.total, 1);  // only the old seeded row remains
        report.steps.push('delete removed the edited row from the database');
        await page.screenshot({path: path.join(output, 'after-delete.png')});
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
