// 只读取受版本管理的夹具；不读取浏览器或现场数据库。
const fs = require('node:fs');
const path = require('node:path');
// train_js.json 留作 v1/通用求解器回归；新编排生成独立 v2 oracle。
const {execFileSync} = require('node:child_process');
const target = path.resolve(__dirname, '../data/coarse_v2_js.json');
const output = execFileSync(process.execPath, [path.join(__dirname, 'dump_coarse_v2_js.js')], {encoding: 'utf8'});
fs.writeFileSync(target, output);
console.log('v2 training oracle refreshed');
