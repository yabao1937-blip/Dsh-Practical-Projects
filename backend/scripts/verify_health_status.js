// Real API/Operations composition in a memory-only browser; no network or store writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {App, ctx, elements} = require('./app_vm')();
ctx.window.location.protocol = 'http:';
ctx.document.visibilityState = 'hidden';
ctx.AbortSignal = {timeout: milliseconds => ({milliseconds})};
vm.runInContext(fs.readFileSync(path.resolve(__dirname, '../../frontend/js/api.js'), 'utf8'), ctx);
vm.runInContext(fs.readFileSync(path.resolve(__dirname, '../../frontend/js/operations.js'), 'utf8'), ctx);
const api = ctx.window.Api;
const ops = ctx.window.Operations;
api.getManualMeasurementPoints = async () => ({items:[]});
for (const id of ['server-status-label', 'server-status-dot', 'ops-server', 'ops-collector',
    'ops-message', 'ops-points', 'status-collect']) elements[id] = {};
const response = (status, body) => ({ok:status >= 200 && status < 300, status, json:async () => body});
const available = {sources:[{name:'KEP',state:'connected'}],items:[]};

async function refresh(ready, live, telemetry = available) {
    const requested = [];
    ctx.fetch = async (url, options) => {
        requested.push({url, options});
        if (url.endsWith('/telemetry/latest')) {
            if (telemetry instanceof Error) throw telemetry;
            return response(200, telemetry);
        }
        const result = url.endsWith('/health/live') ? live : ready;
        if (result instanceof Error) throw result;
        assert.ok(result, 'unexpected extra liveness request');
        return result;
    };
    await ops.refresh();
    return requested.filter(item => item.url.includes('/health'));
}

(async () => {
    const timeout = Object.assign(new Error('readiness timed out'), {name:'TimeoutError'});
    let requests = await refresh(timeout, response(200, {status:'ok',uptime_seconds:30}));
    assert.equal(ops.serverState, 'busy');
    assert.equal(elements['server-status-label'].textContent, '后端繁忙');
    assert.equal(ops.health.checks.database.status, 'unknown', 'liveness cannot prove database readiness');
    assert.equal(ops.telemetryFailed, false, 'busy backend must not mark healthy KEP collection offline');
    assert.equal(elements['status-collect'].textContent, '采集中');
    assert.doesNotMatch(elements['server-status-dot'].className, /offline/);
    assert.equal(requests.length, 2);
    assert.equal(requests[0].options.signal.milliseconds, 5000);
    assert.equal(requests[1].options.signal.milliseconds, 2000);

    await refresh(timeout, response(200, {status:'ok'}), new Error('telemetry query timed out'));
    assert.equal(ops.serverState, 'busy');
    assert.equal(ops.telemetryFailed, true, 'telemetry failure must remain visible independently');
    assert.equal(elements['ops-collector'].textContent, '状态不可达');

    requests = await refresh(new Error('no connection'), new Error('no connection'), new Error('no connection'));
    assert.equal(ops.serverState, 'offline');
    assert.equal(elements['server-status-label'].textContent, '后端离线');
    assert.match(elements['server-status-dot'].className, /offline/);
    assert.equal(requests.length, 2);

    requests = await refresh(response(503, {status:'unavailable',checks:{database:{error_code:'database_unavailable'}}}));
    assert.equal(ops.serverState, 'unavailable');
    assert.equal(elements['server-status-label'].textContent, '数据库未就绪');
    assert.equal(ops.health.checks.database.error_code, 'database_unavailable');
    assert.equal(requests.length, 1, 'a completed 503 must not be overwritten by a successful live probe');

    requests = await refresh(response(200, {status:'ok',checks:{database:{status:'ok'}}}));
    assert.equal(ops.serverState, 'online');
    assert.equal(elements['server-status-label'].textContent, '后端在线');
    assert.match(elements['server-status-dot'].className, /online/);
    assert.equal(requests.length, 1);
    assert.equal(ops._timer, null, 'hidden browser must not start polling timers');
    console.log('PASS: slow readiness + live = busy, unreachable = offline, 503 = unavailable, healthy = online; KEP status remains independent');
})().catch(error => {console.error(error); process.exitCode = 1;});
