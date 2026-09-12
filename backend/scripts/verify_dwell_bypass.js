// 验证「新化验到了，建议不被最短驻留挡住」——复现用户 2026-09-12 实测到的缺陷。
//
// 用户原话：
//   「密度计 1.532，重介精煤灰分 7.8346，总灰分 8.4975 → 灰分达标，建议密度 1.532，没问题；
//     但把重介精煤灰分调到 8.3，总灰分 8.9，此时不达标，为什么建议密度还是 1.532 」
//
// 病根（两处）：
//   ① densityGuardState 的驻留分支只看"距上次改密度多久"，不看"这段时间里有没有来新化验"。
//      驻留的本意是"等过程到位 + 等新化验"，新化验已经到了却还被挡住 → 建议停在旧值。
//   ② 写密度时**同值写入也算一次"动作"**（自动执行/操作员把密度写成与当前相同的值），
//      于是无谓地起了一个 30 分钟驻留窗口。
//   附带：卡片副标题在 hold 时也显示"达标保持"，让人分不清"已达标"和"被守卫挡住"。
//
// 本脚本的断言：
//   SETUP_TARGET_OK               初始 1.532 / 重介 7.8346 → 达标、建议=当前值
//   NOOP_WRITE_NO_DWELL           同值写密度不arm驻留（不算"动过密度"）
//   NEW_LAB_BYPASSES_DWELL        用户场景：新化验 8.3（总灰分≈8.9）→ 必须给出降密一步
//   CARD_SHOWS_STEP               卡片主数字=建议值、副标题显示"降密"
//   REAL_MOVE_ARMS_LATCH          真改密度后，同一份化验再算 → 保持（同一份化验只动作一次）
//   DWELL_HOLDS_WITHOUT_NEW_DATA  没有新数据时驻留仍然生效（P0③ 不被削弱）
//   HOLD_TEXT_DISTINCT            hold 时卡片写"保持（等新化验/新数据）"，不再冒充"达标保持"
//   NEW_LAB_AFTER_MOVE_BYPASSES   距上次调密仅 X 分钟，但已有新化验 → 放行给下一步
//   BYPASS_REASON_TRANSPARENT     放行时 reason 说明"距上次调密 X 分钟、已收到新化验"
//   HEAVY_SCHEME_BYPASS           重介版同样放行（驱动时间戳=重介灰分手动录入时刻）
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

    const UD = path.join(process.env.TEMP || '/tmp', 'dmcs-cdp-dwell-' + Date.now());
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
        if (!ready) throw new Error('页面 30 秒内未完成初始化（等 App.__ready）' + (lastErr ? '；' + lastErr : ''));

        // ---------- 0) 标定：本库种子量下"重介灰分 → 总灰分"的权重 + 达标点 ----------
        // 用户当时的跳变是 重介 7.8346 → 8.3（+0.4654），总灰分 8.4975 → 8.9（≈+0.40）。
        // 这里不硬编用户的具体数值，而是用当前库的量测出权重，再把"达标点"与"+0.4654 的新化验点"算出来，
        // 让断言在任何数据集上都成立（偏差口径与用户完全一致）。
        const cal = JSON.parse(await evalJs(`(() => {
            App.store.guideScheme = 'total';
            App.store.totalAshManualOn = false;
            App.setHeavyAshInput({ manual: 8.000 });
            const t1 = App.resolveTotalAsh();
            App.setHeavyAshInput({ manual: 9.000 });
            const t2 = App.resolveTotalAsh();
            const w = +(t2 - t1).toFixed(4);                       // 重介灰分对总灰分的权重
            const h0 = +(8 + (App.store.ashTarget - t1) / w).toFixed(4);   // 使总灰分 = 目标的达标点
            return JSON.stringify({ w: w, h0: h0, target: App.store.ashTarget,
                                    t1: t1, t2: t2, hNew: +(h0 + 0.4654).toFixed(4) });
        })()`));
        console.log('  标定:', JSON.stringify(cal));

        // ---------- 1) 复现用户场景：达标 + 建议=当前值 ----------
        const setup = JSON.parse(await evalJs(`(() => {
            App.store.guideScheme = 'total';
            App.store.totalAshManualOn = false;          // 总灰分走公式（用户当时就是没开"总灰分修改"）
            App.store.heavyAshManualOn = true;
            App.setInstrumentInput('density', { manual: 1.532 });
            App.setHeavyAshInput({ manual: ${cal.h0} });
            App.store.densityLastMoveAt = 0;             // 上面两次录入不算"调密度"
            App.store.densityActionLatch = null;
            const g = App.computeDensityGuidance(App.store.ashTarget);
            return JSON.stringify({
                target: App.store.ashTarget, total: App.resolveTotalAsh(),
                heavy: App.getHeavyAsh(), rhoCur: g.rhoCur,
                deltaA: g.deltaA, hold: g.hold, rhoNew: g.rhoNew, dir: g.direction, valid: g.valid,
            });
        })()`));
        console.log('  初始（用户口径）:', JSON.stringify(setup));
        check('SETUP_TARGET_OK',
            setup.valid === true && Math.abs(setup.deltaA) <= 0.05 && setup.hold === false
            && Math.abs(setup.rhoNew - 1.532) < 1e-9 && setup.dir === 'stable',
            `总灰分=${setup.total}% 重介=${setup.heavy}% ΔA=${setup.deltaA} hold=${setup.hold} 建议=${setup.rhoNew}`);

        // ---------- 2) 同值写密度不arm驻留 ----------
        const noop = JSON.parse(await evalJs(`(() => {
            App.setInstrumentInput('density', { manual: 1.532, autoExec: true });   // 把建议值写回（值没变）
            return JSON.stringify({
                lastMove: App.store.densityLastMoveAt || 0,
                latch: App.store.densityActionLatch ? App.store.densityActionLatch.key : null,
                rho: App.resolveDensity(),
            });
        })()`));
        check('NOOP_WRITE_NO_DWELL', !noop.lastMove && !noop.latch,
            `lastMoveAt=${noop.lastMove} latch=${noop.latch}（同值写入不该算"动过密度"）`);

        // ---------- 3) 用户投诉的那一步：录入新化验 → 必须给降密建议 ----------
        const newLab = JSON.parse(await evalJs(`(() => {
            App.setHeavyAshInput({ manual: ${cal.hNew} });   // 用户那次的重介灰分跳变（+0.4654）
            const g = App.computeDensityGuidance(App.store.ashTarget);
            OverviewPage.refresh();
            const subs = [...document.querySelectorAll('[id^="density-sub-"]')].map(e => e.textContent);
            const mains = [...document.querySelectorAll('[id^="density-"]')]
                .filter(e => /^density-(sub-)?(total|float|heavy)/.test(e.id) && !e.id.includes('sub'))
                .map(e => e.id + '=' + e.textContent);
            return JSON.stringify({
                total: App.resolveTotalAsh(), deltaA: g.deltaA, hold: g.hold, reason: g.reason || '',
                dir: g.direction, deltaRho: g.deltaRho, rhoNew: g.rhoNew, subs: subs, mains: mains,
            });
        })()`));
        console.log('  新化验 8.3:', JSON.stringify({ total: newLab.total, deltaA: newLab.deltaA, rhoNew: newLab.rhoNew, dir: newLab.dir }));
        check('NEW_LAB_BYPASSES_DWELL',
            newLab.hold === false && newLab.dir === 'down'
            && Math.abs(newLab.deltaRho + 0.02) < 1e-9 && Math.abs(newLab.rhoNew - 1.512) < 1e-9
            && newLab.total > 8.7 && newLab.total < 9.1,
            `总灰分=${newLab.total}% ΔA=${newLab.deltaA} hold=${newLab.hold} 建议=${newLab.rhoNew} (期望 1.512)`);
        check('CARD_SHOWS_STEP',
            newLab.subs.some(s => s.includes('降密')) && !newLab.subs.some(s => s.includes('达标保持'))
            && newLab.mains.some(m => m.includes('1.512')),
            `副标题=${JSON.stringify(newLab.subs)} 主数字=${JSON.stringify(newLab.mains)}`);

        // ---------- 4) 真改密度 → 闩锁与驻留（P0②③ 不被削弱）----------
        const latch = JSON.parse(await evalJs(`(() => {
            App.setInstrumentInput('density', { manual: 1.512, autoExec: true });   // 真改：1.532 → 1.512
            const gSame = App.computeDensityGuidance(App.store.ashTarget);           // 同一份化验（8.3）
            const out = {
                lastMove: !!App.store.densityLastMoveAt,
                latchKey: App.store.densityActionLatch ? App.store.densityActionLatch.key : null,
                sameLabHold: gSame.hold, sameLabReason: gSame.reason || '', sameLabRho: gSame.rhoNew,
            };
            // 清掉闩锁，只留"刚调过密度"，验证驻留本身仍然生效
            App.store.densityActionLatch = null;
            const gDwell = App.computeDensityGuidance(App.store.ashTarget);
            out.dwellHold = gDwell.hold; out.dwellReason = gDwell.reason || ''; out.dwellRho = gDwell.rhoNew;
            OverviewPage.refresh();
            out.subsHold = [...document.querySelectorAll('[id^="density-sub-"]')].map(e => e.textContent);
            return JSON.stringify(out);
        })()`));
        check('REAL_MOVE_ARMS_LATCH',
            latch.lastMove === true && !!latch.latchKey && latch.sameLabHold === true
            && latch.sameLabReason.includes('同一份化验只动作一次') && Math.abs(latch.sameLabRho - 1.512) < 1e-9,
            `lastMove=${latch.lastMove} hold=${latch.sameLabHold}｜${latch.sameLabReason.slice(0, 50)}`);
        check('DWELL_HOLDS_WITHOUT_NEW_DATA',
            latch.dwellHold === true && latch.dwellReason.includes('还没有新的化验')
            && !latch.dwellReason.includes('距上次调密')      // 没新化验 → 不得出现"放行"说明
            && Math.abs(latch.dwellRho - 1.512) < 1e-9,
            `hold=${latch.dwellHold} 建议=${latch.dwellRho}｜${latch.dwellReason}`);
        check('HOLD_TEXT_DISTINCT',
            latch.subsHold.some(s => s.includes('等新化验')) && !latch.subsHold.some(s => s.includes('达标保持')),
            `副标题=${JSON.stringify(latch.subsHold)}`);

        // ---------- 5) 调密之后来了新化验 → 放行（本次修复的核心）----------
        const afterMove = JSON.parse(await evalJs(`(() => {
            App.setHeavyAshInput({ manual: ${+(cal.hNew + 0.1).toFixed(4)} });      // 又一份新化验（距调密 0 分钟）
            const g = App.computeDensityGuidance(App.store.ashTarget);
            const out = { hold: g.hold, dir: g.direction, rhoNew: g.rhoNew, reason: g.reason || '', deltaA: g.deltaA };
            App.setInstrumentInput('density', { manual: g.rhoNew, autoExec: true }); // 执行这一步
            const g2 = App.computeDensityGuidance(App.store.ashTarget);
            out.againHold = g2.hold; out.againRho = g2.rhoNew; out.againReason = g2.reason || '';
            return JSON.stringify(out);
        })()`));
        check('NEW_LAB_AFTER_MOVE_BYPASSES',
            afterMove.hold === false && afterMove.dir === 'down' && afterMove.rhoNew < 1.512,
            `hold=${afterMove.hold} ΔA=${afterMove.deltaA} 建议=${afterMove.rhoNew}（应 < 1.512）`);
        check('BYPASS_REASON_TRANSPARENT',
            afterMove.reason.includes('距上次调密') && afterMove.reason.includes('按新数据给下一步')
            && afterMove.reason.includes('调密前取的煤样'),
            `reason=${afterMove.reason}`);
        check('LATCH_STILL_ONE_ACTION_PER_SAMPLE',
            afterMove.againHold === true && afterMove.againReason.includes('同一份化验只动作一次'),
            `hold=${afterMove.againHold} 建议=${afterMove.againRho}｜${afterMove.againReason.slice(0, 50)}`);

        // ---------- 6) 重介精煤灰分版同样放行 ----------
        const heavySch = JSON.parse(await evalJs(`(() => {
            App.store.guideScheme = 'heavy';
            App.setInstrumentInput('density', { manual: 1.532 });
            App.store.densityLastMoveAt = 0; App.store.densityActionLatch = null;
            App.setHeavyAshInput({ manual: ${cal.h0} });
            App.store.densityLastMoveAt = 0; App.store.densityActionLatch = null;
            const g1 = App.computeDensityGuidance(App.store.ashTarget);
            App.setInstrumentInput('density', { manual: 1.512, autoExec: true });    // 先真调一次密度
            App.setHeavyAshInput({ manual: ${cal.hNew} });                          // 再录新化验
            const g2 = App.computeDensityGuidance(App.store.ashTarget);
            App.store.guideScheme = 'total';
            return JSON.stringify({
                g1Hold: g1.hold, g1Rho: g1.rhoNew, hold: g2.hold, dir: g2.direction,
                rhoNew: g2.rhoNew, deltaAHeavy: g2.deltaAHeavy, scheme: g2.scheme,
            });
        })()`));
        check('HEAVY_SCHEME_BYPASS',
            heavySch.scheme === 'heavy' && heavySch.g1Hold === false && heavySch.hold === false
            && heavySch.dir === 'down' && heavySch.rhoNew < 1.512,
            `重介偏差=${heavySch.deltaAHeavy}% hold=${heavySch.hold} 建议=${heavySch.rhoNew}`);

        ws.close();
    } catch (e) {
        console.error('ERROR:', e.message);
        fails.push('EXCEPTION');
    } finally {
        try { edge.kill(); } catch (e) { }
        console.log(fails.length ? `DWELL_BYPASS: FAIL (${fails.join(', ')})` : 'DWELL_BYPASS: PASS');
        process.exitCode = fails.length ? 1 : 0;
        setTimeout(() => process.exit(), 300);
    }
})();
