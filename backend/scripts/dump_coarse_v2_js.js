// 无浏览器/数据库依赖的 v2 模型 oracle；--stdin 接收完整记录数组供 pytest 对拍。
const fs = require('node:fs');
const path = require('node:path');
const {App} = require('./app_vm')();
const stdin = process.argv.includes('--stdin');
const rows = stdin ? JSON.parse(fs.readFileSync(0, 'utf8'))
    : JSON.parse(fs.readFileSync(path.join(__dirname, '../data/train_js.json'), 'utf8')).coarseCoal;
App.store.coarseTolerance = .8;
if (stdin) {
    console.log(JSON.stringify(App._trainCoarseRows(process.argv.includes('--daily') ? App._coarseDaily(rows) : rows)));
} else {
    App.store.coarseCoal = rows;
    App.store.coarseModelHistory = [];
    App.trainCoarseModel('jun_jul', 'gpt');
    console.log(JSON.stringify({features: App.MLR_FEATURES, coarseModel: App.store.coarseModel,
        history: App.store.coarseModelHistory, coarseCoal: rows.map(r => ({timestamp: r.timestamp, predicted_ash: r.predicted_ash}))}));
}
