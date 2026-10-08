/* 运行状态独立于业务 App.store：轮询不会触发保存、训练或密度动作。 */
window.Operations = {
    health: null,
    telemetry: null,
    manualMeasurements: null,
    manualMeasurementsFailed: false,
    _manualInFlight: null,
    serverState: 'checking',
    telemetryFailed: false,
    checkedAt: null,
    _inFlight: null,
    _timer: null,
    _reconnectTask: null,
    _reconnectBusy: false,
    _densityIncidents: {},

    densityAlerts() {
        const checks = this.telemetry?.density_alarm_checks || [];
        const ids = new Set(checks.map(c => c.id));
        for (const id of Object.keys(this._densityIncidents)) if (!ids.has(id)) delete this._densityIncidents[id];
        return checks.flatMap(check => {
            const state = this.telemetryFailed ? 'unknown' : check.status;
            if (state === 'normal') { delete this._densityIncidents[check.id]; return []; }
            if (state === 'abnormal') this._densityIncidents[check.id] = check;
            const incident = this._densityIncidents[check.id];
            const pending = state === 'unknown';
            return [{id:check.id, level:pending ? '待确认' : '警告',
                message:pending ? (incident ? incident.message + '；当前数据不可用，尚未确认恢复' :
                    this.telemetryFailed ? `${check.device} 密度偏差无法判断：采集查询中断` : check.message) : check.message,
                time:this.time(pending ? incident?.received_at : check.received_at)}];
        });
    },

    init() {
        this.installReconnectControl();
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

    installReconnectControl() {
        const header = document.querySelector('#page-collect .telemetry-panel .panel-header');
        if (!header || document.getElementById('telemetry-reconnect')) return;
        const actions = document.createElement('div');
        actions.className = 'page-header-actions';
        const button = document.createElement('button');
        button.id = 'telemetry-reconnect';
        button.className = 'btn btn-primary btn-sm';
        button.type = 'button';
        button.addEventListener('click', () => this.reconnect());
        actions.appendChild(button);
        const check = header.querySelector('button');
        if (check) actions.appendChild(check);
        header.appendChild(actions);
        this.renderReconnectControl();
    },

    renderReconnectControl() {
        const button = document.getElementById('telemetry-reconnect');
        if (!button) return;
        const local = !window.location.protocol.startsWith('http');
        button.disabled = local || this._reconnectBusy;
        button.textContent = this._reconnectBusy ? '连接中…' : '重新连接';
        button.title = local ? '通过服务器地址打开页面后可重新连接。' : '启动或恢复后台只读采集，关闭页面后继续运行。';
        button.setAttribute('aria-busy', String(this._reconnectBusy));
    },

    reconnect() {
        if (this._reconnectTask) return this._reconnectTask;
        if (!window.location.protocol.startsWith('http')) return Promise.resolve(false);
        this._reconnectBusy = true;
        this.renderReconnectControl();
        this._reconnectTask = this.runReconnect().finally(() => {
            this._reconnectTask = null;
            this._reconnectBusy = false;
            this.renderReconnectControl();
        });
        return this._reconnectTask;
    },

    async runReconnect() {
        try {
            App.showToast('正在重新连接后台采集…', 'info');
            const result = await window.Api.reconnectTelemetry();
            const requestedAt = Date.parse(result.requested_at);
            if (!Number.isFinite(requestedAt)) throw new Error('采集启动响应时间异常');
            const deadline = Date.now() + 90000;
            let lastRequestAt = Date.now();
            while (Date.now() < deadline) {
                await this.refresh();
                const source = !this.telemetryFailed && this.telemetry?.sources.find(item => item.source_id === result.source_id);
                const fresh = source && Date.parse(source.heartbeat_at) >= requestedAt;
                if (fresh && source.state === 'connected' && Date.parse(source.last_sample_at) >= requestedAt) {
                    const items = this.telemetry.items.filter(item => item.source_id === result.source_id);
                    App.showToast(`后台采集已连接，${items.filter(item => item.available).length} / ${items.length} 个点位可用`, 'success');
                    return true;
                }
                if (fresh && source.state === 'fault') throw new Error('采集连接异常：' + (source.error_code || '请检查配置和服务器'));
                // 进程被突然结束时，等待原租约失效后再启动，避免抢占仍在运行的采集。
                if (source && ['offline', 'stopped'].includes(source.state) && Date.now() - lastRequestAt >= 10000) {
                    await window.Api.reconnectTelemetry();
                    lastRequestAt = Date.now();
                }
                await new Promise(resolve => setTimeout(resolve, 2000));
            }
            throw new Error('暂未收到新的采集数据，请检查网线、服务器及采集状态后重试。');
        } catch (error) {
            App.showToast(error.message || '重新连接失败，请稍后重试。', 'error');
            return false;
        }
    },

    refreshManualMeasurements() {
        if (!window.location.protocol.startsWith('http')) return Promise.resolve();
        if (this._manualInFlight) return this._manualInFlight;
        this._manualInFlight = window.Api.getManualMeasurementPoints().then(result => {
            this.manualMeasurements = result;
            this.manualMeasurementsFailed = false;
        }).catch(() => {
            this.manualMeasurementsFailed = true;
        }).finally(() => {
            this._manualInFlight = null;
            if (typeof DecisionOverviewPage !== 'undefined') DecisionOverviewPage.telemetryUpdated();
        });
        return this._manualInFlight;
    },

    async manualMeasurementSaved() {
        // A poll started before POST committed may still carry the old record.
        if (this._manualInFlight) await this._manualInFlight;
        return this.refreshManualMeasurements();
    },

    refresh() {
        if (!window.location.protocol.startsWith('http')) { this.render(); return Promise.resolve(); }
        if (this._inFlight) return this._inFlight;
        if (this._timer) { clearTimeout(this._timer); this._timer = null; }
        this.refreshManualMeasurements();
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
        this.renderReconnectControl();
        if (typeof App.renderAlerts === 'function') App.renderAlerts();
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
            sources.length ? '时间为本机接收时间，每 5 秒刷新；灰色值表示当前不可用，悬停可查看原因。' : '采集尚未启动。完成现场点表和连接配置后，这里将显示实时读数。');
        if (typeof DecisionOverviewPage !== 'undefined') DecisionOverviewPage.telemetryUpdated();
        const body = document.getElementById('telemetry-tbody');
        if (!body) return;
        if (!items.length) { body.innerHTML = '<tr><td colspan="6" class="ops-empty">暂无采集数据</td></tr>'; return; }
        const reasons = {good:'可用', no_data:'暂无数据', bad_quality:'坏质量', uncertain_quality:'质量不确定',
            stale:'接收超时', source_stale:'源时间过期', collector_offline:'采集离线', out_of_range:'超出量程',
            invalid_value_type:'类型不匹配', non_finite_value:'数值无效', future_source_time:'源时钟异常'};
        const sorted = items.slice().sort((a, b) => String(a.device || '').localeCompare(String(b.device || ''), 'zh-CN', {numeric:true}) ||
            String(a.tag_name || a.name || '').localeCompare(String(b.tag_name || b.name || ''), 'zh-CN', {numeric:true}));
        body.innerHTML = sorted.map(item => {
            const sample = item.sample || {};
            const valid = !this.telemetryFailed && item.available;
            const value = sample.value == null ? '—' : typeof sample.value === 'boolean' ? (sample.value ? '1' : '0') : String(sample.value);
            const unit = App.telemetryDisplayUnit(item);
            const displayValue = value + (Number.isFinite(sample.value) && unit ? ' ' + unit : '');
            const quality = {good:'Good', uncertain:'Uncertain', bad:'Bad'}[sample.quality] || '—';
            const reason = this.telemetryFailed ? '查询中断' : reasons[item.status] || '不可用';
            const split = (item.name || '').indexOf(' · ');
            const tag = item.tag_name || (split < 0 ? item.name : item.name.slice(0, split));
            const description = item.description || (split < 0 ? '' : item.name.slice(split + 3));
            return '<tr><td>' + this.escape(item.device || '—') + '</td><td>' + this.escape(tag || '—') + '</td>' +
                '<td>' + this.escape(description || '—') + '</td><td>' + this.escape(item.item_id || sample.node_id || '—') + '</td>' +
                '<td class="' + (valid ? '' : 'ops-unavailable') + '" title="' + this.escape(quality + ' / ' + reason) + '">' + this.escape(displayValue) + '</td>' +
                '<td title="' + this.escape(sample.received_at || '') + '">' + this.escape(this.time(sample.received_at)) + '</td></tr>';
        }).join('');
    },
};
document.addEventListener('DOMContentLoaded', () => window.Operations.init());
