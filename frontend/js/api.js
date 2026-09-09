/* ========================================
   前后端分离数据层（双轨过渡）
   - file:// 打开：仍走 localStorage，行为不变
   - http:// 打开（后端托管）：saveStore 额外把整库镜像到 /api/v1/state
   ======================================== */
window.Api = {
    base: '/api/v1',

    async getState() {
        const r = await fetch(this.base + '/state');
        if (!r.ok) throw new Error('state ' + r.status);
        return await r.json();
    },

    putState(store) {
        // 异步镜像，不阻塞主流程；后端未启动时静默失败
        fetch(this.base + '/state', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(store),
        }).catch(() => {});
    },

    async retrainCoarseModel(range) {
        // 后端训练 MLR+PLS 粗灰模型；返回 { coarseModel(全量), history, production, ... }
        const r = await fetch(this.base + '/training/coarse-model?range=' + encodeURIComponent(range || 'jun_jul'), {
            method: 'POST',
        });
        if (!r.ok) throw new Error('train ' + r.status);
        return await r.json();
    },
};
