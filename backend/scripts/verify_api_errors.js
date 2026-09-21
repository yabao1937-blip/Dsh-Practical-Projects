// 无网络回归：权限、守卫、数据库异常必须保留各自的分类与原因。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const context = vm.createContext({
    window: {}, document: { querySelector: () => null },
    localStorage: { getItem: () => null },
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../frontend/js/api.js'), 'utf8'), context);

(async () => {
    for (const status of [401, 403]) {
        context.fetch = async () => ({status, ok: false, json: async () => ({detail: {error: 'permission denied'}})});
        const result = await context.window.Api.putStateBody('{}');
        assert.equal(result.ok, false);
        assert.equal(result.denied, true);
        assert.equal(result.reason, 'permission denied');
    }
    context.fetch = async () => ({status: 200, ok: true, json: async () => ({ok: false, stale: true, error: 'stale'})});
    assert.equal((await context.window.Api.putStateBody('{}')).rejected, true);
    context.fetch = async () => ({status: 200, ok: true, json: async () => ({ok: false, error: 'database locked'})});
    assert.equal((await context.window.Api.putStateBody('{}')).rejected, false);
    context.fetch = async () => { throw new Error('offline'); };
    assert.equal((await context.window.Api.putStateBody('{}')).error, 'offline');
    console.log('API error regression: 5 scenarios passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
