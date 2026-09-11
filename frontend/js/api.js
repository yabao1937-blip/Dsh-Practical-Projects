/* ========================================
   前后端分离数据层（双轨过渡）
   - file:// 打开：仍走 localStorage，行为不变
   - http:// 打开（后端托管）：saveStore 额外把整库镜像到 /api/v1/state
     · 服务器带防回退守卫：本地记录数少于服务器时整库镜像会被拒绝(stale)，
       旧浏览器靠 App.autoPullIfStale / 「从服务器恢复数据」按钮反向同步
     · force=true 跳过守卫，仅用于清空数据/恢复备份等有意回退
     · 整库镜像由 App._scheduleMirror 合并后调用 putStateBody（见其注释），
       本模块不再自己决定发送时机
   ======================================== */
window.Api = {
    base: '/api/v1',

    async getState() {
        const r = await fetch(this.base + '/state');
        if (!r.ok) throw new Error('state ' + r.status);
        return await r.json();
    },

    // 发送**已序列化**的整库快照。调用方（App._scheduleMirror）已经为了写 localStorage
    // stringify 过一次，这里再序列化一遍纯属浪费（单次快照实测 130 KB）。
    // opts.keepalive：用于页面卸载路径 —— 普通 fetch 在卸载中会被中断，keepalive 才送得出去。
    putStateBody(body, opts) {
        const o = opts || {};
        const url = this.base + '/state' + (o.force ? '?force=true' : '');
        const init = {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: body,
        };
        if (o.keepalive) init.keepalive = true;
        fetch(url, init)
          .then(r => (r.ok ? r.json() : Promise.reject(new Error('state ' + r.status))))
          .then(j => {
              if (j && j.ok === false && j.stale) {
                  console.warn('镜像被服务器拒绝(本地数据较旧):', j.error,
                      '—— 请点「从服务器恢复数据」或刷新页面自动同步');
              }
          })
          .catch(() => {});
    },

    putState(store, force) {
        // 异步镜像，不阻塞主流程；后端未启动时静默失败
        this.putStateBody(JSON.stringify(store), { force: force });
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
