// 端到端验证:整库镜像的**写路径**（浏览器 → PUT /api/v1/state）
//
//   前置  播种：向目标库写入少量记录 + 1 条重介采样，并断言 GUARD_ARMED。
//         为什么必须播种：后端守卫是 `if sum(current.values()) > 0 and regressed`
//         （migrate.replace）—— **空库上守卫是关闭的**，直接跑用例0 会"因为守卫没生效"
//         而永远 PASS，即空转假绿。CI 的库必然是空的（*.db 被 gitignore），
//         所以这一步不是可选项。
//   用例0 冷启动空配置（空 localStorage / heavySamples=0）的整库写**必须不被守卫拒绝**。
//         这正是缺陷 M-1 的复现路径：守卫曾把 heavy_samples 纳入比较，而前端把
//         heavySamples 视为纯本地键（恒为 0）→ 每次都判"回退"→ 浏览器→服务器同步全死，
//         且失败被 .catch(()=>{}) 吞掉。播种 heavy_samples>=1 后本用例才真正有牙齿。
//   用例1 传输失败不得静默：注入发送失败 → 断言**发送前**已落下待发副本 + 提示 + 状态；
//         恢复后由 online 事件触发重试 → 断言服务器**真的**收到（读 /api/v1/state）。
//   用例2 守卫明确拒绝(rejected) 与传输失败必须分开处置：rejected 不留待发副本、
//         不重发内容相同的快照（否则每次 online 都发一次注定被拒的整库）。
//
// 说明：只 stub `Api.putStateBody` 来**注入失败**；成功路径走真实网络并回读服务器状态
//       —— 这个项目的教训是"用 stub 替代成功路径"曾给出过完全错误的绿灯。
//
// 安全：整库 PUT 会 wipe 并重写 manual_entries / alerts / import_logs / coarse_models /
//       settings / auto_state（守卫只保护四类测量记录，不保护这些表）。因此
//       **默认拒绝对着非空库运行**，除非显式设置 DMCS_ALLOW_REAL_DB=1。
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const BASE = (process.env.DMCS_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');
const URL = BASE + '/';
// 注意：本文件的 `const URL` 是字符串，会**遮蔽**全局 URL 类，
// 所以不能写 new URL(URL)（会报 URL is not a constructor）—— 直接剥协议前缀。
const HOST = BASE.replace(/^https?:\/\//, '');   // 形如 192.168.43.104:8013
const CANDIDATES = [
    process.env.DMCS_EDGE,
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    '/usr/bin/microsoft-edge-stable', '/usr/bin/microsoft-edge',
    '/usr/bin/google-chrome', '/usr/bin/chromium',
];
const EDGE = CANDIDATES.find(p => p && fs.existsSync(p));
const PORT = 9300 + Math.floor(Math.random() * 400);   // 随机端口：避免连到遗留的调试浏览器
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const fails = [];
function check(name, ok, detail) {
    console.log(`${name}: ${ok ? 'PASS' : 'FAIL'}${detail ? ' | ' + detail : ''}`);
    if (!ok) fails.push(name);
}
// 本脚本是**在服务器本机运行的测试工具**，因此允许读服务器上的 token 文件来访问写接口。
// （浏览器侧走的是另一条路：首页把 token 注入 <meta>，由 api.js 自动带上 —— 那才是被测对象，
//  所以下面的浏览器断言仍然真实验证了"页面下发 token → 浏览器写入"这条链。）
function writeToken() {
    if (process.env.DMCS_WRITE_TOKEN) return process.env.DMCS_WRITE_TOKEN.trim();
    try {
        const p = path.join(__dirname, '..', 'data', 'write_token.txt');
        return fs.existsSync(p) ? fs.readFileSync(p, 'utf8').trim() : null;
    } catch (e) { return null; }
}

const api = async (p, init) => {
    const opts = Object.assign({}, init);
    const tk = writeToken();
    if (tk) opts.headers = Object.assign({}, opts.headers, { 'X-DMCS-Token': tk });
    const r = await fetch(BASE + p, opts);
    if (!r.ok) {
        // 把服务器**说的内容**带出来：只报 HTTP 状态在 CI 上等于没有信息
        // （422/500 的正文才说明是哪个字段/哪一步不行）
        let body = '';
        try { body = (await r.text()).slice(0, 400); } catch (e) { /* 忽略 */ }
        throw new Error(`${p} → HTTP ${r.status} ${body}`);
    }
    return r.json();
};
const post = (p, body) => api(p, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body) });

