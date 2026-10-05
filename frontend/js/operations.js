/* 运行状态独立于业务 App.store：轮询不会触发保存、训练或密度动作。 */
window.Operations = {
    health: null,
    telemetry: null,
    serverState: 'checking',
    telemetryFailed: false,
    checkedAt: null,
    _inFlight: null,
    _timer: null,

    init() {
        if (!window.location.protocol.startsWith('http')) {
            this.serverState = 'local';
            this.render();
            return;
        }
        this.refresh();
        document.addEventListener('visibilitychange', () => {
            if (this._timer) { clearTimeout(this._timer); this._timer = null; }
            if (document.visibilityState !== 'hidden') this.refresh();
        });
        window.addEventListener('online', () => this.refresh());
    },

    refresh() {
        if (!window.location.protocol.startsWith('http')) { this.render(); return Promise.resolve(); }
        if (this._inFlight) return this._inFlight;
        if (this._timer) { clearTimeout(this._timer); this._timer = null; }
        this._inFlight = Promise.allSettled([window.Api.getHealth(), window.Api.getTelemetry()]).then(results => {
            const [health, telemetry] = results;
            if (health.status === 'fulfilled') {
                this.health = health.value;
                this.serverState = this.health.ok && this.health.status === 'ok' ? 'online' : 'unavailable';
            } else this.serverState = 'offline';
            this.telemetryFailed = telemetry.status !== 'fulfilled';
            if (!this.telemetryFailed) this.telemetry = telemetry.value;
            this.checkedAt = Date.now();
            this.render();
        }).catch(() => {
            this.serverState = 'offline';
            this.telemetryFailed = true;
            this.render();
        }).finally(() => {
            this._inFlight = null;
            if (document.visibilityState !== 'hidden') this._timer = setTimeout(() => this.refresh(), 5000);
        });
        return this._inFlight;
    },

    text(id, value, kind) {
        const el = document.getElementById(id);
        if (!el) return;
        el.textContent = value;
        if (kind) el.className = 'status-val ' + kind;
    },

    escape(value) {
        return String(value == null ? '' : value).replace(/[&<>"']/g, ch => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[ch]));
    },

    time(value) {
        if (!value) return '—';
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', {hour12:false});
    },

    renderSync() {
        if (this.serverState === 'local') { this.text('status-sync', '单机存储', ''); return; }
        const state = App.mirrorStatus;
        let label = '服务器尚未确认', kind = 'warn';
        if (App._mirrorConflict) { label = '版本冲突 · 待核对'; kind = 'error'; }
        else if (App._mirrorInFlight) label = '同步中';
        else if (App._mirrorPending || App._readPendingSlot() || state.pending) {
            label = state.lastError ? '同步失败 · 已保留待发' : '待同步';
            kind = state.lastError ? 'error' : 'warn';
        } else if (App._serverStateConfirmed) { label = '已同步'; kind = 'good'; }
        this.text('status-sync', label, kind);
        const el = document.getElementById('status-sync');
        if (el) el.title = (state.lastError || '') + (state.lastOkAt ? '；最后确认 ' + this.time(state.lastOkAt) : '');
    },

    render() {
        const serverNames = {checking:'检查中', online:'后端在线', offline:'后端离线', unavailable:'数据库未就绪', local:'单机模式'};
        const server = serverNames[this.serverState];
        this.text('server-status-label', server);
        this.text('ops-server', server);
        const dot = document.getElementById('server-status-dot');
        if (dot) dot.className = 'status-dot ' + (this.serverState === 'online' ? 'online' : ['offline','unavailable'].includes(this.serverState) ? 'offline' : '');
        this.text('ops-checked', this.time(this.checkedAt));
        this.renderSync();
        const sources = this.telemetry ? this.telemetry.sources : [];
        const items = this.telemetry ? this.telemetry.items : [];
        const allConnected = sources.length > 0 && sources.every(source => source.state === 'connected');
        const online = !this.telemetryFailed && allConnected;
        const sourceNames = {connecting:'连接中', connected:'采集中', retrying:'断线重连', offline:'心跳中断', stopped:'已停止', fault:'配置或连接异常'};
        const collector = this.serverState === 'local' ? '单机模式' : this.telemetryFailed ? '状态不可达' :
            sources.length ? sources.map(source => source.name + '：' + (sourceNames[source.state] || '未启动')).join('；') : '未启动 / 待配置';
        this.text('ops-collector', collector);
        this.text('status-collect', online ? '采集中' : collector, online ? 'good' : 'warn');
        const available = this.telemetryFailed ? 0 : items.filter(item => item.available).length;
        this.text('ops-points', available + ' / ' + items.length);
        this.text('ops-message', this.serverState === 'local' ? '通过服务器地址打开页面，即可查看实时采集状态。' :
            this.telemetryFailed ? '采集查询失败，保留最后读数供核对；当前读数不可用。' :
            sources.length ? '按质量和时效核对读数；时间按本机时区显示。' : '采集尚未启动。完成现场点表和连接配置后，这里将显示实时读数。');
        const body = document.getElementById('telemetry-tbody');
        if (!body) return;
        if (!items.length) { body.innerHTML = '<tr><td colspan="7" class="ops-empty">暂无采集数据</td></tr>'; return; }
        const reasons = {good:'可用', no_data:'暂无数据', bad_quality:'坏质量', uncertain_quality:'质量不确定',
            stale:'接收超时', source_stale:'源时间过期', collector_offline:'采集离线', out_of_range:'超出量程',
            invalid_value_type:'类型不匹配', non_finite_value:'数值无效', future_source_time:'源时钟异常'};
        body.innerHTML = items.map(item => {
            const sample = item.sample || {};
            const valid = !this.telemetryFailed && item.available;
            const value = sample.value == null ? '—' : typeof sample.value === 'boolean' ? (sample.value ? '开启' : '关闭') : String(sample.value);
            const quality = {good:'Good', uncertain:'Uncertain', bad:'Bad'}[sample.quality] || '—';
            const reason = this.telemetryFailed ? '查询中断' : reasons[item.status] || '不可用';
            const timeCell = value => '<td title="' + this.escape(value || '') + '">' + this.escape(this.time(value)) + '</td>';
            return '<tr><td>' + this.escape(item.name) + '<br><small class="ops-help">' + this.escape(item.source_id + '/' + item.key) + '</small></td>' +
                '<td class="' + (valid ? '' : 'ops-unavailable') + '">' + this.escape(value) + '</td><td>' + this.escape(item.unit) + '</td>' +
                '<td class="' + (valid ? 'ops-good' : 'ops-warning') + '">' + quality + ' / ' + reason + '</td>' +
                timeCell(sample.source_at) + timeCell(sample.server_at) + timeCell(sample.received_at) + '</tr>';
        }).join('');
    },
};
document.addEventListener('DOMContentLoaded', () => window.Operations.init());
