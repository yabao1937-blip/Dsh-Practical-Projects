// 独立 Node 夹具导出，供历史简报前后端对拍。
const fs = require('node:fs');
const path = require('node:path');
const {App} = require('./app_vm')();
const ZERO_MODEL_NODE = {
    type: 'pls', intercept: 10.0,
    coefs: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0], means: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    stds: [1, 1, 1, 1, 1, 1, 1, 1, 1, 1], stdCoef: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    imputeMeans: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    A: 1, metrics: {}, n: 3, tolerance: 0.8, range: 'fixture',
};
const FIXTURE = {
    coarseCoal: [
        { timestamp: '2026-08-01 08:10:00', ash_content: 12.5, coal_amount: 800, level: 55, raw_ash: 40.0 },
        { timestamp: '2026-08-01 12:00:00', ash_content: 13.1, coal_amount: 810, level: 57, raw_ash: 40.0 },
        { timestamp: '2026-08-02 08:00:00', ash_content: 12.8, coal_amount: 790, level: 54, raw_ash: 40.0 },
    ],
    floatCoal: [
        { timestamp: '2026-08-01 07:30:00', ash_content: 9.5, coal_amount: 30.0 },
        { timestamp: '2026-08-02 12:00:00', ash_content: 9.8, coal_amount: 32.0 },
    ],
    calcLogs: [
        { timestamp: '2026-08-01 08:05:00', calc_type: 'ash_density',
          input_json: JSON.stringify({ system: 'A', belt: '502', ash_content: 8.5, density: 1.45 }) },
        { timestamp: '2026-08-01 22:00:00', calc_type: 'ash_density',
          input_json: JSON.stringify({ system: 'A', belt: '502', ash_content: 8.6, density: 1.46 }) },
        { timestamp: '2026-08-02 09:00:00', calc_type: 'ash_density',
          input_json: JSON.stringify({ system: 'A', belt: '502', ash_content: 8.4, density: 1.47 }) },
    ],
    coarseModel: {
        production: 'pls',
        pls: Object.assign({}, ZERO_MODEL_NODE),
        mlr: Object.assign({}, ZERO_MODEL_NODE, { type: 'mlr' }),
        trainedAt: '2026-08-01 00:00:00', n: 3, tolerance: 0.8, range: 'fixture',
    },
};


App.store = FIXTURE;
const result = App.buildHourlyBrief();
fs.writeFileSync(process.argv[2] || path.resolve(__dirname, '../data/brief_js.json'), JSON.stringify(result));
console.log('Brief oracle rows:', result.rows.length);
