/* Read-only test workspace shell. Legacy production and DS pages keep their readers. */
const CoreWorkspace = {
    installed:false,
    install() {
        if (this.installed || !Api.isReadOnlyTest()) return;
        this.installed=true;
        document.body.classList.add('core-workspace');
        const maintenance=document.getElementById('page-maintenance');
        maintenance.hidden=false;
        document.getElementById('maintenance-nav').hidden=false;
        const descriptions={
            'page-overview':'原密度视图 · 业务数据导出与恢复',
            'page-collect':'采集点位状态 · 补录记录',
            'page-coarse':'GPT / DS 分析 · 模型训练与版本核对',
            'page-float':'浮精历史与影响分析',
            'page-import':'批量导入 · 数据校验',
            'page-brief':'小时记录 · 人工反馈',
            'page-history':'历史采集与业务数据查询',
        };
        const tools=document.getElementById('maintenance-pages');
        for (const item of [...document.querySelectorAll('.main-nav .nav-item')]) {
            const description=descriptions[item.dataset.page];
            if (!description) continue;
            const note=document.createElement('small');note.textContent=description;
            item.append(note);tools.append(item);
        }
        const refresh=document.getElementById('btn-pull-server');
        if (refresh) {
            refresh.textContent='刷新主机数据';refresh.title='重新读取主机数据，测试服不写回';
            document.getElementById('maintenance-refresh').append(refresh);
        }
        document.getElementById('maintenance-sources').addEventListener('click', () => {
            App.goToPage('page-decision');
            setTimeout(() => {if(DecisionOverviewPage.active()) DecisionOverviewPage.select('raw-ash');}, 80);
        });
        document.getElementById('maintenance-backup').addEventListener('click', () => App.goToPage('page-overview'));
        document.getElementById('maintenance-diagnostics').addEventListener('click', async () => {
            await window.Operations?.refresh();this.renderDiagnostics();
        });
        const analysis=document.createElement('details');analysis.id='decision-history-analysis';
        analysis.className='core-history-analysis';
        analysis.innerHTML='<summary>历史分析（按需加载）</summary><p role="status" id="core-history-status">展开后读取主机历史数据和模型分析。</p><button type="button" class="btn" id="core-history-retry" hidden>重新加载历史分析</button>';
        const coarse=document.getElementById('decision-coarse-analysis');coarse.before(analysis);
        analysis.append(coarse,document.getElementById('decision-float-analysis'));
        analysis.addEventListener('toggle', () => {if(analysis.open) void this.loadAnalysis();});
        document.getElementById('core-history-retry').addEventListener('click', () => this.loadAnalysis());
        // All refresh paths (theme, poll and model callbacks) honor the same gate.
        for (const controller of [DecisionCoarseCharts,DecisionFloatAnalysis]) {
            const refreshCharts=controller.refresh;
            controller.refresh=function(...args) {
                if (CoreWorkspace.historyOpen() && App._readOnlyFullStateLoaded)
                    return refreshCharts.apply(this,args);
            };
        }
        const render=window.Operations.render;
        window.Operations.render=function(...args) {const result=render.apply(this,args);CoreWorkspace.renderDiagnostics();return result;};
    },
    historyOpen() {
        return !!document.getElementById('decision-history-analysis')?.open &&
            !!document.getElementById('page-decision')?.classList.contains('active');
    },
    needsAnalysis() {
        return this.historyOpen() || !!document.getElementById('page-coarse')?.classList.contains('active');
    },
    async loadAnalysis() {
        const status=document.getElementById('core-history-status'),retry=document.getElementById('core-history-retry');
        if (this.analysisLoading) return this.analysisLoading;
        status.textContent='正在读取主机历史数据…';retry.hidden=true;
        this.analysisLoading=(async () => {
            try {
                await App.ensureReadOnlyHistory();
                if (!this.historyOpen()) return;
                await GptExpert9.load(true);
                if (!this.historyOpen()) return;
                if (GptExpert9.error) throw new Error(GptExpert9.error);
                DecisionCoarseCharts.refresh();DecisionFloatAnalysis.refresh();
                status.textContent='历史分析已加载。';
            } catch(error) {
                status.textContent='历史分析读取失败：'+error.message+'；实时决策仍可使用。';retry.hidden=false;
            } finally {this.analysisLoading=null;}
        })();
        return this.analysisLoading;
    },
    pageChanged(pageId) {
        if (!this.installed) return;
        document.getElementById('maintenance-nav').classList.toggle('active',pageId!=='page-decision');
        if (pageId==='page-coarse') void GptExpert9.load();
        if (pageId==='page-decision' && this.historyOpen()) void this.loadAnalysis();
        if (pageId==='page-maintenance') this.renderDiagnostics();
    },
    renderDiagnostics() {
        if (!this.installed || !document.getElementById('page-maintenance')?.classList.contains('active')) return;
        const ops=window.Operations, host=document.getElementById('maintenance-status');
        const state={online:'在线',busy:'繁忙',offline:'离线',unavailable:'数据库未就绪',checking:'检查中'};
        const collecting={connected:'采集中',connecting:'连接中',retrying:'重连中',offline:'离线',stopped:'已停止',fault:'异常'};
        const facts=[['运行模式','笔记本代码 · 主机数据只读'],['服务状态',state[ops.serverState] || ops.serverState],
            ['采集来源',(ops.telemetry?.sources || []).map(source=>`${source.name}：${collecting[source.state] || source.state}`).join('；') || '暂无'],
            ['采集查询',ops.telemetryFailed?'失败':'正常'],['补录查询',ops.manualMeasurementsFailed?'失败':'正常'],
            ['最后检查',ops.time(ops.checkedAt)],['完整历史',App._readOnlyFullStateLoaded?'已按需加载':'未加载'],
            ['最近手动刷新',App._readOnlyRefreshError || '正常']];
        host.replaceChildren();
        for (const [name,value] of facts) {
            const term=document.createElement('dt'), detail=document.createElement('dd');
            term.textContent=name;detail.textContent=value;host.append(term,detail);
        }
    },
};
window.CoreWorkspace=CoreWorkspace;
