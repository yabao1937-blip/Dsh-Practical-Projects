import fs from 'node:fs/promises';
import path from 'node:path';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const root = path.resolve('D:/dense-medium-density-control-system');
const out = path.join(root, 'outputs/recent-coarse-ash-20261013');
const data = JSON.parse(await fs.readFile(path.join(root, 'backend/data/gpt_coarse/recent_assays/20261013/joined.json'), 'utf8'));
const wb = Workbook.create();
const main = wb.worksheets.add('拼接结果');
const audit = wb.worksheets.add('逐因子来源');
const navy = '#29365D', blue = '#343CF4', muted = '#697590';
const features = data.feature_names;
// Excel stores local wall-clock dates. Explicitly retain Beijing components.
// Numeric date serials avoid the runtime's Date export rounding down by 1 ms.
const excelLocal = value => value ? (Date.parse(value.replace(/(?:Z|[+-]\d\d:\d\d)$/, '') + 'Z') - Date.UTC(1899,11,30))/86400000 : null;
const sourceLabel = m => m.database === 'production' ? '主库' : m.database ? '10月12日备份' : '无';
const statusLabel = s => ({good:'匹配',minute_approximation:'分钟级近似',gap:'超出匹配范围',no_history:'无更早记录',
  stopped:'停机',bad_quality:'质量异常',invalid:'值异常',definition_mismatch:'定义不一致'}[s] || s);

function style(sheet, last, columns) {
  sheet.showGridLines = false;
  const end = String.fromCharCode(64 + columns.length);
  const area = sheet.getRange(`A1:${end}${last}`);
  area.format.font = { name: 'Arial', size: 11, color: navy };
  area.format.rowHeight = 25;
  area.format.verticalAlignment = 'center';
  columns.forEach((width, i) => sheet.getRangeByIndexes(0, i, last, 1).format.columnWidthPx = width);
  sheet.getRange('A2').format.font = { name: 'Arial', size: 16, bold: true, color: navy };
  sheet.getRange('A2').format.rowHeight = 32;
  sheet.freezePanes.freezeRows(8);
  sheet.freezePanes.freezeColumns(3);
  sheet.tabColor = blue;
}

const last = 8 + data.rows.length;
style(main, last, [65,112,170,113,139,69,69,76,76,78,78,142,139,145,148,134]);
main.getRange('A2').values = [['近期粗精煤泥灰分与八因子拼接']];
main.getRange('A4').values = [['采样条数']]; main.getRange('B4').formulas = [[`=COUNTA(A9:A${last})`]];
main.getRange('D4').values = [['完整近似拼接']]; main.getRange('E4').formulas = [[`=COUNTIFS(M9:M${last},"分钟级近似")+COUNTIFS(M9:M${last},"严格匹配")`]];
main.getRange('G4').values = [['缺失']]; main.getRange('H4').formulas = [[`=COUNTIFS(M9:M${last},"历史缺失或异常")`]];
main.getRange('J4').values = [['严格匹配']]; main.getRange('K4').formulas = [[`=COUNTIFS(M9:M${last},"严格匹配")`]];
main.getRange('A5').values = [['北京时间。按生产日分组，凌晨归次日；10.1按10月10日处理。空白因子表示缺失，开关0表示停止、1表示运行。']];
main.getRange('A6').values = [['液位目标为采样前90秒，取目标之前30秒内记录。其余因子仅在本次旧分钟历史拼接中允许向前65秒；在线标准仍为30秒。']];
main.getRange('A7').values = [['来源：近期粗精煤泥灰分.xlsx、SQLite原始采集历史、10月12日09:00备份。化验报告时间未知；未训练或切换模型。']];
main.getRange('A5:P7').format.font = { name: 'Arial', size: 10, color: muted };
const headers = ['原表行','生产日期','实际采样时间','315灰分 (%)','小时带煤量 (t/h)','A系统','B系统','401系统','402系统','473脱粉','474脱粉','精磁尾液位 (%)','匹配状态','煤量/开关偏差 (s)','液位目标偏差 (s)','数据来源'];
main.getRange('A8:P8').values = [headers];
main.getRange(`A9:P${last}`).values = data.rows.map(r => {
  const seven = features.filter(k=>k !== 'level').map(k=>r.matches[k]);
  const ages = seven.filter(m=>m.offline_usable).map(m=>m.age_seconds);
  const level = r.matches.level;
  return [r.source_row, excelLocal(r.production_day+'T00:00:00'),excelLocal(r.sample_at),r.ash_content,
    ...features.map(k=>r.features[k]),r.match_status,
    ages.length===7 ? Math.max(...ages) : null,level.offline_usable ? level.age_seconds : null,
    r.offline_complete ? [...new Set(Object.values(r.matches).map(sourceLabel))].join('、') : null];
});
main.getRange(`B9:B${last}`).setNumberFormat('yyyy-mm-dd');
main.getRange(`C9:C${last}`).setNumberFormat('mm-dd hh:mm:ss');
main.getRange(`D9:E${last}`).setNumberFormat('0.00');
main.getRange(`F9:K${last}`).setNumberFormat('0');
main.getRange(`L9:L${last}`).setNumberFormat('0.00');
main.getRange(`N9:O${last}`).setNumberFormat('0.000');
main.getRange(`A9:O${last}`).format.horizontalAlignment = 'right';
main.getRange(`F9:K${last}`).format.horizontalAlignment = 'center';
main.getRange(`M9:M${last}`).format.horizontalAlignment = 'left';
main.getRange(`D9:D${last}`).format.font = { name: 'Arial', size: 11, bold: true, color: blue };
main.tables.add(`A8:P${last}`,true,'RecentAssayPairs').style='TableStyleLight9';
main.getRange('A8:P8').format = { fill: navy, font:{name:'Arial',size:11,bold:true,color:'#FFFFFF'},rowHeight:34,horizontalAlignment:'center',verticalAlignment:'center'};
main.getRange(`M9:M${last}`).conditionalFormats.add('containsText',{text:'缺失',format:{fill:'#FCEBEC',font:{color:'#B72735'}}});
main.getRange(`M9:M${last}`).conditionalFormats.add('containsText',{text:'近似',format:{fill:'#FFF3D8',font:{color:'#886015'}}});

