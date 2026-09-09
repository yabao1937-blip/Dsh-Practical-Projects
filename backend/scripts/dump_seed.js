// 把前端 seed_data.js 的 DMCS_SEED_STORE 导出为 JSON（供后端 golden 对拍复用同一份数据）
const fs = require('fs');
const vm = require('vm');
const src = process.argv[2];
const out = process.argv[3];
const code = fs.readFileSync(src, 'utf8');
const ctx = { window: {} };
vm.createContext(ctx);
vm.runInContext(code, ctx);
const store = ctx.window.DMCS_SEED_STORE;
if (!store) { console.error('DMCS_SEED_STORE 未找到'); process.exit(1); }
fs.writeFileSync(out, JSON.stringify(store));
console.log('dumped keys:', Object.keys(store).join(','));
