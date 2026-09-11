// 端到端验证:整库镜像的**写路径**（浏览器 → PUT /api/v1/state）
//   用例0  冷启动空配置：新浏览器（空 localStorage / heavySamples=0）的整库写**必须不被
//          stale 守卫拒绝** —— 这是 2026-09 缺陷1 的复现路径：守卫曾把 heavy_samples
//          纳入比较，而前端把 heavySamples 视为纯本地键，于是"新浏览器永远写不进去"，
//          且失败被 .catch(()=>{}) 吞掉，界面完全正常。
//   用例1  写失败不静默：注入发送失败 → 必须留下待发副本 + 提示 + 状态标记；
//          恢复后由 online 事件触发重试 → 待发清除、**服务器真实收到**新值。
// 说明：用例1 只 stub Api.putStateBody 来*注入失败*，重试后走真实网络往返，
//       并直接读服务器 /api/v1/state 校验，避免"用 stub 验证被 stub 掉的层"。
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const BASE = process.env.DMCS_URL || 'http://127.0.0.1:8000';
const URL = BASE.replace(/\/$/, '') + '/';
const CANDIDATES = [
    process.env.DMCS_EDGE,
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    '/usr/bin/microsoft-edge-stable', '/usr/bin/microsoft-edge',
    '/usr/bin/google-chrome', '/usr/bin/chromium',
];
const EDGE = CANDIDATES.find(p => p && fs.existsSync(p));
const PORT = 9387;
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const fails = [];
function check(name, ok, detail) {
    console.log(`${name}: ${ok ? 'PASS' : 'FAIL'}${detail ? ' | ' + detail : ''}`);
    if (!ok) fails.push(name);
}

