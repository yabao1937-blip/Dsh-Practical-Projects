// 只读取受版本管理的夹具；不读取浏览器或现场数据库。
const fs = require('node:fs');
const path = require('node:path');
const {App} = require('./app_vm')();
const target = path.resolve(__dirname, '../data/train_js.json');
const fixture = JSON.parse(fs.readFileSync(target, 'utf8'));
App.store.coarseCoal = fixture.coarseCoal;
App.store.coarseModelHistory = [];
App.store.coarseTolerance = 0.8;
App.trainCoarseModel('jun_jul');
fixture.coarseModel = App.store.coarseModel;
fixture.history = App.store.coarseModelHistory;
fs.writeFileSync(target, JSON.stringify(fixture));
console.log('Training oracle:', fixture.coarseModel.production, 'PLS A:', fixture.coarseModel.pls.A);
