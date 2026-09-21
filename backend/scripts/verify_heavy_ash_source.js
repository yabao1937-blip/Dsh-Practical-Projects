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
            try { if (await evalJs("typeof App !== 'undefined' && App.__ready === true && !!App.store"
                    + " && typeof CollectPage !== 'undefined' && typeof OverviewPage !== 'undefined'")) { ready = true; break; } }
            catch (e) { lastErr = e.message; }
            await sleep(500);
        }
        if (!ready) throw new Error('页面 30 秒内未完成初始化（等 App.__ready）（等 App/CollectPage/OverviewPage）' + (lastErr ? '；' + lastErr : ''));

        // 本脚本会反复写密度与重介灰分（1.49/1.51/1.52/1.60、8.6/8.66/9.6/8.55 等），
        // 之前跑完不恢复 —— 一旦被 allowReal 指到真实库，测试值就留在现场
        // （2026-09-19 真实发生过：密度被写成 1.485）。这里先存档，末尾原样恢复。
        const savedState = JSON.parse(await evalJs(`JSON.stringify({
            inst: App.store.instrumentInputs || {},
            heavy: App.store.heavyAshInput || {},
            heavyOn: (App.store.heavyAshManualOn === undefined) ? null : App.store.heavyAshManualOn,
            totalOn: !!App.store.totalAshManualOn,
            latch: App.store.densityActionLatch || null,
            lastMove: App.store.densityLastMoveAt || 0,
        })`));

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

        // ---------- 3) 计算档：调密度计**不得**改变重介灰分与总灰分（2026-09-21 新语义） ----------
        // 旧行为是"计算档 = 502在线 + 密度仿真"，调密立刻改灰分；现场确认后改为
        // 「仪表值只来自录入/测量，密度变化不得改写灰分测量值」——调密的效果必须等新化验/新数据。
        const calcPins = JSON.parse(await evalJs(`(() => {
            App.store.heavyAshManualOn = false;                 // 相当于把下拉切到「计算」
            App.setInstrumentInput('density', { manual: 1.490 });
            CollectPage.renderTable();
            const before = { h: App.getHeavyAsh(), t: App.resolveTotalAsh() };
            App.setInstrumentInput('density', { manual: 1.510 });   // +0.02
            CollectPage.renderTable();
            const after2 = { h: App.getHeavyAsh(), t: App.resolveTotalAsh() };
            App.setInstrumentInput('density', { manual: 1.600 });   // 再推到远离工作点
            const after60 = { h: App.getHeavyAsh(), t: App.resolveTotalAsh() };
            const tr = [...document.querySelectorAll('#collect-tbody tr')]
                .find(r => r.children[0].textContent.includes('重介精煤灰分'));
            return JSON.stringify({ before, after2, after60, mode: App.heavyAshSource(),
                layer: tr ? tr.children[5].textContent.trim() : null });
        })()`));
        const dH2 = +(calcPins.after2.h - calcPins.before.h).toFixed(3);
        const dT2 = +(calcPins.after2.t - calcPins.before.t).toFixed(3);
        const dH6 = +(calcPins.after60.h - calcPins.before.h).toFixed(3);
        check('DENSITY_MOVE_KEEPS_MEASUREMENT', dH2 === 0 && dT2 === 0 && dH6 === 0,
            `密度 1.490→1.510→1.600：重介灰分 ${calcPins.before.h}→${calcPins.after2.h}→${calcPins.after60.h}`
            + `（Δ${dH2}/${dH6}），总灰分 ${calcPins.before.t}→${calcPins.after2.t}→${calcPins.after60.t}（Δ${dT2}）`
            + `；档位=${calcPins.mode}`);
        check('LAYER_TEXT_NO_SIM', !String(calcPins.layer || '').includes('密度'),
            `来源列文案="${calcPins.layer}"（不应再声称含"密度仿真"）`);

        // ---------- 4) 闭环：调完密度后必须"等新测量"，不能凭空收敛 ----------
        // 新语义下：把密度调到建议值**不会**让偏差变小（不伪造反馈），而是进入"刚调过密度"的保持态；
        // 只有新的化验/在线数据到达（这里用 heavyAshInput.manualAt 更新模拟）才会给出下一步。
        const loop = JSON.parse(await evalJs(`(() => {
            App.store.heavyAshManualOn = true;
            App.setHeavyAshInput({ manual: 8.60 });                 // 一份化验：总灰分不达标
            App.setInstrumentInput('density', { manual: 1.520 });
            App.store.densityActionLatch = null; App.store.densityLastMoveAt = 0;
            const g0 = App.computeDensityGuidance(App.store.ashTarget);
            const before = { rho: App.resolveDensity(), dev: g0.valid ? +g0.deltaA.toFixed(3) : null,
                             total: App.resolveTotalAsh(), step: +(g0.rhoNew - g0.rhoCur).toFixed(3) };
            App.setInstrumentInput('density', { manual: +g0.rhoNew.toFixed(3) });   // 操作员照做（真改密度）
            const gHold = App.computeDensityGuidance(App.store.ashTarget);
            const after = { rho: App.resolveDensity(), dev: gHold.valid ? +gHold.deltaA.toFixed(3) : null,
                            total: App.resolveTotalAsh(), hold: !!gHold.hold, rhoNew: gHold.rhoNew,
                            reason: String(gHold.reason || '').slice(0, 50) };
            App.setHeavyAshInput({ manual: 8.62 });                 // 新一份化验（数值也变了）
            // 同一个同步块里 Date.now() 不会前进 → 必须显式把化验时刻推后，
            // 否则驱动键与上一条完全相同，闩锁会（正确地）判为"同一份化验"。
            App.store.heavyAshInput.manualAt = Date.now() + 60000;
            const gNew = App.computeDensityGuidance(App.store.ashTarget);
            const reopened = { hold: !!gNew.hold, step: +(gNew.rhoNew - gNew.rhoCur).toFixed(3),
                               rhoNew: +gNew.rhoNew.toFixed(3) };
            return JSON.stringify({ before, after, reopened,
                tol: (App.store.ashTargetTol != null ? App.store.ashTargetTol : 0.1) });
        })()`));
        check('LOOP_REQUIRES_MEASUREMENT',
            loop.after.total === loop.before.total && loop.after.dev === loop.before.dev
            && loop.after.hold === true && Math.abs(loop.after.rhoNew - loop.after.rho) < 1e-9
            && Math.abs(loop.before.step) > 0 && Math.abs(loop.before.step) <= 0.02001,
            `调到建议 ρ=${loop.after.rho}（本步 ${loop.before.step}）后：总灰分 ${loop.before.total}→${loop.after.total}`
            + `（未变）、偏差 ${loop.before.dev}%→${loop.after.dev}%、保持态=${loop.after.hold}`
            + `｜${loop.after.reason}`);
        check('NEW_MEASUREMENT_REOPENS_ADVICE',
            loop.reopened.hold === false && Math.abs(loop.reopened.step) > 0
            && Math.abs(loop.reopened.step) <= 0.02001,
            `录入新化验后：保持=${loop.reopened.hold}，建议 ρ=${loop.reopened.rhoNew}（本步 ${loop.reopened.step}）`);

        // 信息项：新语义下"密度→灰分"的实测增益恒为 0（不再有仿真增益可测）
        const measured = JSON.parse(await evalJs(`(() => {
            App.store.densityActionLatch = null; App.store.densityLastMoveAt = 0;
            App.store.heavyAshManualOn = false;
            App.setInstrumentInput('density', { manual: 1.500 });
            const a = App.resolveTotalAsh();
            App.setInstrumentInput('density', { manual: 1.510 });
            const b = App.resolveTotalAsh();
            return JSON.stringify({ dRho: 0.01, dTotal: +((b == null || a == null) ? 0 : (b - a)).toFixed(4) });
        })()`));
        const gainPerUnit = +(measured.dTotal / 0.01).toFixed(2);       // %总灰分 / (g/cm³)
        const tableGain = +((await evalJs("App.EXPERT_ADJUST.gain")) || 15);   // 现场确认 15%/单位、封顶 0.03
        console.log(`LOOP_MEASUREMENT_POLICY_INFO: 密度 +0.01 → 总灰分变化 ${measured.dTotal}%`
            + `（实测增益 ${gainPerUnit}%/单位密度）；专家表隐含 ${tableGain}%/单位密度。`
            + `新语义下密度不再直接改写灰分读数，增益只能由真实调密+新化验配对数据估计。`);

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

        // ---------- 8) P0 守卫：棘轮已停（同一份化验只动作一次） ----------
        const ratchet = JSON.parse(await evalJs(`(() => {
            App.store.densityActionLatch = null; App.store.densityLastMoveAt = 0;
            App.store.heavyAshManualOn = true;
            App.setHeavyAshInput({ manual: 8.60 });          // 一份化验，之后不再更新
            App.setInstrumentInput('density', { manual: 1.520 });
            const g1 = App.computeDensityGuidance(App.store.ashTarget);
            const first = { advice: +(g1.rhoNew - g1.rhoCur).toFixed(3), rhoNew: +g1.rhoNew.toFixed(3),
                            stepwise: !!g1.stepwise, steps: g1.steps, full: g1.rhoTargetFull };
            App.setInstrumentInput('density', { manual: +g1.rhoNew.toFixed(3) });   // 操作员照做
            const g2 = App.computeDensityGuidance(App.store.ashTarget);
            return JSON.stringify({ first, second: { direction: g2.direction,
                advice: +(g2.rhoNew - g2.rhoCur).toFixed(3), reason: g2.reason.slice(0, 60) } });
        })()`));
        check('RATCHET_STOPPED',
            Math.abs(ratchet.first.advice) <= 0.02001 && ratchet.second.direction === 'stable'
            && ratchet.second.advice === 0,
            `第一步建议 ${ratchet.first.advice}（逐步=${ratchet.first.stepwise}，完整目标 ${ratchet.first.full}，`
            + `共 ${ratchet.first.steps} 步）；照做后再算 → ${ratchet.second.direction}／${ratchet.second.advice}`
            + `｜${ratchet.second.reason}`);

        // ---------- 9) P0 守卫：法则与逐步限幅（新语义下"同一份测量 → 同一条建议"） ----------
        // 旧版这里靠仿真让偏差逐步收敛；新语义下密度不再改写灰分，所以正确的断言是：
        //   ① 一次发布的步长 ≤ maxStep(0.02)、且等于 min(|ΔA|/15, 0.03) 被限幅后的值；
        //   ② 完整修正量与"共几步"仍照实给出（供操作员判断要走多远）；
        //   ③ 移动密度后重复计算，同一条测量给出的建议不变（不因密度的改变而"自我感觉好转"）。
        const conv = JSON.parse(await evalJs(`(() => {
            App.store.heavyAshManualOn = true;
            App.setHeavyAshInput({ manual: 8.60 });
            App.setInstrumentInput('density', { manual: 1.520 });
            App.store.densityActionLatch = null; App.store.densityLastMoveAt = 0;
            const g1 = App.computeDensityGuidance(App.store.ashTarget);
            const s1 = { rho: +App.resolveDensity().toFixed(3), dA: +g1.deltaA.toFixed(3),
                         advice: +(g1.rhoNew - g1.rhoCur).toFixed(3), full: g1.deltaRhoFull,
                         target: g1.rhoTargetFull, steps: g1.steps, stepwise: !!g1.stepwise,
                         total: App.resolveTotalAsh() };
            App.setInstrumentInput('density', { manual: +g1.rhoNew.toFixed(3) });   // 照做一步
            App.store.densityActionLatch = null; App.store.densityLastMoveAt = 0;   // 模拟"新数据到达"
            const g2 = App.computeDensityGuidance(App.store.ashTarget);
            const s2 = { rho: +App.resolveDensity().toFixed(3), dA: +g2.deltaA.toFixed(3),
                         advice: +(g2.rhoNew - g2.rhoCur).toFixed(3), total: App.resolveTotalAsh() };
            return JSON.stringify({ s1, s2, tol: App.store.ashTargetTol, maxStep: App.DENSITY_GUIDE.maxStep,
                                    gain: App.EXPERT_ADJUST.gain, cap: App.EXPERT_ADJUST.cap });
        })()`));
        const expectStep = -Math.sign(conv.s1.dA) * Math.min(Math.abs(conv.s1.dA) / conv.gain, conv.cap, conv.maxStep);
        check('STEPWISE_LAW',
            Math.abs(conv.s1.advice - (+expectStep.toFixed(3))) < 5e-4
            && conv.s1.target != null && conv.s1.steps >= 1,
            `ΔA=${conv.s1.dA}% → 本步 ${conv.s1.advice}（期望 ${(+expectStep.toFixed(3))}＝min(|ΔA|/${conv.gain}, ${conv.cap}, `
            + `maxStep ${conv.maxStep})）；完整修正 ${conv.s1.full} → 目标 ${conv.s1.target}，共 ${conv.s1.steps} 步`);
        check('SAME_MEASUREMENT_SAME_ADVICE',
            conv.s2.total === conv.s1.total && conv.s2.dA === conv.s1.dA
            && Math.abs(conv.s2.advice - conv.s1.advice) < 1e-9,
            `移动密度 ${conv.s1.rho}→${conv.s2.rho} 后（测量未变）：ΔA ${conv.s1.dA}→${conv.s2.dA}，`
            + `建议 ${conv.s1.advice}→${conv.s2.advice}（不因密度改变而"自我好转"）`);

        // ---------- 10) P0 守卫：任何密度下都不得凭空生成灰分偏移（新语义） ----------
        // 旧版验证"门控 + 限幅"；现在没有仿真，正确断言是：无论密度层是什么、密度多远，
        // 502 读数都必须等于测量基准本身。
        const gated = JSON.parse(await evalJs(`(() => {
            const realLayer = App.instrumentLayer;
            const base = App.latestBeltAsh('502') != null ? App.latestBeltAsh('502') : App.INSTRUMENT_DEFAULT.ash_502;
            App.instrumentLayer = () => '默认(仪表)';
            const heavyDefaultLayer = App.resolveInstrument('ash_502');
            App.instrumentLayer = () => '手动';
            App.setInstrumentInput('density', { manual: 1.60 });       // 远离工作点
            const heavyFar = App.resolveInstrument('ash_502');
            App.instrumentLayer = realLayer;
            return JSON.stringify({ base, heavyDefaultLayer, heavyFar, rho: 1.60,
                                    delta: +(heavyFar - base).toFixed(4) });
        })()`));
        check('NO_SIM_ON_DEFAULT_LAYER', Math.abs(gated.heavyDefaultLayer - gated.base) < 1e-6,
            `基准=${gated.base}｜层=默认(仪表) 时 502 读数=${gated.heavyDefaultLayer}（应等于基准）`);
        check('NO_SIM_AT_ANY_DENSITY', Math.abs(gated.delta) < 1e-9,
            `ρ=1.60（远离工作点）时 502 读数与基准之差 ${gated.delta}（必须为 0：不得线性外推）`);

        // ---------- 12) 建议密度（推测值）：详情弹窗里显示一步到位的终点 ----------
        // 注意：删掉仿真后，计算档的总灰分是 502/501 实测（本库 8.5488）→ 落在容差内会走"达标早退"，
        // rhoPredict 为 null。所以这里必须自己造一个**真实的**偏差（手动化验值），不能靠仿真。
        const pred = JSON.parse(await evalJs(`(() => {
            App.store.heavyAshManualOn = true;
            App.setHeavyAshInput({ manual: 8.55 });                  // 造一个 ≈+0.44% 的真实偏差
            App.setInstrumentInput('density', { manual: 1.520 });
            App.store.densityActionLatch = null; App.store.densityLastMoveAt = 0;
            const g = App.computeDensityGuidance(App.store.ashTarget);
            OverviewPage.showCardDetail('total');
            const html = document.getElementById('modal-body') ? document.getElementById('modal-body').innerHTML : '';
            const shown = html.includes('建议密度(推测值)') && g.rhoPredict != null
                && html.includes(g.rhoPredict.toFixed(3));
            App.closeModal();
            return JSON.stringify({ dA: g.deltaA, rhoCur: g.rhoCur, rhoNew: g.rhoNew,
                rhoTargetFull: g.rhoTargetFull, rhoPredict: g.rhoPredict,
                deltaRhoPredict: g.deltaRhoPredict, maxStep: g.maxStep, shown,
                expect: (g.rhoPredict == null) ? null : +(g.rhoCur - g.deltaA / 15).toFixed(3) });
        })()`));
        check('PREDICT_TARGET_SHOWN',
            pred.shown === true && pred.rhoPredict != null && Math.abs(pred.rhoPredict - pred.expect) < 1e-9,
            `推测值=${pred.rhoPredict}（= ρ当前 ${pred.rhoCur} − ΔA ${pred.dA} / 15，期望 ${pred.expect}）`
            + `；法则封顶的完整修正=${pred.rhoTargetFull}；本次一步=${pred.rhoNew}（≤${pred.maxStep}）；`
            + `弹窗已显示=${pred.shown}`);

        // ---------- 13) 经验范围提示：大偏差时推测值标"超出经验范围" ----------
        const rng = JSON.parse(await evalJs(`(() => {
            App.store.heavyAshManualOn = true;
            App.setHeavyAshInput({ manual: 9.60 });          // 造大偏差（>0.45%）
            App.setInstrumentInput('density', { manual: 1.52 });
            App.store.densityActionLatch = null; App.store.densityLastMoveAt = 0;
            const g = App.computeDensityGuidance(App.store.ashTarget);
            OverviewPage.showCardDetail('total');
            const html = document.getElementById('modal-body') ? document.getElementById('modal-body').innerHTML : '';
            App.closeModal();
            const out = { dA: g.deltaA, beyond: g.predictBeyondRange, range: g.expertRangeDA,
                          warned: html.includes('超出经验范围'),
                          hasPredict: html.includes('建议密度(推测值)') };
            App.setHeavyAshInput({ manual: 8.55 });          // 小偏差
            App.store.densityActionLatch = null; App.store.densityLastMoveAt = 0;
            const g2 = App.computeDensityGuidance(App.store.ashTarget);
            OverviewPage.showCardDetail('total');
            const html2 = document.getElementById('modal-body') ? document.getElementById('modal-body').innerHTML : '';
            App.closeModal();
            out.smallDA = g2.deltaA; out.smallBeyond = g2.predictBeyondRange;
            out.smallWarned = html2.includes('超出经验范围');
            return JSON.stringify(out);
        })()`));
        check('PREDICT_RANGE_HINT',
            rng.beyond === true && rng.warned === true && rng.hasPredict === true
            && rng.smallBeyond === false && rng.smallWarned === false,
            `大偏差 ΔA=${rng.dA}%（阈值 ${rng.range}%）→ 标注=${rng.warned}；`
            + `小偏差 ΔA=${rng.smallDA}% → 标注=${rng.smallWarned}`);

        // ---------- 11) P0 守卫：占位值（手动=目标且陈旧）不给建议 ----------
        const ph = JSON.parse(await evalJs(`(() => {
            App.store.totalAshManualOn = true;
            App.store.ashInputs.totalAsh = { manual: App.store.ashTarget, manualAt: Date.now() - 48 * 3600 * 1000 };
            const g = App.computeDensityGuidance(App.store.ashTarget);
            const out = { valid: g.valid, reason: g.reason || '' };
            App.store.totalAshManualOn = false;
            App.store.ashInputs.totalAsh = { mode: 'auto', manual: null, entry: null, manualAt: null };
            return JSON.stringify(out);
        })()`));
        check('PLACEHOLDER_BLOCKED', ph.valid === false && ph.reason.includes('占位值'),
            `valid=${ph.valid}｜${ph.reason.slice(0, 60)}`);

        // 收尾：把密度层/重介灰分/开关/守卫书签恢复成跑之前的样子，并冲一次镜像
        const restored = JSON.parse(await evalJs(`(async () => {
            const S = ${JSON.stringify(savedState)};
            const st = App.store;
            st.instrumentInputs = S.inst;
            st.heavyAshInput = S.heavy;
            if (S.heavyOn === null) delete st.heavyAshManualOn; else st.heavyAshManualOn = S.heavyOn;
            st.totalAshManualOn = S.totalOn;
            st.densityActionLatch = S.latch;
            st.densityLastMoveAt = S.lastMove;
            await Api.putStateBody(JSON.stringify(st), {});
            const now = {
                inst: st.instrumentInputs || {}, heavy: st.heavyAshInput || {},
                heavyOn: (st.heavyAshManualOn === undefined) ? null : st.heavyAshManualOn,
                totalOn: !!st.totalAshManualOn,
                latch: st.densityActionLatch || null, lastMove: st.densityLastMoveAt || 0,
            };
            return JSON.stringify({ ok: JSON.stringify(now) === JSON.stringify(S), now: now });
        })()`, true));
        check('STATE_RESTORED', restored.ok === true,
            `密度层=${JSON.stringify((restored.now || {}).inst && restored.now.inst.density)}`
            + ` 重介灰分=${JSON.stringify((restored.now || {}).heavy)}`);
        await sleep(500);

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
