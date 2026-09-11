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
        // 返回结果而不是把失败吞掉（2026-09）：
        // 原来末尾是 .catch(() => {})，于是 keepalive 超限、后端未启动、
        // 守卫拒绝……全部**静默**，表现为"界面一切正常但服务器没收到"。
        // 现在统一返回 {ok:true} 或 {ok:false, rejected|error}，由 App._flushMirror 处置
        // （留下待发副本 + 提示 + 重试）。
        // 加上限超时：中断/挂死的请求若永不回调，App 侧的重试闸门(_mirrorInFlight)会永久卡住。
        if (!init.signal && typeof AbortSignal !== 'undefined' && AbortSignal.timeout) {
            init.signal = AbortSignal.timeout(o.timeoutMs || 20000);
        }
        return fetch(url, init)
          .then(r => (r.ok ? r.json() : Promise.reject(new Error('state ' + r.status))))
          .then(j => {
              if (j && j.ok === false) {
                  // **只有守卫明确的 stale 拒绝**才算"永久拒绝"（重试结果必然相同）。
                  // 后端 migrate.replace 的 except 分支同样返回 ok:false 但没有 stale
                  // （例如 SQLite 写锁超时）—— 那是**瞬时**故障，必须留给上层留副本重试，
                  // 否则"服务器内部异常"会被当成"永久拒绝、不再重试"，
                  // 正是这次改动要根除的那类静默丢失。
                  return { ok: false, rejected: j.stale === true,
                           reason: j.error || '服务器拒绝了整库写' };
              }
              return { ok: true };
          })
          .catch(e => ({ ok: false, error: String((e && e.message) || e) }));
    },

    putState(store, force) {
        // 异步镜像，不阻塞主流程（结果由 App._flushMirror 统一处置）
        return this.putStateBody(JSON.stringify(store), { force: force });
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
