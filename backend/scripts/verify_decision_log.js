// 端到端验证:密度决策日志 —— 手动设定密度 → 日志条目(含工况上下文) → PUT /state 落库(auto_state)
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const BASE = (process.env.DMCS_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');
const URL = BASE + '/';
// 注意：本文件的 `const URL` 是字符串，会**遮蔽**全局 URL 类，
// 所以不能写 new URL(URL)（会报 URL is not a constructor）—— 直接剥协议前缀。
const HOST = BASE.replace(/^https?:\/\//, '');   // 形如 192.168.43.104:8013
// 浏览器候选与 verify_mirror_e2e.js 保持一致（原来只硬编码一个 Windows 路径，
// 与 ci.yml 的预检、另一个脚本的候选列表三处不一致，换 runner 就会红在与代码无关的地方）
const CANDIDATES = [
    process.env.DMCS_EDGE,
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    '/usr/bin/microsoft-edge-stable', '/usr/bin/microsoft-edge',
    '/usr/bin/google-chrome', '/usr/bin/chromium',
];
const EDGE = CANDIDATES.find(p => p && fs.existsSync(p));
const PORT = 9300 + Math.floor(Math.random() * 400);   // 随机端口：避免连到遗留的调试浏览器
const UD = path.join(process.env.TEMP || '/tmp', 'dmcs-cdp-decisionlog-' + Date.now());
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
    if (!EDGE) {
        console.error('ERROR: 未找到浏览器可执行文件，请用环境变量 DMCS_EDGE 指定');
        process.exit(2);
    }
    console.log('浏览器:', EDGE, '| 目标:', URL);
    const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run',
        `--user-data-dir=${UD}`, `--remote-debugging-port=${PORT}`, '--window-size=1680,1200', URL], { stdio: 'ignore' });
    try {
        let page;
        for (let i = 0; i < 30; i++) {
            try { const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); page = l.find(t => t.type === 'page' && t.url.includes(HOST)); if (page) break; } catch (e) {}
            await sleep(500);
        }
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

        // 等页面初始化完成（轮询，不用固定 sleep：CI 冷启动更慢）
        // typeof App（不是 window.App）：app.js 的 App 是脚本作用域的 const
        //
        // 2026-09-21 CI run 35556015087 红的教训：原来只等 `!!App.store.coarseCoal`
        // ——**空数组也满足**，于是脚本在种子数据还没装进 store 时就往下走，
        // 整库 PUT 被 stale 守卫正确拒掉（coarse 0<113），DECISION_LOG 假红。
        // 必须等 App.__ready === true（它在种子数据与迁移之后才置位，其他 verify_*.js 都这么等）。
        let appReady = false, lastErr = null;
        for (let i = 0; i < 60; i++) {
            try {
                if (await evalJs("typeof App !== 'undefined' && App.__ready === true"
                        + " && !!App.store && Array.isArray(App.store.coarseCoal)")) {
                    appReady = true; break;
                }
            } catch (e) { lastErr = e.message; }
            await sleep(500);
        }
        if (!appReady) {
            throw new Error('页面 30 秒内未完成初始化（等 App.__ready）'
                + (lastErr ? '；最后一次求值异常: ' + lastErr : ''));
        }

        const env0 = JSON.parse(await evalJs(`JSON.stringify({
            coarse: (App.store.coarseCoal||[]).length,
            float: (App.store.floatCoal||[]).length,
            ashDensity: (App.store.calcLogs||[]).filter(l => l.calc_type === 'ash_density').length,
        })`));
        console.log('页面记录快照:', JSON.stringify(env0));
        const srv0 = await (await fetch(BASE + '/api/v1/state')).json();
        console.log('服务器记录快照:', JSON.stringify({
            coarse: (srv0.coarseCoal || []).length, float: (srv0.floatCoal || []).length,
            ashDensity: (srv0.calcLogs || []).length,
        }));
        // 非空库守卫（与 verify_mirror_e2e.js 同规格）：本脚本会**真的写密度**并产生一条
        // density_set 决策日志，指到真实库就会把测试值留在现场（2026-09-19 已发生过一次：
        // 密度被写成 1.485、决策日志多出一条测试条目）。因此非空库一律拒跑，除非显式放行。
        const nonEmpty = ['coarseCoal', 'floatCoal', 'calcLogs'].some(k => (srv0[k] || []).length > 0);
        if (nonEmpty && process.env.DMCS_ALLOW_REAL_DB !== '1') {
            console.error('ERROR: 目标库非空（' + JSON.stringify({
                coarse: (srv0.coarseCoal || []).length, float: (srv0.floatCoal || []).length,
                ash_density: (srv0.calcLogs || []).length,
            }) + '），本脚本会写密度值并新增决策日志；'
                + '请指向临时库（DMCS_URL=...），或确知后果后设置 DMCS_ALLOW_REAL_DB=1。');
            // 不能用 process.exit()：此时 CDP 的 WebSocket 还开着，Node 会在 win/async.c 触发
            // `Assertion failed: !(handle->flags & UV_HANDLE_CLOSING)` 直接崩（exit 0xC0000409）。
            // 设 exitCode 后 return，交给 finally 关浏览器、再按 exitCode 退出。
            process.exitCode = 2;
            return;
        }

        // 先存档密度层（manual/manualAt/autoExec）与决策日志条数，跑完原样恢复：
        // 这条"用测试值改现场密度"的路径必须自己收尾，不能留给现场。
        const savedDensity = JSON.parse(await evalJs(
            `JSON.stringify(((App.store.instrumentInputs || {}).density) || null)`));
        const savedLatch = JSON.parse(await evalJs('JSON.stringify(App.store.densityActionLatch || null)'));
        const savedLastMove = await evalJs('App.store.densityLastMoveAt || 0');

        const n0 = await evalJs('(App.store.densityDecisionLog || []).length');
        // 操作员手动设定密度(决策日志应记录 density_set,含工况上下文)
        await evalJs("App.setInstrumentInput('density', { manual: 1.485 }); true");
        const out = JSON.parse(await evalJs(`JSON.stringify((App.store.densityDecisionLog || [])[App.store.densityDecisionLog.length - 1] || null)`));
        console.log('日志条目:', JSON.stringify(out, null, 2).slice(0, 900));

        // 整库镜像：已改为合并发送（App.MIRROR_DEBOUNCE_MS=2000ms），这里显式冲一次。
        // 另外补一次**显式 await 的 PUT**：本脚本验证的是"决策日志这条数据链能否到服务器"，
        // 而镜像的调度机制（防抖/待发槽/重试/守卫拒绝处置）由 verify_mirror_e2e.js 专门负责 ——
        // 这里用确定性的写入，避免"服务器侧条数 0"究竟是没发出去还是被守卫拒绝看不出来
        // （CI run 12 就卡在这个不可见性上）。
        const putRes = await evalJs(
            `(async () => JSON.stringify(await Api.putStateBody(JSON.stringify(App.store), {})))()`, true);
        console.log('显式整库 PUT 结果:', putRes);
        await evalJs('App._flushMirror(true); true');
        // 然后**轮询**等服务器侧出现，不要用固定 sleep 猜时间：
        // 镜像是不等待响应的异步发送，而整库重写（529 条记录/175KB）提交需要时间；
        // 若此脚本在提交完成前就 kill 掉 Edge，那次 PUT 会被直接中断（这正是它之前报 FAIL 的原因）。
        let last = null, log = [];
        for (let i = 0; i < 24; i++) {
            const dbSide = await (await fetch(BASE + '/api/v1/state')).json();
            log = dbSide.densityDecisionLog || [];
            last = log[log.length - 1];
            if (last && out && last.ts === out.ts) break;   // 以浏览器侧那条的时间戳为准
            await sleep(500);
        }
        console.log('服务器侧条数:', log.length, '| 最新 trigger:', last && last.trigger,
            '| rhoNew:', last && last.rhoNew, '| ctx.rawAsh:', last && last.ctx && last.ctx.rawAsh,
            '| ctx.miningFace:', last && last.ctx && last.ctx.miningFace);
        const ok = !!(last && out && last.ts === out.ts && last.trigger === 'density_set'
            && last.rhoNew === 1.485 && last.ctx && typeof last.ctx.rawAsh === 'number');
        console.log(ok ? 'DECISION_LOG: PASS' : 'DECISION_LOG: FAIL');
        // 节流验证:1秒内重复同类设定不应新增
        await evalJs("App.setInstrumentInput('density', { manual: 1.487 }); true");
        await sleep(300);
        const n1 = await evalJs('(App.store.densityDecisionLog || []).length');
        console.log('节流(10分钟内同类不重复):', n1 === n0 + 1 ? 'PASS' : `FAIL(${n0}->${n1})`);

        // 收尾：把密度层与守卫书签恢复成跑之前的样子，并冲一次镜像
        const restored = JSON.parse(await evalJs(`(async () => {
            const st = App.store;
            if (!st.instrumentInputs) st.instrumentInputs = {};
            if (${JSON.stringify(savedDensity)} === null) delete st.instrumentInputs.density;
            else st.instrumentInputs.density = ${JSON.stringify(savedDensity)};
            st.densityActionLatch = ${JSON.stringify(savedLatch)};
            st.densityLastMoveAt = ${JSON.stringify(savedLastMove)};
            await Api.putStateBody(JSON.stringify(st), {});
            const now = st.instrumentInputs.density || null;
            return JSON.stringify({ now: now, ok: JSON.stringify(now) === ${JSON.stringify(JSON.stringify(savedDensity))} });
        })()`, true));
        console.log('RESTORE: ' + (restored.ok ? 'PASS' : 'FAIL')
            + ' | 密度层已恢复为 ' + JSON.stringify(restored.now));
        ws.close();
        process.exitCode = (ok && n1 === n0 + 1 && restored.ok) ? 0 : 1;
    } catch (e) {
        console.error('ERROR:', e.message);
        process.exitCode = 1;
    } finally {
        try { edge.kill(); } catch (e) {}
        setTimeout(() => process.exit(), 300);
    }
})();
