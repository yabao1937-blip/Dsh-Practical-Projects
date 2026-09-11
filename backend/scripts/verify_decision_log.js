// 端到端验证:密度决策日志 —— 手动设定密度 → 日志条目(含工况上下文) → PUT /state 落库(auto_state)
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const URL = 'http://127.0.0.1:8000/';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 9383;
const UD = path.join(process.env.TEMP, 'dmcs-cdp-decisionlog');
if (fs.existsSync(UD)) fs.rmSync(UD, { recursive: true, force: true });
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
    const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run',
        `--user-data-dir=${UD}`, `--remote-debugging-port=${PORT}`, '--window-size=1680,1200', URL], { stdio: 'ignore' });
    try {
        let page;
        for (let i = 0; i < 30; i++) {
            try { const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); page = l.find(t => t.type === 'page' && t.url.includes('127.0.0.1')); if (page) break; } catch (e) {}
            await sleep(500);
        }
        const ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = fail; });
        await sleep(3000);   // 等 autoPull 同步 + init
        let idc = 0; const pending = new Map();
        ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
        const send = (method, params) => new Promise((ok) => { const id = ++idc; pending.set(id, ok); ws.send(JSON.stringify({ id, method, params })); });
        const evalJs = async (expr, awaitPromise = false) => {
            const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise });
            if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text);
            return r.result.result.value;
        };

        const n0 = await evalJs('(App.store.densityDecisionLog || []).length');
        // 操作员手动设定密度(决策日志应记录 density_set,含工况上下文)
        await evalJs("App.setInstrumentInput('density', { manual: 1.485 }); true");
        const out = JSON.parse(await evalJs(`JSON.stringify((App.store.densityDecisionLog || [])[App.store.densityDecisionLog.length - 1] || null)`));
        console.log('日志条目:', JSON.stringify(out, null, 2).slice(0, 900));

        // 整库镜像：已改为合并发送（App.MIRROR_DEBOUNCE_MS=2000ms），这里显式冲一次。
        await evalJs('App._flushMirror(true); true');
        // 然后**轮询**等服务器侧出现，不要用固定 sleep 猜时间：
        // 镜像是不等待响应的异步发送，而整库重写（529 条记录/175KB）提交需要时间；
        // 若此脚本在提交完成前就 kill 掉 Edge，那次 PUT 会被直接中断（这正是它之前报 FAIL 的原因）。
        let last = null, log = [];
        for (let i = 0; i < 24; i++) {
            const dbSide = await (await fetch('http://127.0.0.1:8000/api/v1/state')).json();
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
        ws.close();
        process.exitCode = (ok && n1 === n0 + 1) ? 0 : 1;
    } catch (e) {
        console.error('ERROR:', e.message);
        process.exitCode = 1;
    } finally {
        try { edge.kill(); } catch (e) {}
        setTimeout(() => process.exit(), 300);
    }
})();
