// Standalone P-GPT build: never load the existing app.js or coarse.js.
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
require(path.join(root, 'frontend/pgpt/data.js'));
const model = require(path.join(root, 'frontend/pgpt/model.js'));
const result = {schema: 1, builtAt: new Date().toISOString(),
    evaluationNote: '6—7月训练与时间分块调参；8—9月为向前时间检验。该批数据已在项目开发中使用，不是新的独立检验数据。',
    sample: model.evaluate(globalThis.PGPT_DATA.rows, 'sample'),
    day: model.evaluate(globalThis.PGPT_DATA.rows, 'day')};
fs.writeFileSync(path.join(root, 'frontend/pgpt/models.js'), 'globalThis.PGPT_MODELS = ' + JSON.stringify(result) + ';\n');
fs.writeFileSync(path.join(root, 'docs/pgpt-evaluation.json'), JSON.stringify(result, null, 2) + '\n');
for (const mode of ['sample', 'day']) console.log(mode, JSON.stringify({lambda: result[mode].model.lambda,
    historical: result[mode].historical, tuning: result[mode].tuning.validation, forward: result[mode].forward, baseline: result[mode].forwardBaseline}));
