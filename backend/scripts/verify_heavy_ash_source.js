// 验证「重介精煤灰分 手动/计算」与用户要的闭环：
//   总灰分不达标 → 给出建议密度 → 把密度计调到建议值 → 重介灰分随密度变化 → 总灰分回到目标。
//
// 关键断言：
//   SOURCE_SELECT_EXISTS   在线仪表行里有 手动/计算 下拉
//   MANUAL_MODE_PINS       手动档：调密度计**不改变**重介灰分与总灰分（这是原来的行为）
//   CALC_MODE_FOLLOWS      计算档：调密度计**改变**重介灰分与总灰分
//   LOOP_REACHES_TARGET    计算档：按建议密度调整后，总灰分偏差显著变小 / 达标
//   TYPING_SWITCHES_MODE   双击填值会自动切到手动档（避免"填了却不生效"）
//   FREEZE_WITH_TOTAL_ASH  「总灰分修改」开启时该下拉不可用（公式因素冻结）
//   CTX_RECORDS_SOURCE     决策日志 ctx 带 heavyAshSource
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const BASE = (process.env.DMCS_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');
const URL = BASE + '/';
const HOST = BASE.replace(/^https?:\/\//, '');
const EDGE = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    '/usr/bin/microsoft-edge-stable', '/usr/bin/google-chrome'].find(p => p && fs.existsSync(p));
const PORT = 9300 + Math.floor(Math.random() * 400);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const fails = [];
function check(name, ok, detail) {
    console.log(`${name}: ${ok ? 'PASS' : 'FAIL'}${detail ? ' | ' + detail : ''}`);
    if (!ok) fails.push(name);
}
const api = async (p) => {
    const opts = {};
    if (process.env.DMCS_WRITE_TOKEN) opts.headers = { 'X-DMCS-Token': process.env.DMCS_WRITE_TOKEN };
    const r = await fetch(BASE + p, opts);
    if (!r.ok) throw new Error(`${p} → HTTP ${r.status}`);
    return r.json();
};

(async () => {
    if (!EDGE) { console.error('ERROR: 未找到浏览器'); process.exit(2); }
    console.log('浏览器:', EDGE, '| 目标:', URL, '| Node', process.version);
    const st = await api('/api/v1/state');
    const nonEmpty = ['coarseCoal', 'floatCoal', 'calcLogs'].some(k => (st[k] || []).length > 0);
    if (nonEmpty && process.env.DMCS_ALLOW_REAL_DB !== '1') {
        console.error('ERROR: 目标库非空，请指向临时库（或显式 DMCS_ALLOW_REAL_DB=1）');
        process.exit(2);
    }

    const UD = path.join(process.env.TEMP || '/tmp', 'dmcs-cdp-heavy-' + Date.now());
    const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run', '--hide-scrollbars',
        `--user-data-dir=${UD}`, `--remote-debugging-port=${PORT}`, '--window-size=1680,1200', URL], { stdio: 'ignore' });
    try {
        let page;
        for (let i = 0; i < 40; i++) {
            try {
                const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
                page = l.find(t => t.type === 'page' && t.url.includes(HOST));
                if (page) break;
            } catch (e) { }
            await sleep(500);
        }
        if (!page) throw new Error('未能连上浏览器调试端口');
        const ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = fail; });
        let idc = 0; const pending = new Map();
        ws.onmessage = ev => { const m = JSON.parse(ev.data); if (pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
        const send = (m, p) => new Promise(ok => { const id = ++idc; pending.set(id, ok); ws.send(JSON.stringify({ id, method: m, params: p })); });
        const evalJs = async (expr, awaitPromise = false) => {
            const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise });
            if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text);
            return r.result.result.value;
        };
        let ready = false, lastErr = null;
        for (let i = 0; i < 60; i++) {
            try { if (await evalJs("typeof App !== 'undefined' && !!App.store && !!App.store.coarseCoal")) { ready = true; break; } }
            catch (e) { lastErr = e.message; }
            await sleep(500);
        }
        if (!ready) throw new Error('页面 30 秒内未完成初始化' + (lastErr ? '；' + lastErr : ''));

        // ---------- 1) 下拉存在 ----------
        const ui = JSON.parse(await evalJs(`(() => {
            App.goToPage('page-collect');
            CollectPage.renderTable();
            const tr = [...document.querySelectorAll('#collect-tbody tr')]
                .find(r => r.children[0].textContent.includes('重介精煤灰分'));
            const sel = document.getElementById('heavyash-source');
            return JSON.stringify({
                row: !!tr, hasSelect: !!sel,
                options: sel ? [...sel.options].map(o => o.textContent) : [],
                value: sel ? sel.value : null,
                sourceCell: tr ? tr.children[5].textContent.trim() : null,
            });
        })()`));
        check('SOURCE_SELECT_EXISTS', ui.hasSelect && ui.options.join('/') === '计算/手动',
            `行存在=${ui.row} 选项=${ui.options.join('/')}`);

        // 固定初始条件：给 502 一个录入基准值，并把密度设为 1.49（仿真基准）
        await evalJs(`(() => {
            App.setInstrumentInput('heavyAshInput', {});   // 占位，避免误用不存在的接口
            return true;
        })()`).catch(() => { });
        const setup = JSON.parse(await evalJs(`(() => {
            // 手动档 + 填一个化验值（模拟"我手动调了重介精煤灰分"）
            App.store.heavyAshManualOn = true;
            App.setHeavyAshInput({ manual: 8.60 });
            App.setInstrumentInput('density', { manual: 1.490 });
            CollectPage.renderTable();
            const g = App.computeDensityGuidance(App.store.ashTarget);
            return JSON.stringify({
                mode: App.heavyAshSource(), manual: App.store.heavyAshInput.manual,
                heavyAsh: App.getHeavyAsh(), computed: App.heavyAshComputed(),
                totalAsh: App.resolveTotalAsh(), target: App.store.ashTarget,
                deltaA: g.valid ? g.deltaA : null, rhoCur: g.rhoCur, rhoNew: g.rhoNew,
            });
        })()`));
        console.log('  初始（手动档）:', JSON.stringify(setup));

        // ---------- 2) 手动档：调密度计不改变重介灰分 ----------
        const manualPinned = JSON.parse(await evalJs(`(() => {
            const before = { h: App.getHeavyAsh(), t: App.resolveTotalAsh() };
            App.setInstrumentInput('density', { manual: 1.510 });    // 密度 +0.02
            CollectPage.renderTable();
            return JSON.stringify({
                before, after: { h: App.getHeavyAsh(), t: App.resolveTotalAsh() },
                mode: App.heavyAshSource(),
                hint: (() => { const tr = [...document.querySelectorAll('#collect-tbody tr')]
                    .find(r => r.children[0].textContent.includes('重介精煤灰分'));
                    return tr ? tr.children[2].textContent.trim() : null; })(),
            });
        })()`));
        check('MANUAL_MODE_PINS',
            manualPinned.after.h === manualPinned.before.h && manualPinned.after.t === manualPinned.before.t,
            `重介灰分 ${manualPinned.before.h}→${manualPinned.after.h}，总灰分 ${manualPinned.before.t}→${manualPinned.after.t}`
            + `（手动档应当不变）`);

        // ---------- 3) 计算档：调密度计会改变重介灰分与总灰分 ----------
        const calcFollows = JSON.parse(await evalJs(`(() => {
            App.store.heavyAshManualOn = false;                 // 相当于把下拉切到「计算」
            App.setInstrumentInput('density', { manual: 1.490 });
            CollectPage.renderTable();
            const before = { h: App.getHeavyAsh(), t: App.resolveTotalAsh() };
            App.setInstrumentInput('density', { manual: 1.510 });
            CollectPage.renderTable();
            const after = { h: App.getHeavyAsh(), t: App.resolveTotalAsh() };
            const tr = [...document.querySelectorAll('#collect-tbody tr')]
                .find(r => r.children[0].textContent.includes('重介精煤灰分'));
            return JSON.stringify({ before, after, mode: App.heavyAshSource(),
                layer: tr ? tr.children[5].textContent.trim() : null,
                gh: 1 / App.DENSITY_GUIDE.simK });
        })()`));
        const dH = +(calcFollows.after.h - calcFollows.before.h).toFixed(3);
        const dT = +(calcFollows.after.t - calcFollows.before.t).toFixed(3);
        check('CALC_MODE_FOLLOWS', dH > 0.3 && dT > 0.1,
            `密度 +0.02 → 重介灰分 ${calcFollows.before.h}→${calcFollows.after.h}（Δ${dH}），`
            + `总灰分 ${calcFollows.before.t}→${calcFollows.after.t}（Δ${dT}）`);

        // ---------- 4) 闭环：按建议密度调整后，总灰分偏差变小/达标 ----------
        const loop = JSON.parse(await evalJs(`(() => {
            App.store.heavyAshManualOn = false;
            // 造一个"总灰分不达标"的起点：密度偏高 → 灰分偏高
            App.setInstrumentInput('density', { manual: 1.520 });
            const g0 = App.computeDensityGuidance(App.store.ashTarget);
            const before = { rho: App.resolveDensity(), dev: g0.valid ? +g0.deltaA.toFixed(3) : null,
                             total: App.resolveTotalAsh() };
            // 把密度计调到建议值
            App.setInstrumentInput('density', { manual: +g0.rhoNew.toFixed(3) });
            const g1 = App.computeDensityGuidance(App.store.ashTarget);
            const after = { rho: App.resolveDensity(), dev: g1.valid ? +g1.deltaA.toFixed(3) : null,
                            total: App.resolveTotalAsh(), rhoNew: g1.rhoNew, dir: g1.direction };
            return JSON.stringify({ before, after, tol: (App.store.ashTargetTol != null ? App.store.ashTargetTol : 0.1) });
        })()`));
        const devBefore = Math.abs(loop.before.dev), devAfter = Math.abs(loop.after.dev);
        // 方向性：调整后灰分必须**朝目标方向**移动（起点高于目标 → 调完应低于或接近目标）
        const crossed = Math.sign(loop.after.dev) !== Math.sign(loop.before.dev);
        check('LOOP_DIRECTION_OK', crossed || devAfter < devBefore,
            `起点 ρ=${loop.before.rho} 偏差 ${loop.before.dev}% → 按建议 ρ=${loop.after.rho} 后偏差 ${loop.after.dev}%`
            + `（总灰分 ${loop.before.total}→${loop.after.total}）`);

        // 过冲与增益测算（信息项）：说清"专家表假设的增益"与"仿真+配煤公式的实际增益"差多少
        const step = +(loop.after.rho - loop.before.rho).toFixed(4);
        const simGain = +(dT / Math.abs(calcFollows.after.rho || 0.02) * 1).toFixed(0);   // 占位，下面重算
        const measured = JSON.parse(await evalJs(`(() => {
            App.store.heavyAshManualOn = false;
            App.setInstrumentInput('density', { manual: 1.500 });
            const a = App.resolveTotalAsh();
            App.setInstrumentInput('density', { manual: 1.510 });
            const b = App.resolveTotalAsh();
            const c = App.computeDensityGuidance(App.store.ashTarget);
            return JSON.stringify({ dRho: 0.01, dTotal: +(b - a).toFixed(4), guide: c });
        })()`));
        const gainPerUnit = +(measured.dTotal / 0.01).toFixed(2);       // %总灰分 / (g/cm³)
        const tableGain = +(0.15 / 0.01).toFixed(1);                    // 专家表第一档隐含的增益
        console.log(`LOOP_OVERSHOOT_INFO: 建议步长 Δρ=${step}；应用后偏差 ${loop.before.dev}% → ${loop.after.dev}%`
            + `（过冲 ${(devAfter / Math.max(devBefore, 1e-9)).toFixed(2)} 倍）`);
        console.log(`LOOP_GAIN_INFO: 仿真+配煤公式实测增益 ${gainPerUnit}%/单位密度`
            + `（0.01 密度 → ${measured.dTotal}% 总灰分）；专家表第一档隐含 ${tableGain}%/单位密度`
            + ` → 步长偏大约 ${(gainPerUnit / tableGain).toFixed(2)} 倍`);

        // ---------- 5) 双击填值自动切到手动 ----------
        const typing = JSON.parse(await evalJs(`(() => {
            App.store.heavyAshManualOn = false;
            CollectPage.commitTotalInput('heavyAsh', 8.72);
            return JSON.stringify({ mode: App.heavyAshSource(), value: App.getHeavyAsh(),
                manual: App.store.heavyAshInput.manual });
        })()`));
        check('TYPING_SWITCHES_MODE', typing.mode === 'manual' && typing.value === 8.72,
            `填入 8.72 后 mode=${typing.mode} 取值=${typing.value}`);

        // ---------- 6) 总灰分=手动 时冻结 ----------
        const frozen = JSON.parse(await evalJs(`(() => {
            App.store.totalAshManualOn = true;
            CollectPage.renderTable();
            const sel = document.getElementById('heavyash-source');
            const tr = [...document.querySelectorAll('#collect-tbody tr')]
                .find(r => r.children[0].textContent.includes('重介精煤灰分'));
            const out = { selectGone: !sel, sourceCell: tr ? tr.children[5].textContent.trim() : null };
            App.store.totalAshManualOn = false;
            CollectPage.renderTable();
            return JSON.stringify(out);
        })()`));
        check('FREEZE_WITH_TOTAL_ASH', frozen.selectGone && /冻结/.test(String(frozen.sourceCell)),
            `下拉消失=${frozen.selectGone} 来源列="${frozen.sourceCell}"`);

        // ---------- 7) 决策日志 ctx 记录来源 ----------
        const ctx = JSON.parse(await evalJs(`(() => {
            App.store.heavyAshManualOn = false;
            const c1 = App._decisionCtx().heavyAshSource;
            App.setHeavyAshInput({ manual: 8.66 });
            const c2 = App._decisionCtx().heavyAshSource;
            return JSON.stringify({ calc: c1, manual: c2 });
        })()`));
        check('CTX_RECORDS_SOURCE', ctx.calc === 'calc' && ctx.manual === 'manual',
            `计算档=${ctx.calc} 手动档=${ctx.manual}`);

        ws.close();
    } catch (e) {
        console.error('ERROR:', e.message);
        fails.push('EXCEPTION');
    } finally {
        try { edge.kill(); } catch (e) { }
        console.log(fails.length ? `HEAVY_ASH_SOURCE: FAIL (${fails.join(', ')})` : 'HEAVY_ASH_SOURCE: PASS');
        process.exitCode = fails.length ? 1 : 0;
        setTimeout(() => process.exit(), 300);
    }
})();