async function seed() {
    // 四类测量记录里播种 coarse + 采样：让守卫的 sum(current)>0 成立、且 heavy_samples>=1
    await post('/api/v1/records', { category: 'coarse', ts: '2000-01-01 00:00:00',
        system: 'seed', ash_content: 10.0, coal_amount: 40 });
    await post('/api/v1/records', { category: 'float', ts: '2000-01-01 00:00:00',
        system: 'seed', ash_content: 9.0 });
    await post('/api/v1/records', { category: 'ash_density', ts: '2000-01-01 00:00:00',
        system: 'seed', density: 1.50, ash_content: 9.5 });
    // 返回自增 id：重介采样**没有 GET 接口**（它在前端是纯本地键），
    // 于是用"自增 id 是否递增"来判断中间那次整库镜像有没有把采样表清掉
    // —— 表被清空后新插入会重新拿到 id=1，探针因此能察觉。
    return post('/api/v1/samples/heavy-ash', { ts: '2000-01-01 00:00:00', rho: 1.50,
        ash_content: 8.0, source: 'E2E播种' });
}

(async () => {
    if (!EDGE) {
        console.error('ERROR: 未找到浏览器可执行文件，请用环境变量 DMCS_EDGE 指定');
        process.exit(2);
    }
    console.log('浏览器:', EDGE, '| 目标:', URL, '| Node', process.version);

    // ---------- 前置阶段（库状态检查 + 播种）----------
    // 刻意包在 try 里：这一阶段若抛异常而无人捕获，Node 只打印堆栈并以 1 退出，
    // CI 上就表现为"1 秒就红、日志里看不到任何有用原因"（2026-09-11 实际踩到过）。
    let heavyId = null, armed = null, serverAsh = null, localAsh = null;
    try {
        // 目标库必须为空（除非显式放行）：整库 PUT 会 wipe/重写下面这些表
        const sv0 = await api('/api/v1/state');
        const vec0 = { coarse: (sv0.coarseCoal || []).length, float: (sv0.floatCoal || []).length,
            ash_density: (sv0.calcLogs || []).length };
        const nonEmpty = Object.values(vec0).some(n => n > 0);
        if (nonEmpty && process.env.DMCS_ALLOW_REAL_DB !== '1') {
            console.error('ERROR: 目标库非空（' + JSON.stringify(vec0) + '），本脚本会整库 PUT，');
            console.error('       而整库 PUT 会 wipe/重写 manual_entries/alerts/import_logs/coarse_models/');
            console.error('       settings/auto_state（守卫不保护这些表）。请指向空库（CI 即如此），');
            console.error('       或确知后果后设置 DMCS_ALLOW_REAL_DB=1。');
            process.exit(2);
        }
        if (nonEmpty) console.warn('警告: 正在对着非空库运行（已显式放行）:', JSON.stringify(vec0));

        // 播种 + 守卫已武装
        const seededSample = await seed();
        const seeded = await api('/api/v1/state');
        armed = { coarse: (seeded.coarseCoal || []).length, float: (seeded.floatCoal || []).length,
            ash_density: (seeded.calcLogs || []).length, total: 0 };
        armed.total = armed.coarse + armed.float + armed.ash_density;
        heavyId = seededSample && seededSample.id;
    } catch (e) {
        console.error('ERROR(前置阶段/播种):', e.message);
        console.log('MIRROR_E2E: FAIL (前置阶段异常)');
        process.exitCode = 1;
        process.exit();
    }
    check('GUARD_ARMED', armed.total > 0 && heavyId >= 1,
        `测量记录 ${armed.total} / 重介采样 id=${heavyId}（守卫在空库上是关闭的，必须播种）`);

    const UD = path.join(process.env.TEMP || '/tmp', 'dmcs-cdp-mirror-' + Date.now());
    const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run',
        `--user-data-dir=${UD}`, `--remote-debugging-port=${PORT}`, '--window-size=1680,1200', URL],
        { stdio: 'ignore' });
    try {
        let page;
        for (let i = 0; i < 40; i++) {
            try {
                const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
                page = l.find(t => t.type === 'page' && t.url.includes(HOST));   // 按目标主机匹配，别写死 127.0.0.1
                if (page) break;
            } catch (e) { /* 调试端口尚未起来 */ }
            await sleep(500);
        }
        if (!page) throw new Error('未能连上浏览器调试端口');
        const ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = fail; });
        let idc = 0; const pending = new Map();
        ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
        const send = (method, params) => new Promise((ok) => { const id = ++idc; pending.set(id, ok); ws.send(JSON.stringify({ id, method, params })); });
        const evalJs = async (expr, awaitPromise = false) => {
            const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise });
            if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text);
            return r.result.result.value;
        };

        // 等页面初始化完成：**轮询**而不是固定 sleep。
        // CI 冷启动比开发机慢得多（seed_data.js 153KB + app.js 120KB 都是冷读），
        // 固定 3.5 秒曾让第一条 evalJs 撞上 "ReferenceError: App is not defined"。
        // 注意用 typeof App 而不是 window.App：app.js 是 `const App = {...}`，
        // 脚本作用域的 const **不会**挂到 window 上（第一版就是这么写错的）。
        let appReady = false, lastErr = null;
        for (let i = 0; i < 60; i++) {
            try {
                if (await evalJs("typeof App !== 'undefined' && !!App.store && !!App.store.coarseCoal")) {
                    appReady = true; break;
                }
            } catch (e) { lastErr = e.message; }   // 记下来，别让失败没有信息量
            await sleep(500);
        }
        if (!appReady) {
            throw new Error('页面 30 秒内未完成初始化（typeof App 仍不可用）'
                + (lastErr ? '；最后一次求值异常: ' + lastErr : ''));
        }
        await sleep(800);   // 再给 autoPullIfStale 一点时间（它不影响下面的断言，只影响基线值）

        // 初始状态。空库时 GET /state 的 ashTarget 是 null → 以**浏览器侧**值作基线
        // （冷启动把种子默认值推上去正是这条链路要做的事）。
        const before = await api('/api/v1/state');
        const serverAsh = before.ashTarget;
        const localAsh = await evalJs('App.store.ashTarget');
        const baseline = (serverAsh === null || serverAsh === undefined) ? localAsh : serverAsh;
        // 探针值必须同时不同于服务器值与浏览器值：否则新快照与"上次已发送内容"逐字节相同，
        // 会被"内容相同则跳过"短路 → 用例空转，并在遗留值上假 PASS（首版就是这样骗过我的）。
        // 兜底断言是下面的 calls===1。
        const probe = Math.round((Number(baseline) + 0.03) * 100) / 100;
        const localHeavy = await evalJs('(App.store.heavySamples||[]).length');
        console.log('冷启动: 浏览器重介采样', localHeavy, '/ 服务器播种 id', heavyId,
            '| ashTarget 服务器', serverAsh, '本地', localAsh, '| 基线', baseline, '| 探针', probe);
        check('E2E_PROBE_IS_NEW', String(probe) !== String(serverAsh) && String(probe) !== String(localAsh),
            `probe=${probe} baseline=${baseline}`);
        // 缺陷 M-1 的复现前提：服务器有采样、浏览器没有（旧守卫据此判回退并拒绝整库写）
        check('E2E_M1_PRECONDITION', localHeavy === 0 && heavyId >= 1,
            `浏览器 ${localHeavy} / 服务器播种 id ${heavyId}`);

        // ---------- 用例0：冷启动一次整库写必须成功（非 stale） ----------
        const cold = JSON.parse(await evalJs(
            `(async () => JSON.stringify(await Api.putStateBody(JSON.stringify(App.store), {})))()`, true));
        check('MIRROR_COLD_START', cold.ok === true, JSON.stringify(cold).slice(0, 300));

        // ---------- 用例1a：传输失败必须留下痕迹（不静默） ----------
        const inj = JSON.parse(await evalJs(`(async () => {
            App.mirrorStatus.failures = 0; App.mirrorStatus.rejected = 0;
            App._clearPendingSlot(null);
            window.__realPut = Api.putStateBody.bind(Api);
            window.__calls = 0;
            Api.putStateBody = () => { window.__calls++; return Promise.resolve({ ok: false, error: 'E2E 注入的传输失败' }); };
            App.store.ashTarget = ${JSON.stringify(probe)};
            App.saveStore();            // 排入镜像（防抖 2s）
            App._flushMirror(false);    // 立即冲，不等防抖
            const slotRightAfterSend = !!App._readPendingSlot();   // 发送前就该写好
            await new Promise(r => setTimeout(r, 900));
            const slot = App._readPendingSlot();
            const toast = document.querySelector('#toast-container .toast.warning');
            return JSON.stringify({
                calls: window.__calls,                 // 必须 ===1：证明本用例真的走到了发送环节
                slotRightAfterSend: slotRightAfterSend,
                slotKept: !!(slot && slot.body),       // 失败后仍留着
                statusPending: App.mirrorStatus.pending,
                failures: App.mirrorStatus.failures,
                lastError: App.mirrorStatus.lastError,
                toast: toast ? toast.textContent.slice(0, 50) : null,
            });
        })()`, true));
        console.log('  (debug)', JSON.stringify(inj).slice(0, 320));
        check('MIRROR_FAIL_NOT_SILENT',
            inj.calls === 1 && inj.slotRightAfterSend === true && inj.slotKept === true
            && inj.statusPending === true && inj.failures === 1 && !!inj.toast,
            JSON.stringify(inj).slice(0, 300));

        // 因果前提：此刻服务器**还没有**探针值（否则后面的"重试送达"可能只是遗留值恰好相等）
        const midAsh = (await api('/api/v1/state')).ashTarget;
        check('MIRROR_NOT_DELIVERED_YET', String(midAsh) === String(baseline),
            `服务器 ashTarget=${midAsh}（应为基线 ${baseline}）`);

        // ---------- 用例1b：恢复后 online 触发重试，且服务器真实收到 ----------
        const retry = JSON.parse(await evalJs(`(async () => {
            Api.putStateBody = window.__realPut;        // 恢复正常发送
            window.dispatchEvent(new Event('online'));  // 重试触发点之一
            await new Promise(r => setTimeout(r, 3000));
            const slot = App._readPendingSlot();
            return JSON.stringify({
                slotCleared: !(slot && slot.body),
                statusPending: App.mirrorStatus.pending,
                failures: App.mirrorStatus.failures,
                lastOkAt: !!App.mirrorStatus.lastOkAt,
            });
        })()`, true));
        check('MIRROR_RETRY_ON_ONLINE',
            retry.slotCleared === true && retry.statusPending === false
            && retry.failures === 0 && retry.lastOkAt === true,
            JSON.stringify(retry).slice(0, 300));

        let sv = null;
        for (let i = 0; i < 20; i++) {
            sv = await api('/api/v1/state');
            if (String(sv.ashTarget) === String(probe)) break;
            await sleep(500);
        }
        check('MIRROR_RETRY_ARRIVED', String(sv.ashTarget) === String(probe), `服务器 ashTarget = ${sv.ashTarget}`);

        // ---------- 用例2：守卫明确拒绝 ≠ 传输失败 ----------
        // rejected 必须：记状态但**不留**待发副本（重试结果必然相同），且内容不变时不重发
        const rej = JSON.parse(await evalJs(`(async () => {
            window.__calls2 = 0;
            Api.putStateBody = () => { window.__calls2++; return Promise.resolve(
                { ok: false, rejected: true, reason: 'stale_store: E2E 注入的守卫拒绝' }); };
            App.mirrorStatus.failures = 0; App.mirrorStatus.rejected = 0;
            App.store.ashTarget = ${JSON.stringify(probe)};   // 与上次成功内容相同
            App._mirrorSent = null;                          // 强制真的发一次
            App.saveStore(); App._flushMirror(false);
            await new Promise(r => setTimeout(r, 900));
            const slot = App._readPendingSlot();
            const info = document.querySelector('#toast-container .toast.info');
            const first = { calls: window.__calls2, slotKept: !!(slot && slot.body),
                rejected: App.mirrorStatus.rejected, failures: App.mirrorStatus.failures,
                toast: info ? info.textContent.slice(0, 40) : null };
            // 再来一次内容完全相同的保存：不应再发（否则每次防抖都发一次注定被拒的整库）
            App.saveStore(); App._flushMirror(false);
            await new Promise(r => setTimeout(r, 600));
            const second = { calls: window.__calls2, stats: App._mirrorStats };
            Api.putStateBody = window.__realPut;
            return JSON.stringify({ first: first, second: second });
        })()`, true));
        console.log('  (debug)', JSON.stringify(rej).slice(0, 320));
        check('MIRROR_REJECTED_NOT_RETRIED',
            rej.first.calls === 1 && rej.first.slotKept === false && rej.first.rejected === 1
            && rej.first.failures === 1 && !!rej.first.toast && rej.second.calls === 1,
            `rejected=${rej.first.rejected} 槽保留=${rej.first.slotKept} 第二次发送次数=${rej.second.calls}`);

        // ---------- 复原：ashTarget 写回基线 ----------
        // 注意：_flushMirror 只发"待发内容"，没有待发就是空操作 —— 必须 saveStore 排入。
        await evalJs(`App.mirrorStatus.failures = 0; App._mirrorSent = null;
            App.store.ashTarget = ${JSON.stringify(baseline)};
            App.saveStore(); App._flushMirror(false); true`);
        let restored = null;
        for (let i = 0; i < 20; i++) {
            restored = await api('/api/v1/state');
            if (String(restored.ashTarget) === String(baseline)) break;
            await sleep(500);
        }
        check('MIRROR_RESTORED', String(restored.ashTarget) === String(baseline),
            `ashTarget 复原为 ${restored.ashTarget}（基线 ${baseline}）`);
        // 服务器上的重介采样必须还在（镜像不拥有 heavy_samples）：
        // 没有 GET 接口，用"新插入的自增 id 是否在原 id 之后"判断表没被清空
        const probeSample = await post('/api/v1/samples/heavy-ash', { ts: '2000-01-02 00:00:00',
            rho: 1.51, ash_content: 8.1, source: 'E2E探针' }).catch(() => null);
        check('MIRROR_KEEPS_HEAVY_SAMPLES',
            !!(probeSample && probeSample.id > heavyId),
            `镜像前播种 id=${heavyId}，镜像后新插入 id=${probeSample && probeSample.id}`
            + '（若表被清空会重新拿到 1）');
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
