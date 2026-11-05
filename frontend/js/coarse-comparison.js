/* Read-only comparison: every score and method comes from audited API evidence. */
(() => {
    const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const number = (value, digits = 2) => Number.isFinite(value) ? value.toFixed(digits) : '—';
    const count = value => Number.isInteger(value) && value >= 0 ? String(value) : '—';
    const signed = value => Number.isFinite(value) ? `${value < 0?'-':'+'}${Math.abs(value).toFixed(2)}` : '—';
    const date = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}) : '待确定';
    const range = value => value?.start || value?.end ? `${value.start?escape(String(value.start).slice(0,10)):'起始日期待确定'} 至 ${value.end?escape(String(value.end).slice(0,10)):'结束日期待确定'}` : '日期待确定';
    const phases = [
        ['history_all','历史全量回算','Historical Evaluation · MAE','最终模型回算现有可评价历史，包含开发数据，不能代表未来准确率。'],
        ['history_forward','历史时序回测','TSCV · MAE','只用较早日期训练，再预测较新日期；按日期扩窗回放。'],
        ['future','冻结后未来验证','OOT · MAE','模型冻结后，使用未参与开发的新数据验证。'],
    ];
    const states = {research:'研究中',provisional:'待验收',ready:'已冻结',accepted:'已冻结',frozen:'冻结基线',missing:'证据待补充',invalid:'证据校验失败',stale:'证据已过期'};
    const comparison = {
        data:null, error:'', pending:null, lastAttempt:0, system:'3', normal:false,
        async load(force = false) {
            if (this.pending || (!force && this.lastAttempt && Date.now()-this.lastAttempt < 30000)) return this.pending;
            this.lastAttempt = Date.now();
            this.error = '';
            this.pending = (async () => {
                try {
                    if (!location.protocol.startsWith('http')) throw new Error('请通过本地服务或系统网址打开页面后读取对比。');
                    const response = await fetch((window.Api?.base || '/api/v1')+'/training/coarse-model-comparison', {method:'GET',cache:'no-store',signal:AbortSignal.timeout(15000)});
                    if (!response.ok) throw new Error(`对比服务暂不可用（HTTP ${response.status}）。`);
                    const data = await response.json();
                    if (data?.schema_version !== 1 || !data.models?.current || !data.models?.candidate) throw new Error('对比数据结构不完整，暂不展示指标。');
                    this.data = data;
                } catch (error) { this.data = null; this.error = error.message; }
                finally { this.pending = null; this.render(); }
            })();
            this.render();
            return this.pending;
        },
        metric(model, key) {
            if (['missing','invalid','stale'].includes(this.data?.status)) return {status:key==='future'?'pending':'missing',note:'等待有效且通过校验的对比证据。'};
            const parent = model?.metrics?.[key];
            // Never show the overall pool under a system-specific heading.
            const system = parent?.by_system?.[this.system];
            const baseline = {baseline_id:system?.baseline_id || parent?.baseline_id,baseline_label:system?.baseline_label || parent?.baseline_label};
            return system ? {...system,status:system.status || parent.status,note:system.note || parent.note,...baseline} : {status:key==='future'?'pending':'missing',note:key==='future'?parent?.note || '模型冻结后，使用未参与开发的新数据验证。':'尚无该系统的同池评价结果。',...baseline};
        },
        metricHTML(model, phase) {
            const [key,title,english,definition] = phase;
            const m = this.metric(model,key);
            const valid = m.status==='available' && Number.isFinite(m.mae) && m.n>0;
            const observed = m.status!=='pending' && Number.isInteger(m.eligible_n) && m.eligible_n>0;
            const pending = !valid && (m.status==='pending' || key==='future'&&!observed);
            const showCounts = valid || observed;
            // Every stage has its own eligible prediction pool; never reuse a global scope denominator.
            const total = m.eligible_n;
            const refused = Number.isInteger(total) && Number.isInteger(m.n) ? Math.max(0,total-m.n) : null;
            const coverageValue = Number.isFinite(m.coverage) ? m.coverage : Number.isInteger(total)&&total>0&&Number.isInteger(m.n) ? m.n/total : null;
            const coverage = Number.isFinite(coverageValue) ? `${number(coverageValue*100,1)}%` : '—';
            const rate = Number.isFinite(m.hit1_rate) ? `${number(m.hit1_rate*100,1)}%` : '—';
            const normal = m.normal;
            const freeze = this.data?.freeze || {};
            const unavailable = pending?'待验证':'暂无结果';
            const reasons = m.reason_counts && typeof m.reason_counts==='object' ? Object.entries(m.reason_counts).filter(([,n])=>Number.isInteger(n)&&n>0) : [];
            const timeRange = key==='future'&&!valid ? freeze.status==='accepted' || this.data?.status==='ready' ? `预计评价期：${date(freeze.sealed_at)} 至 ${date(freeze.due_at)}（北京时间）` : '封存待验收 · 未来评价日期待确定' : `评价日期：${range(m.date_range)}`;
            const secondary = [
                ['RMSE',valid&&Number.isFinite(m.rmse)?number(m.rmse):unavailable,'pp'],
                ['Bias',valid&&Number.isFinite(m.bias)?signed(m.bias):unavailable,'pp'],
                ['R²',valid?(Number.isFinite(m.r2)&&m.n>=2?number(m.r2,3):'不可计算'):observed?'不可计算':unavailable,''],
            ];
            return `<section class="coarse-comparison-metric" data-phase="${key}" aria-label="${title}">
                <h5>${title}<small lang="en">${english}</small></h5>
                <p class="coarse-comparison-time">${timeRange}</p>
                <div class="coarse-comparison-value${valid?'':' is-pending'}"><strong>${valid?number(m.mae):pending?'待验证':observed?'未能评分':'暂无结果'}</strong>${valid?'<span>pp · 灰分百分点</span>':''}</div>
                <dl class="coarse-comparison-secondary">${secondary.map(([label,value,unit])=>`<div data-score="${label}"><dt>${label}</dt><dd>${value}${valid&&Number.isFinite(m[label==='R²'?'r2':label.toLowerCase()])&&unit?` <small>${unit}</small>`:''}</dd></div>`).join('')}</dl>
                <div class="coarse-comparison-score"><span>±1 pp 达标率 <b>${valid?rate:observed?'不可计算':unavailable}</b>${valid?`（${count(m.hit1)} / ${count(m.n)}）`:''}</span><span>N = ${showCounts?count(m.n):unavailable}</span></div>
                <p class="coarse-comparison-note">${key==='future'?'已揭标签可评分覆盖率':'预测覆盖率'} ${showCounts?coverage:unavailable}${showCounts?` · 已评分 ${count(m.n)}${Number.isInteger(total)?' / '+total:''} 条${Number.isInteger(refused)?` · 未评分 ${refused} 条`:''}`:''}</p>
                <p class="coarse-comparison-note">${escape(valid?key==='history_forward'?m.training_note || (model===this.data.models.current?'冻结的现用模型对同一回测日期作对照，未逐折重新训练。':definition):definition:pending&&key==='future'?'冻结后，用未参与开发的新数据验证；目前尚无可评价新数据。':m.note || definition)}</p>
                ${key==='future'&&model===this.data.models.current&&this.system==='3'?`<p class="coarse-comparison-note">未来现用对照：${escape(m.baseline_label || '现用 EX8 生产 RUNS 口径（参数冻结）')}${m.baseline_id?' · '+escape(m.baseline_id):''}。仅未来使用现场真实 RUNS 输入；历史两阶段保留原生 legacy 输入，不回填重算。</p>`:''}
                ${reasons.length?`<details><summary>未评分原因</summary><ul>${reasons.map(([reason,n])=>`<li>${escape(reason)}：${n} 条</li>`).join('')}</ul></details>`:''}
                ${key==='history_forward'&&m.train_date_range?`<details><summary>训练日期与回测方式</summary><p class="coarse-comparison-note">训练日期：${range(m.train_date_range)}${Number.isInteger(m.fold_count)?` · ${m.fold_count} 个日期折`:''}。${escape(m.fold_description || '')}</p></details>`:''}
                ${key==='history_all'&&m.review?.n>0?`<details><summary>待核对记录单列 · ${count(m.review.n)} 条</summary><p class="coarse-comparison-note">MAE ${number(m.review.mae)} pp · ±1 pp ${number(m.review.hit1_rate*100,1)}%（${count(m.review.hit1)} / ${count(m.review.n)}）。这些记录计入回算，不作为已审计训练样本。</p></details>`:''}
                ${this.normal?`<div class="coarse-comparison-normal">常规样本补充：${valid&&Number.isFinite(normal?.mae)&&normal.n>0?`MAE ${number(normal.mae)} pp · ±1 pp ${number(normal.hit1_rate*100,1)}%（${count(normal.hit1)} / ${count(normal.n)}）`:'暂无可评价结果'}<br>常规样本按训练标签规则确定，未按预测误差删样。</div>`:''}
            </section>`;
        },
        futureComparisonHTML() {
            const usable = !['missing','invalid','stale'].includes(this.data?.status);
            const common = usable ? this.data?.future_comparison?.by_system?.[this.system] : null;
            const measured = common?.status==='available' && common.n>0;
            const observed = Number.isInteger(common?.eligible_n) && common.eligible_n>0;
            const caption = measured ? `N = ${count(common.n)} / ${count(common.eligible_n)} · ${Number.isFinite(common.coverage)?number(common.coverage*100,1)+'%':'覆盖率待确定'}` : observed ? `N = ${count(common.n)} / ${count(common.eligible_n)} · 暂无共同评分样本` : '待验证';
            const scoreHTML = (label,score) => `<section><h5>${label}</h5><p>MAE ${number(score?.mae)} pp · RMSE ${number(score?.rmse)} pp · Bias ${signed(score?.bias)} pp · R² ${Number.isFinite(score?.r2)?number(score.r2,3):'不可计算'}</p><p>±1 pp 达标率 ${number(score?.hit1_rate*100,1)}%（${count(score?.hit1)} / ${count(score?.n)}）</p></section>`;
            const allSystems = ['current','candidate'].map(key=>{
                const m = usable ? this.data?.models?.[key]?.metrics?.future : null;
                return `<span>${key==='current'?'现用':'候选'} ${Number.isFinite(m?.prediction_coverage)?number(m.prediction_coverage*100,1)+'%（'+count(m.prediction_n)+' / '+count(m.attempted_n)+'）':'待验证'}</span>`;
            }).join('');
            const commonAll = usable ? this.data?.future_comparison : null;
            const currentAll = usable ? this.data?.models?.current?.metrics?.future : null;
            const candidateAll = usable ? this.data?.models?.candidate?.metrics?.future : null;
            const otherKeys = [...new Set([...Object.keys(commonAll?.by_system || {}),...Object.keys(currentAll?.by_system || {}),...Object.keys(candidateAll?.by_system || {})])].filter(key=>key!=='2'&&key!=='3');
            const otherCounts = otherKeys.map(key=>({key,n:[commonAll?.by_system?.[key]?.eligible_n,currentAll?.by_system?.[key]?.eligible_n,candidateAll?.by_system?.[key]?.eligible_n].find(Number.isInteger)}));
            const hasLabelledFuture = [commonAll?.eligible_n,currentAll?.labelled_n,candidateAll?.labelled_n].some(n=>Number.isInteger(n)&&n>0);
            const otherTotal = (otherCounts.length || hasLabelledFuture) && otherCounts.every(item=>Number.isInteger(item.n)) ? otherCounts.reduce((sum,item)=>sum+item.n,0) : null;
            const otherRange = Number.isInteger(otherTotal) ? `其它工况已揭标签目标 ${otherTotal} 条${otherCounts.length?'（'+otherCounts.map(item=>`${item.key==='unknown'?'未知工况':escape(item.key)+' 系统'} ${item.n} 条`).join('、')+'）':''}` : '其它工况（0 / 1 / 4 系统及未知）范围：待验证';
            return `<section class="coarse-comparison-future-common" aria-label="未来共同样本对比"><h4>未来共同样本对比 <span>${caption}</span></h4><p>当前上方仅显示${this.system==='3'?'三':'二'}系统。仅比较两侧都有预测且已揭标签的相同样本；单侧拒绝仍保留在各模型自身覆盖率中。</p>${measured?`<div>${scoreHTML('现用对照',common.current)}${scoreHTML('离线候选',common.candidate)}</div>`:''}<p class="coarse-comparison-all-future">全部系统的未来预测覆盖（包含尚未揭标签的尝试）：${allSystems}</p><p class="coarse-comparison-future-other">${otherRange}。这些工况不属于二/三系统标签页，仍计入全部系统的范围与覆盖；不能用默认三系统覆盖代表全部未来数据。</p></section>`;
        },
        modelHTML(key) {
            const model = this.data.models[key];
            const current = key==='current';
            const systemModel = model.by_system?.[this.system];
            const method = model.method?.by_system?.[this.system] || systemModel?.method || model.method;
            const methodSummary = method?.summary?.replace(/Gradient Boosting Regression \(GBR\)/g,'梯度提升回归（GBR，多棵决策树逐步组合）').replace(/本系统数/g,'本系统');
            const hasSystemMethod = !!(model.method?.by_system?.[this.system] || systemModel?.method);
            const details = [...new Set([...(Array.isArray(method?.details)?method.details:[]),...(!current&&Array.isArray(model.method?.details)?model.method.details:[])])];
            const name = current&&this.system==='2' ? '现用 14/15 规则' : model.name || (current?'现用 EX8':'离线候选');
            const modelStatus = !current&&model.status==='frozen'?'已冻结 · 待未来验证':states[model.status] || '状态待核对';
            return `<article class="coarse-comparison-model" data-model="${key}" aria-label="${current?'现用模型':'离线候选'}">
                <header><div><h4>${escape(name)}</h4><p>${current?'现用生产基线':'离线候选 · 尚未替换生产'}${model.version?' · '+escape(model.version):''}</p></div><span class="coarse-comparison-badge">${escape(modelStatus)}</span></header>
                ${phases.map(phase=>this.metricHTML(model,phase)).join('')}
                <section class="coarse-comparison-method"><h5>${this.system==='2'?'模型取值方法':'训练方法'}</h5><p>${escape(current&&this.system==='2'&&!hasSystemMethod?'二系统现用 14/15 经验规则，不使用 EX8 算法回算冒充生产结果。':methodSummary || '训练方法元数据待补充。')}</p>
                ${this.system==='3'&&model.training_date_range?`<p class="coarse-comparison-note">模型训练日期：${range(model.training_date_range)}</p>`:''}
                ${details.length&&!(current&&this.system==='2'&&!hasSystemMethod)?`<details><summary>查看方法与参数</summary><ul>${details.map(text=>`<li>${escape(text)}</li>`).join('')}</ul></details>`:''}</section>
            </article>`;
        },
        render() {
            const el = document.getElementById('coarse-model-comparison');
            if (!el) return;
            const heading = `<div class="coarse-comparison-heading"><div><h3 id="coarse-comparison-title">现用模型与离线候选对比</h3><p>按历史时序回测选型 · 冻结后新数据验收 · 历史全量回算作诊断 · 默认全样本</p></div><button class="btn btn-sm" data-comparison-action="refresh" ${this.pending?'disabled':''}>${this.pending?'读取中…':'刷新对比'}</button></div>`;
            if (this.error) { el.innerHTML = heading+`<div class="coarse-comparison-error" role="alert"><strong>暂时无法读取模型对比</strong><p>${escape(this.error)} 请重试；未用旧值或零值代替。</p><button class="btn" data-comparison-action="refresh">重新读取</button></div>`; this.bind(el); return; }
            if (!this.data) { el.innerHTML=heading+'<p class="coarse-comparison-loading" role="status">正在读取模型方法与历史评价证据…</p>'; this.bind(el); return; }
            const d = this.data, scope = d.scope || {};
            const available = !['missing','invalid','stale'].includes(d.status);
            const status = states[d.status] || '状态待核对';
            const disclosure = Array.isArray(scope.reasons) ? scope.reasons : scope.reasons&&typeof scope.reasons==='object' ? Object.entries(scope.reasons).map(([reason,n])=>({reason,n})) : Array.isArray(scope.exclusions) ? scope.exclusions : [];
            el.innerHTML = heading+`<div class="coarse-comparison-controls"><div class="coarse-comparison-system" role="group" aria-label="比较生产系统"><button type="button" data-system="3" aria-pressed="${this.system==='3'}">三系统 · 主比较</button><button type="button" data-system="2" aria-pressed="${this.system==='2'}">二系统 · 单列</button></div><label><input type="checkbox" data-comparison-normal ${this.normal?'checked':''}>查看常规样本补充</label></div>
                <p class="coarse-comparison-status${d.status==='ready'?'':' is-caution'}" role="status">候选${status}${d.reason?' · '+escape(d.reason):''}${this.system==='2'?' · 左侧为实际 14/15 生产规则':''}</p>
                <p class="coarse-comparison-note coarse-comparison-period-note">三个评价口径的日期范围可能重叠，并非三个互不重叠的日期段。历史回算用于诊断，时序回测用于选型，冻结后未来数据用于验收。</p>
                ${available?'':`<p class="coarse-comparison-error" role="alert">${escape(d.reason || '对比证据尚不可用')}。历史指标暂不展示，等待有效导出。</p>`}
                <div class="coarse-comparison-grid">${this.modelHTML('current')}${this.modelHTML('candidate')}</div>
                ${this.futureComparisonHTML()}
                <div class="coarse-comparison-scope"><details><summary>样本范围与未评分说明 · DB ${count(scope.total_n)} 条 / 可回算 ${count(scope.evaluable_n)} 条 / 已审计 ${count(scope.audited_n)} 条</summary><p>${escape(scope.description || '样本池信息待补充。')}</p><p>已审计训练资格 ${count(scope.audited_n)} 条；待核对 ${count(scope.review_n)} 条已单列回算；无法预测 ${count(scope.rejected_n ?? scope.excluded_n)} 条。各行 N 为实际共同评分数；覆盖不足不会按已预测处理。</p>${scope.by_system?`<p>三系统：可回算 ${count(scope.by_system['3']?.evaluable_n)} / ${count(scope.by_system['3']?.total_n)} 条，已审计 ${count(scope.by_system['3']?.audited_n)} 条，待核对 ${count(scope.by_system['3']?.review_n)} 条。二系统：可回算 ${count(scope.by_system['2']?.evaluable_n)} / ${count(scope.by_system['2']?.total_n)} 条，已审计 ${count(scope.by_system['2']?.audited_n)} 条，待核对 ${count(scope.by_system['2']?.review_n)} 条。</p>`:''}${disclosure.length?`<ul>${disclosure.map(item=>`<li>${escape(typeof item==='string'?item:item.reason || item.description || '')}${Number.isInteger(item.n)?`（${item.n} 条）`:''}</li>`).join('')}</ul>`:'<p>详细原因以审核记录为准；尚无原因明细时不推断为已评价。</p>'}<p>历史全量回算包含开发数据；TSCV 为按日期扩窗的历史开发诊断，不是留一日期验证或独立盲测。同日化验未默认提前可用。</p><p>二系统使用实际 14/15 规则作生产对照；EX8 原生模型主要用于三系统。</p></details>
                <div class="coarse-comparison-definitions"><span>MAE：平均绝对误差</span><span>RMSE：均方根误差</span><span>Bias：平均（预测 − 实测），正值高估、负值低估</span><span>R²：决定系数，无单位；可为负值</span><span>pp：灰分百分点</span><span>N：评分条数</span><span>TSCV：Time-Series Cross-Validation</span><span>OOT：Out-of-Time Validation</span></div></div>`;
            this.bind(el);
        },
        bind(el) {
            el.querySelectorAll('[data-comparison-action="refresh"]').forEach(button=>button.addEventListener('click',()=>this.load(true)));
            el.querySelectorAll('[data-system]').forEach(button=>button.addEventListener('click',()=>{this.system=button.dataset.system;this.render();el.querySelector(`[data-system="${this.system}"]`)?.focus();}));
            el.querySelector('[data-comparison-normal]')?.addEventListener('change',event=>{this.normal=event.target.checked;this.render();el.querySelector('[data-comparison-normal]')?.focus();});
        },
    };
    window.CoarseComparison = comparison;
})();
