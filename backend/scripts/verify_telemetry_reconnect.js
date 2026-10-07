// 纯内存检查：授权请求、重复点击及新读数确认；不会联网或启动采集。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {App, ctx, elements} = require('./app_vm')();
ctx.window.location.protocol = 'http:';
ctx.AbortSignal = AbortSignal;
ctx.document.querySelector = () => ({content: 'test-token'});
ctx.setTimeout = callback => {queueMicrotask(callback); return 0;};
elements['telemetry-reconnect'] = {setAttribute() {}};
const toasts = [];
App.showToast = (text, kind) => toasts.push({text, kind});
App.saveStore = () => {throw Error('reconnect must not write business state');};
for (const file of ['api.js', 'operations.js']) {
    vm.runInContext(fs.readFileSync(path.resolve(__dirname, '../../frontend/js/' + file), 'utf8'), ctx);
}
const api = ctx.window.Api;
const ops = ctx.window.Operations;
const requested = new Date().toISOString();
const result = {ok:true, source_id:'kep', requested_at:requested, state:'starting'};
const source = (state, stamp=requested) => ({source_id:'kep', state, heartbeat_at:stamp, last_sample_at:stamp});
const latest = item => ({sources:[item], items:[{source_id:'kep',available:true}]});

(async () => {
    let request;
    ctx.fetch = async (url, init) => {request={url,init}; return {ok:true,json:async()=>result};};
    await api.reconnectTelemetry();
    assert.equal(request.url, '/api/v1/telemetry/reconnect');
    assert.equal(request.init.method, 'POST');
    assert.equal(request.init.headers['X-DMCS-Token'], 'test-token');
    ctx.fetch = async () => ({ok:false,status:401,json:async()=>({})});
    await assert.rejects(api.reconnectTelemetry(), /权限/);
    ctx.fetch = async () => ({ok:false,status:503,json:async()=>({detail:{message:'配置未启用'}})});
    await assert.rejects(api.reconnectTelemetry(), /配置未启用/);

    let resolveRequest, posts=0, checks=0;
    api.reconnectTelemetry = () => {posts++; return new Promise(resolve=>{resolveRequest=resolve;});};
    ops.refresh = async () => {checks++; ops.telemetry=latest(source('connected')); ops.telemetryFailed=false;};
    const pending = ops.reconnect();
    assert.equal(ops.reconnect(), pending, '双击共用连接任务');
    assert.equal(elements['telemetry-reconnect'].disabled, true);
    resolveRequest(result);
    assert.equal(await pending, true);
    assert.equal(posts, 1);
    assert.equal(elements['telemetry-reconnect'].disabled, false);
    assert.match(toasts.at(-1).text, /1 \/ 1/);

    api.reconnectTelemetry = async () => result;
    checks=0;
    const older = new Date(Date.parse(requested)-1000).toISOString();
    ops.refresh = async () => {ops.telemetry=latest(source('connected', ++checks===1 ? older : requested));};
    assert.equal(await ops.reconnect(), true);
    assert.equal(checks, 2, '旧connected快照不能冒充本次重新连接成功');

    ops.refresh = async () => {ops.telemetry=latest({...source('fault'),error_code:'BadUserAccessDenied'});};
    assert.equal(await ops.reconnect(), false);
    assert.equal(toasts.at(-1).kind, 'error');
    assert.equal(elements['telemetry-reconnect'].disabled, false);

    let ticks = 0;
    const OriginalDate = vm.runInContext('Date', ctx);
    ctx.Date = class extends OriginalDate {static now(){return ticks;}};
    posts=0; checks=0;
    api.reconnectTelemetry = async () => {posts++; return result;};
    ops.refresh = async () => {
        if (++checks===1) {ticks=11000;ops.telemetry=latest(source('offline',older));}
        else ops.telemetry=latest(source('connected'));
    };
    assert.equal(await ops.reconnect(), true);
    assert.equal(posts, 2, '失效来源可再次请求后台启动');

    ctx.window.location.protocol='file:';
    assert.equal(await ops.reconnect(), false);
    assert.equal(posts, 2);
    ops.renderReconnectControl();
    assert.equal(elements['telemetry-reconnect'].disabled, true);
    console.log('PASS: authenticated start, double-click guard, fresh sample confirmation, failure and offline recovery');
})().catch(error => {console.error(error);process.exitCode=1;});