const detail = data.rows.flatMap(r => features.map(k => {
  const m=r.matches[k];
  return [r.source_row,excelLocal(r.sample_at),m.name,m.candidate_value===true?1:m.candidate_value===false?0:m.candidate_value,
    excelLocal(m.target_local),excelLocal(m.received_local),m.age_seconds??null,statusLabel(m.status),
    sourceLabel(m),m.reading_id??null,m.quality??null,m.reason || (m.status==='minute_approximation'?'超出在线30秒时效，仅作离线近似':''),
    m.point_key,m.batch_id??null];
}));
const end=8+detail.length;
style(audit,end,[65,170,125,118,205,218,115,137,139,105,84,530,259,299]);
audit.getRange('A2').values=[['逐因子匹配时间与来源']];
audit.getRange('A4').values=[['每条采样对应8行。候选原始值可能已过期；只有拼接结果页非空因子才进入本次离线候选数据。']];
audit.getRange('A5').values=[['相差秒数 = 目标时间 − 前次接收时间。液位目标已提前90秒；同一条坏质量记录不会被更早的好记录替换。']];
audit.getRange('A6').values=[['“10月12日备份”指五秒权威采集升级前的原库备份；记录ID、点位和批次可用于复核。']];
audit.getRange('A7').values=[['原始source_at、server_at、校验哈希与缺口后的首条记录另存于配套JSON，匹配统一以received_at为准。']];
audit.getRange('A4:N7').format.font={name:'Arial',size:10,color:muted};
audit.getRange('A8:N8').values=[['原表行','实际采样时间','因子','候选原始值','目标时间','前次接收时间','相差 (s)','匹配状态','来源库','记录ID','质量','原因','点位','批次']];
audit.getRange(`A9:N${end}`).values=detail;
audit.getRange(`B9:B${end}`).setNumberFormat('mm-dd hh:mm:ss');
audit.getRange(`D9:D${end}`).setNumberFormat('0.000');
audit.getRange(`E9:F${end}`).setNumberFormat('mm-dd hh:mm:ss.000');
audit.getRange(`G9:G${end}`).setNumberFormat('0.000');
audit.getRange(`A9:B${end}`).format.horizontalAlignment='right';
audit.getRange(`D9:G${end}`).format.horizontalAlignment='right';
audit.tables.add(`A8:N${end}`,true,'AssayFactorEvidence').style='TableStyleLight9';
audit.getRange('A8:N8').format={fill:navy,font:{name:'Arial',size:11,bold:true,color:'#FFFFFF'},rowHeight:34,horizontalAlignment:'center',verticalAlignment:'center'};
audit.getRange(`H9:H${end}`).conditionalFormats.add('containsText',{text:'超出',format:{fill:'#FCEBEC',font:{color:'#B72735'}}});
audit.getRange(`H9:H${end}`).conditionalFormats.add('containsText',{text:'无更早',format:{fill:'#FCEBEC',font:{color:'#B72735'}}});
audit.getRange(`H9:H${end}`).conditionalFormats.add('containsText',{text:'近似',format:{fill:'#FFF3D8',font:{color:'#886015'}}});

wb.recalculate();
console.log((await wb.inspect({kind:'table',range:'拼接结果!A4:K4',include:'values,formulas',tableMaxRows:1,tableMaxCols:11,maxChars:1800})).ndjson);
console.log((await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:20},maxChars:1600})).ndjson);
for (const [sheetName,range,file] of [['拼接结果',`A1:P${last}`,'main.png'],['逐因子来源','A1:N20','audit.png']]) {
  const image=await wb.render({sheetName,range,scale:1,format:'png'});
  await fs.writeFile(path.join(out,file),new Uint8Array(await image.arrayBuffer()));
}
await (await SpreadsheetFile.exportXlsx(wb)).save(path.join(out,'近期粗精煤泥灰分_八因子拼接.xlsx'));
await fs.copyFile(path.join(root,'backend/data/gpt_coarse/recent_assays/20261013/joined.json'),path.join(out,'joined.json'));
console.log(JSON.stringify({output:path.join(out,'近期粗精煤泥灰分_八因子拼接.xlsx'),rows:data.rows.length,details:detail.length}));
