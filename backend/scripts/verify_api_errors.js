// 无网络回归：权限、守卫、数据库异常必须保留各自的分类与原因。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const timeouts=[];
const context = vm.createContext({
    window: {}, document: { querySelector: () => null },
    localStorage: { getItem: () => null },
    URLSearchParams,
    AbortSignal: { timeout: value => {timeouts.push(value);return undefined;} },
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
    context.fetch = async () => ({status: 409, ok: false, json: async () => ({ok: false, conflict: true, error: 'version conflict'})});
    assert.equal((await context.window.Api.putStateBody('{}')).conflict, true);
    context.fetch = async () => ({status: 200, ok: true, json: async () => ({ok: true, revision: 'next'})});
    assert.equal((await context.window.Api.putStateBody('{}')).revision, 'next');
    const pending={status:'pending',checked_at:'2026-10-14T02:00:01Z',reason:'上一小时统计正在生成',
        period:{start:'2026-10-14T01:00:00Z',end:'2026-10-14T02:00:00Z'}};
    context.fetch=async()=>({ok:true,json:async()=>pending});
    assert.equal((await context.window.Api.getDecisionHourly()).status,'pending');
    for(const malformed of [null,{status:'pending'},{...pending,period:{start:'invalid',end:'invalid'}}]) {
        context.fetch=async()=>({ok:true,json:async()=>malformed});
        await assert.rejects(()=>context.window.Api.getDecisionHourly(),/小时统计响应异常/);
    }
    context.fetch=async()=>({ok:true,json:async()=>({policy:'decision-ash-hours-v1',items:[],summary:{},period:{}})});
    await context.window.Api.getTotalAshHours();
    assert.equal(timeouts.at(-1),10000,'production retains its ten-second timeout');
    context.document.querySelector=()=>({content:'true'});
    await context.window.Api.getTotalAshHours();
    assert.equal(timeouts.at(-1),30000,'read-only cold replay shares the bounded decision timeout');
    await context.window.Api.getTotalAshHours({start:'2026-10-14T01:00:00Z'});
    assert.equal(timeouts.at(-1),10000,'explicit historical queries keep their existing timeout');
    const customSignal={},priorTimeouts=timeouts.length;
    context.fetch=async(_url,options)=>{
        assert.strictEqual(options.signal,customSignal);
        return {ok:true,json:async()=>({policy:'decision-ash-hours-v1',items:[],summary:{},period:{}})};
    };
    await context.window.Api.getTotalAshHours({},customSignal);
    assert.equal(timeouts.length,priorTimeouts,'caller cancellation remains authoritative');
    console.log('API regression: 7 state/error scenarios and 4 hourly pending/validation scenarios passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
