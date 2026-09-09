// 通过真实 UI 代码路径(headless Edge + CDP)批量导入三表 xlsx。
// 与手工在"批量数据导入"页操作完全等价:localStorage 主存储 + PUT /state 镜像到后端。
// 用法: node import_xlsx_via_ui.js [目录]   (目录默认为桌面"导入数据/新")
// 前置: 后端已运行于 http://127.0.0.1:8000/
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const URL = 'http://127.0.0.1:8000/';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 9379;
const UD = path.join(process.env.TEMP, 'dmcs-cdp-import');
const ROOT = process.argv[2] || 'C:\\Users\\25925\\Desktop\\web(2)导入数据\\新';

// 在目录中挑每类文件(优先 9-4 新版:按修改时间取最新)
function pickFiles(dir) {
    const files = fs.readdirSync(dir).filter(f => /\.xlsx$/i.test(f) && !f.startsWith('~$'));
    const latest = (kw) => files.filter(f => f.includes(kw))
        .map(f => ({ f, m: fs.statSync(path.join(dir, f)).mtimeMs }))
        .sort((a, b) => b.m - a.m)[0];
    return {
        float: latest('浮精'),
        ash_density: latest('密度'),      // 注意:不能用"灰分"(粗精煤泥文件名也含"灰分")
        coarse: latest('粗精煤泥'),
    };
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
    if (!fs.existsSync(EDGE)) { console.error('未找到 Edge:', EDGE); process.exit(1); }
    const picks = pickFiles(ROOT);
    for (const k of Object.keys(picks)) {
        if (!picks[k]) { console.error('目录中未找到类别文件:', k, 'in', ROOT); process.exit(1); }
        console.log('待导入', k, '→', picks[k].f);
    }
    if (fs.existsSync(UD)) fs.rmSync(UD, { recursive: true, force: true });
    const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run',
        `--user-data-dir=${UD}`, `--remote-debugging-port=${PORT}`, '--window-size=1680,1200', URL],
        { stdio: 'ignore' });
    try {
        let page;
        for (let i = 0; i < 30; i++) {
            try { const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); page = l.find(t => t.type === 'page' && t.url.includes('127.0.0.1')); if (page) break; } catch (e) {}
            await sleep(500);
        }
        if (!page) throw new Error('no page');
        const ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = fail; });
        await sleep(2500);
        let idc = 0; const pending = new Map();
        ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
        const send = (method, params) => new Promise((ok) => { const id = ++idc; pending.set(id, ok); ws.send(JSON.stringify({ id, method, params })); });
        const evalJs = async (expr, awaitPromise = false) => {
            const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise });
            if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text || 'eval error');
            return r.result.result.value;
        };
        const apiGet = async (p) => (await (await fetch('http://127.0.0.1:8000/api/v1' + p)).json());

        const counts = async () => ({
            coarse: (await apiGet('/records?category=coarse&limit=1')).total,
            float: (await apiGet('/records?category=float&limit=1')).total,
            ash_density: (await apiGet('/records?category=ash_density&limit=1')).total,
        });
        const before = await counts();
        console.log('DB before:', JSON.stringify(before));

        // 依次导入:浮精 → 灰分密度 → 粗精煤泥(最后,因其导入后自动训练)
        for (const key of ['float', 'ash_density', 'coarse']) {
            const file = path.join(ROOT, picks[key].f);
            const b64 = fs.readFileSync(file).toString('base64');
            await evalJs(`(async () => {
                const bytes = Uint8Array.from(atob(${JSON.stringify(b64)}), c => c.charCodeAt(0));
                ImportPage.processFile(new File([bytes], ${JSON.stringify(picks[key].f)}));
                return true;
            })()`, true);
            // 等解析完成
            const probe = key === 'coarse'
                ? `!!(ImportPage.parsedFactors && ImportPage.parsedFactors.length)`
                : `!!(ImportPage.parsedData && ImportPage.parsedData.rows && ImportPage.parsedData.rows.length)`;
            let parsed = false;
            for (let i = 0; i < 60; i++) {
                parsed = await evalJs(probe);
                if (parsed) break;
                await sleep(300);
            }
            if (!parsed) throw new Error('解析超时: ' + picks[key].f);
            const nRows = await evalJs(key === 'coarse'
                ? 'ImportPage.parsedFactors.length'
                : 'ImportPage.parsedData.rows.length');
            console.log(`[${key}] ${picks[key].f} 解析 ${nRows} 行,开始导入...`);
            // 确认导入(coarse 为 async,含自动训练)
            const out = await evalJs(`(async () => {
                ${key === 'coarse' ? 'await ImportPage.confirmImportFactors();' : 'ImportPage.confirmImport();'}
                return JSON.stringify({ coarseCoal: App.store.coarseCoal.length, floatCoal: App.store.floatCoal.length,
                    calcLogs: App.store.calcLogs.length });
            })()`, true);
            console.log(`[${key}] 完成,store:`, out);
            if (key === 'coarse') {
                // 一次性数据修复:旧 fixDay 规则把 7.2/7.3 解析成 7月20/30 日(源表实为 7月2/3 日),
                // 种子数据里遗留的 2026-07-20/30 孤儿行在此清除(修正后的导入已写回 07-02/03)
                const fixed = await evalJs(`(() => {
                    const bad = /^2026-07-(20|30) /;
                    const c0 = App.store.coarseCoal.length, m0 = App.store.magneticTail.length;
                    App.store.coarseCoal = App.store.coarseCoal.filter(c => !bad.test(c.timestamp));
                    App.store.magneticTail = App.store.magneticTail.filter(c => !bad.test(c.timestamp));
                    App.saveStore();
                    return JSON.stringify({ removedCoarse: c0 - App.store.coarseCoal.length,
                        removedMag: m0 - App.store.magneticTail.length });
                })()`);
                console.log('[coarse] 旧误解析孤儿行清理:', fixed);
            }
            // 等 PUT /state 镜像落库(store 计数稳定 + API 可见)
            let stable = 0, last = '';
            for (let i = 0; i < 40; i++) {
                await sleep(500);
                const c = await counts();
                const s = JSON.stringify(c);
                if (s === last) { stable++; if (stable >= 3) break; } else stable = 0;
                last = s;
            }
            console.log(`[${key}] DB now:`, last);
        }

        const after = await counts();
        console.log('DB after:', JSON.stringify(after));

        // 全量重训练(range=all,含 8 月新工况),并读取结果与数据驱动 K
        const trainOut = JSON.parse(await evalJs(`(async () => {
            const ok = await App.retrainCoarseModelAsync('all');
            const cm = App.store.coarseModel || {};
            const k = App.densityGainK();
            return JSON.stringify({ ok, production: cm.production, n: cm.n, range: cm.range,
                mlr: { r2: cm.mlr && cm.mlr.metrics && cm.mlr.metrics.r2, q2: cm.mlr && cm.mlr.metrics && cm.mlr.metrics.q2, q2Time: cm.mlr && cm.mlr.metrics && cm.mlr.metrics.q2Time },
                pls: { r2: cm.pls && cm.pls.metrics && cm.pls.metrics.r2, q2: cm.pls && cm.pls.metrics && cm.pls.metrics.q2, q2Time: cm.pls && cm.pls.metrics && cm.pls.metrics.q2Time, A: cm.pls && cm.pls.A },
                k });
        })()`, true));
        console.log('RETRAIN(all):', JSON.stringify(trainOut, null, 2));
        console.log('RESULT:', JSON.stringify({ before, after, retrain: trainOut }));
        ws.close();
        process.exitCode = 0;
    } catch (e) {
        console.error('ERROR:', e.message);
        process.exitCode = 1;
    } finally {
        try { edge.kill(); } catch (e) {}
        setTimeout(() => process.exit(), 300);
    }
})();
