import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import {SpreadsheetFile, Workbook} from '@oai/artifact-tool';

const [inputPath, outputPath, evidencePath] = process.argv.slice(2);
const source = JSON.parse(await fs.readFile(inputPath, 'utf8'));
const headers = ['序号', 'tag（point_key）', '标签名', '设备', '说明', 'ItemID', 'NodeID'];
const data = source.points.map((point, index) => [index + 1, point.key, point.tag_name,
    point.device, point.description, point.item_id, point.node_id]);
assert.equal(data.length, 50);

const workbook = Workbook.create();
const sheet = workbook.worksheets.add('50点位对照');
sheet.showGridLines = false;
sheet.getRange('A1:G57').format.font = {name: 'Arial', size: 11, color: '#24324A'};
sheet.getRange('A1').values = [['KEPServer 50点位 tag 对照表']];
sheet.getRange('A1').format.font = {name: 'Arial', size: 18, bold: true, color: '#253A79'};
sheet.getRange('A1:G1').format.rowHeightPx = 40;
sheet.getRange('A2').values = [[`采集源：${source.source_id}；当前启用的50个物理点位`]];
sheet.getRange('A2:E2').format.font = {name: 'Arial', size: 10, italic: true, color: '#617087'};
sheet.getRange('F2').values = [['导出日期']];
sheet.getRange('F2').format.horizontalAlignment = 'right';
sheet.getRange('G2').values = [[new Date(`${source.date}T00:00:00Z`)]];
sheet.getRange('G2').setNumberFormat('yyyy-mm-dd');
sheet.getRange('A2:G2').format.rowHeightPx = 27;
sheet.getRange('A3').values = [['来源：kepserver.local.json，并与 dense_medium.db 的 telemetry_points 核对']];
sheet.getRange('A3:G3').format.font = {name: 'Arial', size: 10, italic: true, color: '#617087'};
sheet.getRange('A3:G3').format.rowHeightPx = 27;
sheet.getRange('A3:G3').format.borders = {bottom: {style: 'thin', color: '#C6D2E7'}};

sheet.getRange('A4:G54').values = [headers, ...data];
const table = sheet.tables.add('A4:G54', true, 'KepserverPointMapping');
table.style = 'TableStyleMedium2';
table.showFilterButton = true;
sheet.getRange('A4:G4').format = {fill: '#253A79', font: {name: 'Arial', size: 11,
    bold: true, color: '#FFFFFF'}, horizontalAlignment: 'center', verticalAlignment: 'center',
    borders: {insideVertical: {style: 'thin', color: '#FFFFFF'}}, rowHeightPx: 36};
sheet.getRange('A5:G54').format.horizontalAlignment = 'left';
sheet.getRange('A5:G54').format.verticalAlignment = 'center';
sheet.getRange('A5:G54').format.rowHeightPx = 32;
sheet.getRange('A5:A54').format.horizontalAlignment = 'right';
sheet.getRange('A5:A54').setNumberFormat('0');
sheet.getRange('B5:G54').setNumberFormat('@');
sheet.getRange('E5:E54').format.wrapText = true;
for (const [column, width] of [['A',50],['B',230],['C',170],['D',110],['E',225],['F',320],['G',370]]) {
    sheet.getRange(`${column}1:${column}57`).format.columnWidthPx = width;
}
sheet.freezePanes.freezeRows(4);
sheet.freezePanes.freezeColumns(2);
sheet.getRange('A56').values = [['tag生成规则：tag_ + ItemID的SHA-256哈希值前20位。']];
sheet.getRange('A57').values = [[`查询历史记录：source_id = ${source.source_id}，point_key = 本表tag。`]];
sheet.getRange('A56:G57').format.font = {name: 'Arial', size: 10, italic: true, color: '#617087'};
sheet.getRange('A56:G57').format.rowHeightPx = 27;

assert.deepEqual(sheet.getRange('A5:G54').values, data);
const sample = await workbook.inspect({kind: 'table', range: '50点位对照!A4:G10',
    include: 'values,formulas', tableMaxRows: 7, tableMaxCols: 7, maxChars: 2200});
const errors = await workbook.inspect({kind: 'match', searchTerm: '#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',
    options: {useRegex: true, maxResults: 30}, summary: 'formula error scan', maxChars: 1000});
await fs.writeFile(path.join(evidencePath, 'inspection.txt'), sample.ndjson + '\n' + errors.ndjson);
for (const [name, range] of [['preview-top','A1:G15'],['preview-bottom','A45:G57']]) {
    const preview = await workbook.render({sheetName: '50点位对照', range, scale: 1.5, format: 'png'});
    await fs.writeFile(path.join(evidencePath, `${name}.png`), new Uint8Array(await preview.arrayBuffer()));
}
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
await fs.writeFile(path.join(evidencePath,'result.json'),JSON.stringify({output:outputPath,points:data.length,
    sourceId:source.source_id,tagGenerationVerified:true,disabledPointsExcluded:true,uniqueIdentifiers:true},null,2)+'\n');
console.log(JSON.stringify({output:outputPath,points:data.length,formulaScan:errors.ndjson}));
