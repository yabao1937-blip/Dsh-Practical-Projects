// 验证「实测密度计」：展示位置、量程校验、补录层级，以及**最重要的一条——不参与任何计算**。
//
// 为什么要有这个脚本：它是"仅展示"这个承诺的唯一守卫。如果以后有人把实测密度接进
// 建议密度/灰分公式（哪怕只是"顺手"），下面的 CALC_UNAFFECTED 会立刻变红。
//
// 用法：node scripts/verify_density_actual.js
//   DMCS_URL 指向哪个库就往哪个库写镜像，**请指向临时库**（脚本会检查目标是否为空库，
//   非空则拒跑，除非 DMCS_ALLOW_REAL_DB=1）。
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const BASE = (process.env.DMCS_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');
const URL = BASE + '/';
// 注意：本文件的 `const URL` 会遮蔽全局 URL 类，别写 new URL(URL)
const HOST = BASE.replace(/^https?:\/\//, '');
const CANDIDATES = [
    process.env.DMCS_EDGE,
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    '/usr/bin/microsoft-edge-stable', '/usr/bin/microsoft-edge',
    '/usr/bin/google-chrome', '/usr/bin/chromium',
];
const EDGE = CANDIDATES.find(p => p && fs.existsSync(p));
const PORT = 9300 + Math.floor(Math.random() * 400);
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const fails = [];
function check(name, ok, detail) {
    console.log(`${name}: ${ok ? 'PASS' : 'FAIL'}${detail ? ' | ' + detail : ''}`);
    if (!ok) fails.push(name);
}
const api = async (p, init) => {
    const opts = Object.assign({}, init);
    if (process.env.DMCS_WRITE_TOKEN) {
        opts.headers = Object.assign({}, opts.headers, { 'X-DMCS-Token': process.env.DMCS_WRITE_TOKEN });
    }
    const r = await fetch(BASE + p, opts);
    if (!r.ok) throw new Error(`${p} → HTTP ${r.status} ${(await r.text().catch(() => '')).slice(0, 200)}`);
    return r.json();
};

(async () => {
    if (!EDGE) { console.error('ERROR: 未找到浏览器可执行文件（可用 DMCS_EDGE 指定）'); process.exit(2); }
    console.log('浏览器:', EDGE, '| 目标:', URL, '| Node', process.version);

    let serverState = null;
    try {
        serverState = await api('/api/v1/state');
    } catch (e) {
        console.error('ERROR(前置): 无法读取服务器状态:', e.message);
        console.log('DENSITY_ACTUAL: FAIL (前置阶段异常)');
        process.exit(1);
    }
    const nonEmpty = ['coarseCoal', 'floatCoal', 'calcLogs']
        .some(k => (serverState[k] || []).length > 0);
    if (nonEmpty && process.env.DMCS_ALLOW_REAL_DB !== '1') {
        console.error('ERROR: 目标库非空，本脚本会产生镜像写入；请指向临时库（或显式 DMCS_ALLOW_REAL_DB=1）。');
        process.exit(2);
    }

    const UD = path.join(process.env.TEMP || '/tmp', 'dmcs-cdp-densityactual-' + Date.now());
    const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run',
        `--user-data-dir=${UD}`, `--remote-debugging-port=${PORT}`, '--window-size=1680,1200', URL],
        { stdio: 'ignore' });
    try {
        let page;
        for (let i = 0; i < 40; i++) {
            try {
                const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
                page = l.find(t => t.type === 'page' && t.url.includes(HOST));
                if (page) break;
            } catch (e) { /* 端口未就绪 */ }
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
        // 轮询等页面初始化（app.js 的 App 是脚本作用域的 const，不能用 window.App 判断）
        let ready = false, lastErr = null;
        for (let i = 0; i < 60; i++) {
            try {
                if (await evalJs("typeof App !== 'undefined' && !!App.store && !!App.store.coarseCoal")) { ready = true; break; }
            } catch (e) { lastErr = e.message; }
            await sleep(500);
        }
        if (!ready) throw new Error('页面 30 秒内未完成初始化' + (lastErr ? '；' + lastErr : ''));

        // ---------- 1) 在线仪表表里有一行"实测密度计"，初始未录入 ----------
        const row0 = JSON.parse(await evalJs(`(() => {
            App.goToPage('page-collect');
            CollectPage.renderTable();
            const tr = [...document.querySelectorAll('#collect-tbody tr')]
                .find(r => r.children[0].textContent.includes('实测密度计'));
            return JSON.stringify(tr ? {
                cells: [...tr.children].map(td => td.textContent.trim()),
                name: tr.children[0].textContent.trim(),
            } : null);
        })()`));
        check('INSTRUMENT_ROW_EXISTS', !!row0, row0 ? row0.cells.join(' | ') : '表里找不到"实测密度计"行');
        check('INSTRUMENT_ROW_STARTS_EMPTY', !!row0 && row0.cells[2] === '—' && row0.cells[5] === '未录入',
            row0 ? `当前值=${row0.cells[2]} 来源=${row0.cells[5]}` : '（未录入时应显示 — 且来源列为"未录入"）');

        // ---------- 2) 记录设置前的计算口径（用于证明"不参与计算"）----------
        const before = JSON.parse(await evalJs(`(() => {
            const g = App.computeDensityGuidance(App.store.ashTarget);
            return JSON.stringify({
                rhoNew: g.rhoNew, rhoCur: g.rhoCur, deltaRho: g.deltaRho, valid: g.valid,
                totalAsh: App.resolveTotalAsh(), heavyAsh: App.getHeavyAsh(),
                coarseAsh: App.resolveCoarseAsh(), floatAsh: App.resolveFloatAsh(),
                totalAmount: App.resolveTotalAmount(),
            });
        })()`));

        // ---------- 3) 按现场路径录入（commitTotalInput = 表格双击那条路）----------
        const setv = JSON.parse(await evalJs(`(() => {
            CollectPage.commitTotalInput('density_actual', 1.53);
            const dev = App.densityActualDeviation();
            const tr = [...document.querySelectorAll('#collect-tbody tr')]
                .find(r => r.children[0].textContent.includes('实测密度计'));
            return JSON.stringify({
                manual: App.store.instrumentInputs.density_actual.manual,
                manualAt: !!App.store.instrumentInputs.density_actual.manualAt,
                layer: App.instrumentLayer('density_actual'),
                dev: dev.dev, online: dev.online, actual: dev.actual,
                rowValue: tr ? tr.children[2].textContent.trim() : null,
                rowSource: tr ? tr.children[5].textContent.trim() : null,
                rowStatus: tr ? tr.children[7].textContent.trim() : null,
            });
        })()`));
        check('MANUAL_INPUT_ACCEPTED', setv.manual === 1.53 && setv.manualAt === true,
            `manual=${setv.manual} layer=${setv.layer}`);
        check('ROW_SHOWS_VALUE_AND_DEV', String(setv.rowValue).includes('1.53') && String(setv.rowValue).includes('Δ'),
            `当前值单元格="${setv.rowValue}"（应含实测值与 Δ）`);
        check('DEV_COMPUTED', setv.dev != null && Math.abs(setv.dev - +(setv.actual - setv.online).toFixed(3)) < 1e-9,
            `实测 ${setv.actual} / 在线 ${setv.online} / Δ ${setv.dev}`);

        // ---------- 4) 量程校验：离谱值拒收，越界值标黄 ----------
        const guard = JSON.parse(await evalJs(`(() => {
            CollectPage.commitTotalInput('density_actual', 1.9);      // 超出硬限，应拒收
            const after = App.store.instrumentInputs.density_actual.manual;
            CollectPage.commitTotalInput('density_actual', 1.62);     // 硬限内、软限外：接受但状态标黄
            const tr = [...document.querySelectorAll('#collect-tbody tr')]
                .find(r => r.children[0].textContent.includes('实测密度计'));
            return JSON.stringify({
                afterReject: after, afterSoft: App.store.instrumentInputs.density_actual.manual,
                status: tr ? tr.children[7].textContent.trim() : null,
            });
        })()`));
        check('RANGE_REJECT_HARD_LIMIT', guard.afterReject === 1.53, `拒收后仍为 ${guard.afterReject}`);
        check('RANGE_SOFT_WARN', guard.afterSoft === 1.62 && /偏离/.test(String(guard.status)),
            `1.62 已接受，质量状态="${guard.status}"`);

        // ---------- 5) 关键不变量：设置实测密度**不改变任何计算结果** ----------
        const after = JSON.parse(await evalJs(`(() => {
            const g = App.computeDensityGuidance(App.store.ashTarget);
            return JSON.stringify({
                rhoNew: g.rhoNew, rhoCur: g.rhoCur, deltaRho: g.deltaRho, valid: g.valid,
                totalAsh: App.resolveTotalAsh(), heavyAsh: App.getHeavyAsh(),
                coarseAsh: App.resolveCoarseAsh(), floatAsh: App.resolveFloatAsh(),
                totalAmount: App.resolveTotalAmount(),
            });
        })()`));
        const same = JSON.stringify(before) === JSON.stringify(after);
        check('CALC_UNAFFECTED', same,
            same ? '设置实测密度前后，建议密度/灰分/用量全部一致'
                 : `计算被影响了！before=${JSON.stringify(before)} after=${JSON.stringify(after)}`);

        // ---------- 6) 补录（录入层）：清掉手动值后应显示补录值，来源=录入 ----------
        const layer = JSON.parse(await evalJs(`(() => {
            const now = App.formatDate(new Date());
            App.store.calcLogs.push({
                id: (App.store.calcLogs || []).length + 1, timestamp: now,
                calc_type: 'density_actual', input_json: JSON.stringify({ value: 1.55 }), output_json: '{}',
            });
            App.saveStore();
            CollectPage.commitTotalInput('density_actual', null);   // 清空手动值 → 回落到录入层
            CollectPage.renderTable();
            const tr = [...document.querySelectorAll('#collect-tbody tr')]
                .find(r => r.children[0].textContent.includes('实测密度计'));
            return JSON.stringify({
                value: App.resolveInstrument('density_actual'),
                layer: App.instrumentLayer('density_actual'),
                cell: tr ? tr.children[2].textContent.trim() : null,
            });
        })()`));
        check('ENTRY_LAYER_FALLBACK', layer.value === 1.55 && layer.layer === '录入',
            `值=${layer.value} 来源=${layer.layer} 单元格="${layer.cell}"`);

        // ---------- 7) 合并系统卡片上显示 ----------
        const card = JSON.parse(await evalJs(`(() => {
            App.goToPage('page-overview');
            OverviewPage.refresh();
            return JSON.stringify({
                value: document.getElementById('density-actual-total').textContent,
                dev: document.getElementById('density-actual-dev-total').textContent,
                devColor: document.getElementById('density-actual-dev-total').style.color,
                suggestion: document.getElementById('density-total').textContent,
            });
        })()`));
        check('CARD_SHOWS_ACTUAL', card.value === '1.550', `卡片实测密度=${card.value}`);
        check('CARD_SHOWS_DEV', /Δ/.test(card.dev) && /在线/.test(card.dev), `卡片偏差="${card.dev}"`);

        // ---------- 8) 决策日志 ctx 里记了一笔（仅记录，不参与计算）----------
        const ctx = JSON.parse(await evalJs(`(() => {
            const c = App._decisionCtx();
            return JSON.stringify({ densityActual: c.densityActual, keys: Object.keys(c).length });
        })()`));
        check('DECISION_CTX_RECORDS_IT', ctx.densityActual === 1.55,
            `ctx.densityActual=${ctx.densityActual}（共 ${ctx.keys} 个工况字段）`);

        // ---------- 9) 导出表里多了一列"实测密度" ----------
        const exp = JSON.parse(await evalJs(`(() => {
            const origWrite = XLSX.writeFile, origSheet = XLSX.utils.aoa_to_sheet;
            let head = null, firstRow = null;
            XLSX.utils.aoa_to_sheet = function (aoa) {
                if (!head && aoa && aoa.length > 1 && aoa[0].includes('决策时间')) { head = aoa[0]; firstRow = aoa[1]; }
                return origSheet.apply(this, arguments);
            };
            XLSX.writeFile = function () { /* 不真的下载 */ };
            // 先造一条决策日志：导出函数在日志为空时会直接 return（那是正常行为）
            App.store.densityDecisionLog = (App.store.densityDecisionLog || []).concat([{
                ts: App.formatDate(new Date()), trigger: 'density_set', scheme: 'total',
                target: 8.5, tol: 0.1, rhoCur: 1.49, rhoNew: 1.48, deltaRho: -0.01, deltaA: 0.15,
                kUsed: 0.075, kSource: 'default', heavyAsh: 7.9, totalAsh: 8.55,
                ctx: App._decisionCtx(), response: null,
            }]);
            App.exportDensityDecisionLog();
            XLSX.writeFile = origWrite; XLSX.utils.aoa_to_sheet = origSheet;
            return JSON.stringify({ hasColumn: !!(head && head.includes('实测密度')), idx: head ? head.indexOf('实测密度') : -1 });
        })()`));
        check('EXPORT_HAS_COLUMN', exp.hasColumn === true, `"实测密度"列下标=${exp.idx}`);

        ws.close();
    } catch (e) {
        console.error('ERROR:', e.message);
        fails.push('EXCEPTION');
    } finally {
        try { edge.kill(); } catch (e) {}
        console.log(fails.length ? `DENSITY_ACTUAL: FAIL (${fails.join(', ')})` : 'DENSITY_ACTUAL: PASS');
        process.exitCode = fails.length ? 1 : 0;
        setTimeout(() => process.exit(), 300);
    }
})();