(async () => {
    if (!EDGE) {
        console.error('ERROR: 未找到浏览器可执行文件，请用环境变量 DMCS_EDGE 指定');
        process.exit(2);
    }
    console.log('浏览器:', EDGE, '| 目标:', URL);
    // 冷启动 = 全新用户目录（空 localStorage），用例0 的前提
    const UD = path.join(process.env.TEMP || '/tmp', 'dmcs-cdp-mirror-' + Date.now());
    const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run',
        `--user-data-dir=${UD}`, `--remote-debugging-port=${PORT}`, '--window-size=1680,1200', URL],
        { stdio: 'ignore' });
    try {
        let page;
        for (let i = 0; i < 40; i++) {
            try {
                const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
                page = l.find(t => t.type === 'page' && t.url.includes('127.0.0.1'));
                if (page) break;
            } catch (e) { /* 调试端口尚未起来 */ }
            await sleep(500);
        }
        if (!page) throw new Error('未能连上浏览器调试端口');
        const ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = fail; });
        await sleep(3500);   // 等 init + autoPullIfStale
        let idc = 0; const pending = new Map();
        ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
        const send = (method, params) => new Promise((ok) => { const id = ++idc; pending.set(id, ok); ws.send(JSON.stringify({ id, method, params })); });
        const evalJs = async (expr, awaitPromise = false) => {
            const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise });
            if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text);
            return r.result.result.value;
        };

        // 记录初始状态，用例1 结束后复原（脚本也可能对着真实开发库跑）。
        // 空库（CI 就是这种：全新 SQLite，一条记录都没有）时 GET /state 只有 14 个键、
        // ashTarget 为 null —— 此时以**浏览器侧**的值作基线：冷启动把种子默认值推上去
        // 正是这条链路本身要做的事，复原目标就取这个基线。
        const before = await (await fetch(BASE + '/api/v1/state')).json();
        const serverAsh = before.ashTarget;
        const localAsh = await evalJs('App.store.ashTarget');
        const baseline = (serverAsh === null || serverAsh === undefined) ? localAsh : serverAsh;
        // 探针值必须与"服务器当前值"和"浏览器当前值"都不同：否则新快照与上次已发送的
        // 内容逐字节相同，会被 _flushMirror 的"内容相同则跳过"短路 —— 用例会**空转**，
        // 并在遗留值上假 PASS（首版就是这样骗过我的）。空转的兜底断言是下面的 calls===1。
        const probe = Math.round((Number(baseline) + 0.03) * 100) / 100;
        console.log('冷启动本地: heavySamples =', await evalJs('(App.store.heavySamples||[]).length'),
            '| 服务器: heavySamples =', (before.heavySamples || []).length,
            '| ashTarget: 服务器', serverAsh, '/ 本地', localAsh, '| 基线', baseline, '| 探针值', probe);
        check('E2E_PROBE_IS_NEW', String(probe) !== String(serverAsh) && String(probe) !== String(localAsh),
            `probe=${probe} baseline=${baseline}`);

        // ---------- 用例0：冷启动一次整库写必须成功（非 stale） ----------
        const cold = JSON.parse(await evalJs(
            `(async () => JSON.stringify(await Api.putStateBody(JSON.stringify(App.store), {})))()`, true));
        check('MIRROR_COLD_START', cold.ok === true, JSON.stringify(cold).slice(0, 300));

        // ---------- 用例1a：发送失败必须留下痕迹（不静默） ----------
        const inj = JSON.parse(await evalJs(`(async () => {
            App.mirrorStatus.failures = 0;
            App._savePendingMirror(null);
            window.__realPut = Api.putStateBody.bind(Api);
            window.__calls = 0;
            Api.putStateBody = () => { window.__calls++; return Promise.resolve({ ok: false, error: 'E2E 注入的发送失败' }); };
            App.store.ashTarget = ${JSON.stringify(probe)};
            App.saveStore();            // 排入镜像（防抖 2s）
            App._flushMirror(false);    // 立即冲，不等防抖
            await new Promise(r => setTimeout(r, 800));
            const toast = document.querySelector('#toast-container .toast.warning');
            return JSON.stringify({
                calls: window.__calls,      // 必须 >0：证明这个用例真的走到了发送环节
                pendingSaved: localStorage.getItem('dmcs_mirror_pending') ? 'yes' : 'no',
                statusPending: App.mirrorStatus.pending,
                failures: App.mirrorStatus.failures,
                lastError: App.mirrorStatus.lastError,
                toast: toast ? toast.textContent.slice(0, 60) : null,
            });
        })()`, true));
        console.log('  (debug)', JSON.stringify(inj).slice(0, 300));
        check('MIRROR_FAIL_NOT_SILENT',
            inj.calls === 1 && inj.pendingSaved === 'yes' && inj.statusPending === true
            && inj.failures === 1 && !!inj.toast, JSON.stringify(inj).slice(0, 300));

        // ---------- 用例1b：恢复后 online 事件触发重试，且服务器真实收到 ----------
        const retry = JSON.parse(await evalJs(`(async () => {
            Api.putStateBody = window.__realPut;      // 恢复正常发送
            window.dispatchEvent(new Event('online')); // 重试触发点之一
            await new Promise(r => setTimeout(r, 3000));
            return JSON.stringify({
                pendingSaved: localStorage.getItem('dmcs_mirror_pending') ? 'yes' : 'no',
                statusPending: App.mirrorStatus.pending,
                failures: App.mirrorStatus.failures,
                lastOkAt: !!App.mirrorStatus.lastOkAt,
            });
        })()`, true));
        check('MIRROR_RETRY_ON_ONLINE',
            retry.pendingSaved === 'no' && retry.statusPending === false && retry.failures === 0 && retry.lastOkAt,
            JSON.stringify(retry).slice(0, 300));

        let sv = null;
        for (let i = 0; i < 20; i++) {
            sv = await (await fetch(BASE + '/api/v1/state')).json();
            if (String(sv.ashTarget) === String(probe)) break;
            await sleep(500);
        }
        check('MIRROR_RETRY_ARRIVED', String(sv.ashTarget) === String(probe), `服务器 ashTarget = ${sv.ashTarget}`);

        // ---------- 复原：把 ashTarget 写回基线并确认服务器已回到基线 ----------
        // 注意：_flushMirror 只发"待发内容"，没有待发就是空操作 —— 必须 saveStore 排入。
        await evalJs(`App.store.ashTarget = ${JSON.stringify(baseline)};
            App.saveStore(); App._flushMirror(false); true`);
        let restored = null;
        for (let i = 0; i < 20; i++) {
            restored = await (await fetch(BASE + '/api/v1/state')).json();
            if (String(restored.ashTarget) === String(baseline)) break;
            await sleep(500);
        }
        check('MIRROR_RESTORED', String(restored.ashTarget) === String(baseline),
            `ashTarget 复原为 ${restored.ashTarget}（基线 ${baseline}）`);
        ws.close();
    } catch (e) {
        console.error('ERROR:', e.message);
        fails.push('EXCEPTION');
    } finally {
        try { edge.kill(); } catch (e) {}
        console.log(fails.length ? `MIRROR_E2E: FAIL (${fails.join(', ')})` : 'MIRROR_E2E: PASS');
        process.exitCode = fails.length ? 1 : 0;
        setTimeout(() => process.exit(), 300);
    }
})();
