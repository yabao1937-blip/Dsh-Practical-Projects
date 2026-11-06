/* Current new-version density advice. This module never submits a density setting. */
window.DensityCandidatePanel = {
    currentSeq: 0,
    esc(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); },
    num(value, digits = 3) { return Number.isFinite(value) ? value.toFixed(digits) : '—'; },
    signed(value) { return Number.isFinite(value) ? (value > 0 ? '+' : '') + value.toFixed(3) : '—'; },
    time(value) { return Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',hourCycle:'h23'}) : '未记录'; },
    status(value) { return ({adjust:'一步调整',hold:'有依据保持',wait:'等待观察',unavailable:'无法给出'})[value] || '无法给出'; },
    quality(value) { return ({'non-provisional':'非暂估',provisional:'暂估',unavailable:'输入不可用'})[value] || '质量未确认'; },
    band(value) { return ({low:'低于目标',in_band:'目标范围内',high:'高于目标',unknown:'无法分层'})[value] || '无法分层'; },
    reasonText(candidate,parameters={}) {
        const codes=candidate?.reason_codes || [];
        if((codes.includes('bounded_step') || codes.includes('provisional_step')) && Number.isFinite(candidate.delta) && candidate.delta!==0){
            const provisional=codes.includes('provisional_step');
            return `${provisional?'灰分为暂估，新版采用谨慎步长':'新版按常规单步规则'}，本步${candidate.delta>0?'上调':'下调'} ${this.num(Math.abs(candidate.delta))} g/cm³，受单步上限约束。`;
        }
        return candidate?.reasons?.join('；') || '';
    },
    async get(path) {
        const response = await fetch((window.Api?.base || '/api/v1') + path, {cache:'no-store',signal:AbortSignal.timeout(30000)});
        if (!response.ok) throw new Error(response.status === 404 ? '新版建议接口暂未启用。' : '新版建议读取失败，请点击页面“刷新”重试。');
        const data = await response.json();
        if (data?.experimental !== true || data.policy !== 'density-candidate-v1') throw new Error('新版建议响应格式未确认，暂不显示数值。');
        return data;
    },
    async loadCurrent() {
        const host = document.getElementById('density-candidate-current'), panel = document.getElementById('density-candidate-panel');
        if (!host) return;
        const seq = ++this.currentSeq;
        panel.setAttribute('aria-busy','true');
        host.innerHTML = '<p class="density-trend-note">正在读取当前依据…</p>';
        try {
            const data = await this.get('/decision/experimental-density');
            if (seq !== this.currentSeq) return;
            if (data.mode !== 'current') throw new Error('新版建议响应不是当前计算，暂不显示数值。');
            this.current = data; this.renderCurrent(data);
        } catch (error) {
            if (seq !== this.currentSeq) return;
            this.current = null; host.innerHTML = `<p class="density-candidate-error">${this.esc(error.message)}</p>`;
        } finally { if (seq === this.currentSeq) panel.setAttribute('aria-busy','false'); }
    },
    renderCurrent(data) {
        const setting = data.setting || {}, candidate = data.candidate || {},
            ash = data.basis?.ash || {}, target = data.basis?.target || {}, quality = data.quality || {}, observation = data.observation || {};
        const status = ['adjust','hold','wait','unavailable'].includes(candidate.status) ? candidate.status : 'unavailable';
        const usable = ['adjust','hold','wait'].includes(status) && Number.isFinite(candidate.value);
        const reason=this.reasonText(candidate,data.parameters);
        const reasons=reason?[reason]:[];
        const notes = Array.isArray(quality.notes) ? quality.notes : [];
        const actual = {following:'当前读数接近设定（单点不代表稳定）','not-following':'实测尚未接近设定',unknown:'暂无可靠实测响应'}[observation.actual_response] || '暂无可靠实测响应';
        const reading = observation.actual_reading || {};
        const readingStatus = {stale:'过时',future:'时刻晚于计算',missing:'缺失',bad:'质量不合格',unknown:'未知'}[reading.status] || '未确认';
        const readingLine = reading.usable === true && Number.isFinite(reading.value) && Number.isFinite(reading.difference_from_setting)
            ? `实测 ${this.num(reading.value)} / 设定 ${this.num(setting.value)} g/cm³ · 偏差 ${this.signed(reading.difference_from_setting)}（跟随容差 ±${this.num(reading.tolerance)}）`
            : `实测不可用于判断（${readingStatus}） · 观测于 ${this.time(reading.observed_at)}`;
        const fullyAfter = observation.ash_fully_after_action === true ? '灰分生产窗口完整位于本次调密之后' :
            observation.ash_fully_after_action === false ? '灰分生产窗口尚未完整位于本次调密之后' : '调后完整灰分窗口尚未确认';
        const aim = Number.isFinite(candidate.rho_target) && candidate.rho_target !== candidate.value
            ? ` · 趋向 ${this.num(candidate.rho_target)}（${candidate.target_direction === 'up' ? '升' : candidate.target_direction === 'down' ? '降' : '平'}）` : '';
        const sens = data.basis?.ash_sensitivity;
        const sensTag = sens?.provisional === true && Number.isFinite(sens.distance_pp)
            ? ` · 暂估距界 ${this.num(sens.distance_pp, 2)}pp` : '';
        document.getElementById('density-candidate-current').innerHTML = `
            <div class="density-candidate-values">
                <div><h4>当前 A304 设定</h4><p class="density-candidate-number">${this.num(setting.value)} <span>g/cm³</span></p><p class="density-trend-note">观测于 ${this.esc(this.time(setting.at))}</p></div>
                <div class="density-candidate-action" data-status="${status}"><h4>建议密度（新版） · ${this.status(status)}</h4><p class="density-candidate-number">${usable ? this.num(candidate.value) : '—'} <span>${usable ? 'g/cm³' : this.status(status)}</span></p><p class="density-trend-note">${status==='wait' && usable ? `等待期间保留当前设定，不作为调整指令${this.esc(aim)}` : usable ? `相对当前设定 ${this.signed(candidate.delta)} g/cm³${this.esc(aim)}` : '缺少可用建议值，原因见下方'}</p></div>
            </div>
            <div class="density-candidate-basis">
                <div><h4>灰分依据 · ${this.quality(quality.level)}</h4><p>${this.num(ash.value,2)} % · ${this.band(data.basis?.band)}${Number.isFinite(target.min) && Number.isFinite(target.max) ? `（目标 ${this.num(target.min,2)}–${this.num(target.max,2)} %）` : ''}${this.esc(sensTag)}</p><p class="density-trend-note">生产时段 ${this.esc(this.time(ash.start))} — ${this.esc(this.time(ash.end))}</p></div>
                <div><h4>为什么这样建议</h4><p>${reasons.length ? reasons.map(r=>this.esc(r)).join('；') : '新版规则未返回明确依据，暂不作操作判断。'}</p><p class="density-trend-note">${this.esc(fullyAfter)}${observation.after_at ? ` · 最近调密后观测 ${this.esc(this.time(observation.after_at))}` : ''}</p><p class="density-trend-note">${this.esc(readingLine)}</p><p class="density-trend-note">${this.esc(actual)}</p></div>
            </div>
            <p class="density-trend-note density-candidate-step">单步上限：常规 ${this.num(data.parameters?.max_step)} / 暂估 ${this.num(data.parameters?.provisional_step)} g/cm³；工程参数，尚未验证执行收益。</p>
            <details class="density-candidate-rules"><summary>新版规则与数据来源</summary>${this.parameters(data.parameters)}${notes.length ? `<p>${notes.map(n=>this.esc(n)).join('；')}</p>` : ''}<p>${(data.limitations || []).map(v=>this.esc(v)).join('；') || '新版规则尚未经过执行效果验证。'}</p></details>
            <p class="density-candidate-timestamp">计算于 ${this.esc(this.time(data.evaluated_at))} · 建议由人工确认执行，不代表执行后效果</p>`;
    },
    parameters(parameters) {
        const labels = {max_step:'常规单步上限 g/cm³',provisional_mode:'暂估处置方式',min_coverage:'灰分有效覆盖门槛（须严格超过）',actual_follow_tolerance:'单点跟随容差 g/cm³',max_age_seconds:'实时读数最大时龄 秒',max_action_gap_seconds:'调密观测最大间隔 秒',rho_min:'密度下限 g/cm³',rho_max:'密度上限 g/cm³',regular_step:'常规单步上限',provisional_step:'暂估单步上限 g/cm³',confirmed_step:'非暂估单步上限',step:'单步上限',deadband:'灰分死区',density_min:'密度下限',density_max:'密度上限',observation_minutes:'调后观察分钟'};
        return `<dl>${Object.entries(parameters || {}).map(([key,value]) => `<div><dt>${this.esc(labels[key] || key)}</dt><dd>${this.esc(typeof value === 'object' ? JSON.stringify(value) : value)}</dd></div>`).join('')}</dl>`;
    },
};
