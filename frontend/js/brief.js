/* Hourly reports are generated and frozen by the server. */
const BriefPage = {
    result: null, day: null, offset: 0, error: '', loading: false, sequence: 0, timer: null,

    escape(value) {
        return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
    },
    localTime(value, dateOnly = false) {
        if (!value || !Number.isFinite(Date.parse(value))) return '—';
        return new Intl.DateTimeFormat('sv-SE', {
            timeZone: 'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit',
            ...(dateOnly ? {} : {hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'}),
        }).format(new Date(value));
    },
    init() {
        if (this.day === null) this.day = this.localTime(new Date().toISOString(), true);
        this.generate();
        if (!this.timer) this.timer = setInterval(() => {
            if (!document.hidden && document.getElementById('page-brief')?.classList.contains('active')) this.generate(true);
        }, 15000);
    },
    async generate(quiet = false) {
        if (quiet && this.loading) return;
        const sequence = ++this.sequence;
        this.loading = true; this.error = '';
        if (!quiet) this.render();
        try {
            if (location.protocol === 'file:') throw new Error('请通过服务器地址打开，小时简报由后台生成。');
            const params = new URLSearchParams({limit:'100', offset:String(this.offset)});
            if (this.day) params.set('day', this.day);
            const response = await fetch((window.Api?.base || '/api/v1') + '/hourly-brief?' + params, {
                cache:'no-store', signal:AbortSignal.timeout(15000),
            });
            if (!response.ok) throw new Error('小时简报读取失败，请稍后刷新。');
            const data = await response.json();
            if (!Array.isArray(data.columns) || !Array.isArray(data.rows)) throw new Error('小时简报返回格式异常。');
            if (sequence !== this.sequence) return;
            this.result = data;
        } catch (error) {
            if (sequence !== this.sequence) return;
            this.error = error.name === 'TimeoutError' ? '请求超时，请检查服务器连接。' : error.message;
        } finally {
            if (sequence === this.sequence) { this.loading = false; this.render(); }
        }
        return this.result;
    },
    changeDay(value) { this.day = value; this.offset = 0; this.result = null; this.generate(); },
    pageBy(delta) { this.offset = Math.max(0, this.offset + delta * 100); this.result = null; this.generate(); },
    value(cell, key, formatted = true) {
        const value = cell?.value;
        if (value === null || value === undefined) return '—';
        if (typeof value === 'object') return Object.entries(value).map(([name, on]) =>
            name + '：' + (on === true ? '运行' : on === false ? '停止' : '—')).join(' / ');
        if (typeof value === 'number') return formatted ? value.toFixed(key.includes('density') ? 3 : 2) : value;
        return String(value);
    },
    valueHTML(cell, key) {
        const members = {systems:['401','402','A','B'], desliming:['473','474'], float_press:['A415','A416','A417','A418']}[key];
        if (!members) return `<span class="brief-value">${this.escape(this.value(cell, key))}</span>`;
        // Only the saved, validated group value determines the light; raw components are context only.
        return `<span class="brief-state-grid">${members.map(name => {
            const on = cell?.value?.[name], part = cell?.components?.[name];
            const state = on === true ? 'running' : on === false ? 'stopped' : 'unknown';
            const label = name + '：' + ({running:'运行', stopped:'停止', unknown:'无有效数据'}[state]);
            const title = [label, part?.reason || cell?.reason, part?.observed_at ? this.localTime(part.observed_at) : ''].filter(Boolean).join(' · ');
            return `<span class="brief-device-state" role="img" aria-label="${this.escape(label)}" title="${this.escape(title)}"><span aria-hidden="true">${name}</span><i class="brief-state-dot is-${state}" aria-hidden="true"></i></span>`;
        }).join('')}</span>`;
    },
    note(cell) {
        if (!cell) return '无数据';
        return cell.reason || {default:'默认值', manual:'手工补录', predicted:'模型预测', calculated:'计算值', held:'沿用前值'}[cell.status] || '';
    },
    render() {
        const box = document.getElementById('brief-content');
        if (!box) return;
        const sc = box.querySelector('.brief-scroll');
        const position = {left:sc?.scrollLeft || 0, top:sc?.scrollTop || 0};
        const data = this.result, rows = data?.rows || [], e = v => this.escape(v);
        box.innerHTML = `
            <div class="panel brief-toolbar"><div class="panel-body">
                <div class="brief-controls">
                    <label>日期（北京时间） <input aria-label="简报日期" type="date" value="${e(this.day || '')}" onchange="BriefPage.changeDay(this.value)"></label>
                    <button class="btn btn-sm" onclick="BriefPage.changeDay('')">全部记录</button>
                    <span class="brief-next">下一整点 <strong>${e(this.localTime(data?.next_hour))}</strong></span>
                    <span class="brief-muted">${this.loading ? '正在读取…' : data ? '后台自动生成 · 每小时一行' : ''}</span>
                </div>
                <p class="brief-policy">取整点前最近的已接收数据；无手工值时使用已配置的在线仪表默认值。点击时间查看来源和缺项原因。</p>
                <div class="brief-state-legend" aria-label="设备状态图例"><span><i class="brief-state-dot is-running" aria-hidden="true"></i>运行</span><span><i class="brief-state-dot is-stopped" aria-hidden="true"></i>停止</span><span><i class="brief-state-dot is-unknown" aria-hidden="true"></i>无有效数据</span></div>
                ${this.error ? `<p class="brief-error" role="alert">${e(this.error)}${data ? ' 当前保留上次读取结果。' : ''}</p>` : ''}
            </div></div>
            <div class="panel">
                <div class="panel-header"><h3>小时推测明细</h3>
                    <span class="brief-muted">共 ${data?.total ?? 0} 行 · 最新在前 · 左右滑动查看全部指标</span></div>
                <div class="panel-body" style="padding:0">
                    ${rows.length ? `<div class="brief-scroll"><table class="data-table brief-table hourly-brief-table">
                        <thead><tr><th>整点时间</th>${data.columns.map(c => `<th>${e(c.label)}${c.unit ? `<small>${e(c.unit)}</small>` : ''}</th>`).join('')}</tr></thead>
                        <tbody>${rows.map((row, index) => `<tr>
                            <td><button class="brief-hour" onclick="BriefPage.showSources(${index})" aria-label="查看 ${e(this.localTime(row.hour))} 数据来源">${e(this.localTime(row.hour).slice(0,16))}</button></td>
                            ${data.columns.map(c => {
                                const cell = row.cells[c.key], note = this.note(cell);
                                return `<td class="brief-cell-${e(cell?.status || 'missing')}" title="${e([c.label, note, cell?.source, this.localTime(cell?.observed_at)].filter(Boolean).join(' · '))}">
                                    ${this.valueHTML(cell, c.key)}
                                    ${note ? `<small class="brief-cell-note">${e(note)}</small>` : ''}</td>`;
                            }).join('')}</tr>`).join('')}</tbody>
                    </table></div>` : `<div class="brief-empty">
                        <strong>${this.loading ? '正在读取小时简报' : this.error ? '暂时无法读取简报' : '暂无小时记录'}</strong>
                        <p>${data?.next_hour ? `后台将在 ${e(this.localTime(data.next_hour))} 生成下一行。` : '等待后台任务就绪。'}</p>
                        <p class="brief-muted">从功能启用后的下一个整点开始记录；关闭网页后，服务运行期间仍会继续生成。</p>
                    </div>`}
                </div>
            </div>
            ${(data?.total || 0) > 100 ? `<div class="brief-pagination">
                <button class="btn" ${this.offset === 0 ? 'disabled' : ''} onclick="BriefPage.pageBy(-1)">上一页</button>
                <span>第 ${Math.floor(this.offset / 100) + 1} 页</span>
                <button class="btn" ${this.offset + 100 >= data.total ? 'disabled' : ''} onclick="BriefPage.pageBy(1)">下一页</button></div>` : ''}`;
        const current = box.querySelector('.brief-scroll');
        if (current) { current.scrollLeft = position.left; current.scrollTop = position.top; }
    },
    showSources(index) {
        const row = this.result?.rows[index];
        if (!row) return;
        const e = v => this.escape(v);
        App.openModal('整点数据来源 · ' + this.localTime(row.hour).slice(0,16),
            `<p class="brief-muted">生成时间：${e(this.localTime(row.generated_at))} · 此行已保存，后续补录不会改写。</p>
            ${row.model ? `<p class="brief-muted">模型：${e(row.model.method?.toUpperCase())} · 训练时间 ${e(row.model.trained_at)} · ${e(row.model.n)} 条训练记录</p>` : ''}
            <div class="brief-source-scroll"><table class="data-table">
            <thead><tr><th>指标</th><th>值</th><th>来源 / 采样时间</th><th>说明</th></tr></thead>
            <tbody>${this.result.columns.map(c => {
                const cell = row.cells[c.key];
                return `<tr><td>${e(c.label)}</td><td>${e(this.value(cell,c.key))} ${e(c.unit)}</td>
                <td>${e(cell?.source || '—')}<br><small>${e(this.localTime(cell?.observed_at))}</small></td>
                <td>${e(this.note(cell) || '正常')}${cell?.item_id ? `<br><small>${e(cell.item_id)}</small>` : ''}
                ${cell?.components ? `<br><small>${Object.entries(cell.components).map(([name, part]) => e(name + '：' + this.localTime(part.observed_at) + (part.reason ? ' · ' + part.reason : ''))).join('<br>')}</small>` : ''}</td></tr>`;
            }).join('')}</tbody></table></div>`,
            '<button class="btn" onclick="App.closeModal()">关闭</button>');
    },
    async exportExcel() {
        const data = this.result;
        if (!data?.rows.length) { App.showToast('当前列表暂无可导出的小时记录', 'warning'); return; }
        const params = new URLSearchParams();
        data.rows.forEach(row => params.append('hour', row.hour));
        try {
            const response = await fetch((window.Api?.base || '/api/v1') + '/hourly-brief/export?' + params,
                {cache:'no-store', signal:AbortSignal.timeout(20000)});
            if (!response.ok) throw new Error('导出失败，请稍后重试');
            const url = URL.createObjectURL(await response.blob());
            const link = document.createElement('a');
            link.href = url;
            link.download = '小时推测简报_' + (this.day || '全部') + '_第' + (Math.floor(this.offset/100)+1) + '页.xlsx';
            document.body.appendChild(link);
            link.click();
            link.remove();
            setTimeout(() => URL.revokeObjectURL(url), 10000);
            App.showToast('已导出当前页 ' + data.rows.length + ' 行及来源说明', 'success');
        } catch (error) { App.showToast(error.message || '导出失败', 'error'); }
    },
};
