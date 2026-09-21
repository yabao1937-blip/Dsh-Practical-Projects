import fs from 'node:fs/promises';
import path from 'node:path';
import {Workbook, SpreadsheetFile} from '@oai/artifact-tool';

const [output, previews] = process.argv.slice(2);
if (!output || !previews) throw new Error('Expected output.xlsx and preview directory');
const wb = Workbook.create();
const changes = wb.worksheets.add('本轮改动');
const tasks = wb.worksheets.add('后续任务');
const phases = wb.worksheets.add('实施批次');

// 人日是开发与测试工作量估算，不含现场等待、数据积累和审批时间。
const taskRows = [
 ['T01','P0','快照版本冲突检测','整库写入携带基础版本；版本校验与写入必须原子执行，防止同条数旧数据覆盖新数据。','两个浏览器修改同一快照，后提交者收到409；修改、删除和重训练都推进版本。','本轮修复',3,'B1','待实施'],
 ['T02','P0','服务端权威与增量同步','记录、配置、采样改用增量接口；离线操作携带幂等键，冲突保留双方内容。','并发、离线重试、重复提交不丢记录；整库接口仅用于明确的恢复流程。','T01',5,'B1','待实施'],
 ['T03','P0','测量来源和有效期','统一value/source/sampledAt/receivedAt/quality/unit；区分实测、预测、默认及过期值。','缺测、过期、未来时间、非法数值均有明确状态；默认值不能冒充实测驱动建议。','现场确认采样周期',3,'B1','待实施'],
 ['T04','P1','测点唯一键与迁移','核实501/502同一时间采样规则，加入测点身份；先备份、排查冲突，再迁移。','同系统同时刻两条皮带均保留；重复导入幂等；迁移与回滚演练通过。','现场确认测点字典',2,'B2','待确认'],
 ['T05','P1','质量平衡与单位口径','核实501与502秤的位置，检查总量相加是否重复计量；确认干湿基、流量和时间窗。','工艺人员确认秤点图和公式；人工算例与系统一致；不自动修改未确认公式。','工艺与仪表人员访谈',2,'B2','待确认'],
 ['T06','P1','无泄漏时序验证','缺失填补、标准化、特征筛选与调参均在训练折内完成；引入按日或班次切分。','未来数据不影响早期折预处理；输出时序RMSE/MAE及最近实测值基线；前后端迁移口径明确。','T03；确认样本分组',5,'B2','待实施'],
 ['T07','P1','模型发布与回滚','候选模型达到时序性能门槛后才发布；保存数据版本、特征版本、参数与模型版本。','两个候选均不合格时保留旧模型；发布可回滚；历史预测可追溯到模型版本。','T06',3,'B2','待实施'],
 ['T08','P1','账号权限与审计','替换公开页面token模式，按查看、操作、管理授权；AI调用增加权限与限流。','查看者不能写入或删除；操作可查人员与时间；恢复和管理功能单独授权。','确认部署网络与人员角色',4,'B3','待确认'],
 ['T09','P1','备份恢复与发布流程','强制覆盖前备份失败则阻断；定期备份；统一Alembic迁移、生产启动和依赖锁。','恢复演练核对数据量与关键字段；新库和旧库升级通过；生产关闭reload。','T01；明确备份保留周期',3,'B3','待实施'],
 ['T10','P2','前端拆分与计算收敛','拆分store、同步、取值、决策和训练；在线模式逐步采用服务端计算结果。','关键业务对拍与浏览器回归通过；文件离线模式明确边界；页面无重复计算副作用。','T02、T03',4,'B3','待实施'],
 ['T11','P2','查询与训练性能','按日期分页与聚合；训练任务异步化，记录任务状态，避免整库读取和长时间占用。','以实际数据量测量响应时间；训练期间可查看页面；超时、取消、失败可恢复。','T02；确定数据增长预期',3,'B3','待实施'],
 ['T12','P2','PLC只读接入与密度标定','先接入带质量码的只读测量，再用现场批准的阶跃实验识别时延与增益。','先影子运行核对仪表值；记录工况、执行时刻与化验响应；未验收前保持人工执行。','T03、T05；现场实验窗口',5,'B4','待确认'],
];

const changeRows = [
 ['C01','P0','灰分测量取值','移除501/502与总灰分录入链中的密度仿真增量；缺失读数不转换为0。','改变密度后原始灰分不变；前后端对拍包含缺测和在线读数。','已实现，待审阅'],
 ['C02','P0','统一服务端决策','总览与AI助手复用取值、常量守卫、重介换算、动作保护和maxStep。','相同状态下两接口给出相同有效性、方向、原因和建议密度。','已实现，待审阅'],
 ['C03','P0','动作与来源持久化','保存动作闩锁、最后调密时间及重介来源开关；新日志标记测量口径。','数据库往返不丢保护状态；重复动作保持；旧日志保留原值并区分。','已实现，待审阅'],
 ['C04','P0','删除接口鉴权','记录与补录DELETE接口接入写权限依赖。','远端无token或错误token返回401；有效token进入记录查询。','已实现，待审阅'],
 ['C05','P1','同步失败分类','保留401/403的denied标记与原因，避免误报为网络故障。','权限、过期快照、数据库异常和断网共5个场景分类正确。','已实现，待审阅'],
 ['C06','P1','回归与说明','增加真实前端函数对拍、决策入口一致性及持久化测试；更新工艺说明。','11个前后端场景按1e-6容差比较；后端回归与JS语法检查通过。','已实现，待审阅'],
];

function layout(sheet, title, subtitle, headers, rows, widths, rowHeight=66) {
    const end = String.fromCharCode(64 + headers.length);
    const last = rows.length + 4;
    sheet.showGridLines = false;
    const used = sheet.getRange(`A1:${end}${last}`);
    used.format.font.name = 'Arial';
    used.format.font.size = 11;
    used.format.font.color = '#243245';
    used.format.verticalAlignment = 'top';
    used.format.wrapText = true;
    sheet.getRange('A1').values = [[title]];
    sheet.getRange('A1').format.font.size = 16;
    sheet.getRange('A1').format.font.bold = true;
    sheet.getRange(`A1:${end}1`).format.rowHeight = 29;
    sheet.getRange(`A1:${end}1`).format.wrapText = false;
    sheet.getRange(`A1:${end}1`).format.borders = {bottom:{style:'thin',color:'#9DAFC0'}};
    sheet.getRange('A2').values = [[subtitle]];
    sheet.getRange(`A2:${end}2`).format.wrapText = false;
    sheet.getRange(`A2:${end}2`).format.rowHeight = 25;
    sheet.getRange('A2').format.font.color = '#64748B';
    sheet.getRange('A4').write([headers, ...rows]);
    sheet.getRange(`A4:${end}4`).format.fill = '#33465C';
    sheet.getRange(`A4:${end}4`).format.font.color = '#FFFFFF';
    sheet.getRange(`A4:${end}4`).format.font.bold = true;
    sheet.getRange(`A4:${end}4`).format.horizontalAlignment = 'center';
    sheet.getRange(`A4:${end}4`).format.rowHeight = 30;
    sheet.getRange(`A4:${end}4`).format.borders = {insideVertical:{style:'thin',color:'#FFFFFF'}};
    sheet.getRange(`A5:${end}${last}`).format.rowHeight = rowHeight;
    for (let i=0;i<widths.length;i++) sheet.getRange(`${String.fromCharCode(65+i)}1:${String.fromCharCode(65+i)}${last}`).format.columnWidthPx=widths[i];
    for (let r=5;r<=last;r+=2) sheet.getRange(`A${r}:${end}${r}`).format.fill='#F1F5F9';
    sheet.freezePanes.freezeRows(4);
    return last;
}

layout(changes,'本轮优化内容','2026-09-21。代码依据：取值链、总览、助手、鉴权、同步与回归测试。',
    ['编号','优先级','模块','完成内容','验证内容','当前阶段'],changeRows,[70,70,165,360,370,145],78);
const lastTask=layout(tasks,'后续优化任务','估算为开发与测试人日，未包含现场等待。黄色列可更新；待确认项需先落实现场条件。',
    ['编号','优先级','工作项','交付内容与原因','验收标准','前置依赖','估算人日','批次','状态'],taskRows,[65,70,180,335,350,165,85,65,95],92);
tasks.getRange(`G5:G${lastTask}`).setNumberFormat('0.0');
tasks.getRange(`G5:G${lastTask}`).format.fill='#FFF3CC';
tasks.getRange(`I5:I${lastTask}`).format.fill='#FFF3CC';
tasks.getRange(`I5:I${lastTask}`).dataValidation={rule:{type:'list',values:['待实施','待确认','进行中','已完成']}};
tasks.getRange(`B5:B${lastTask}`).conditionalFormats.add('containsText',{text:'P0',format:{font:{bold:true,color:'#A63832'}}});

const phaseRows=[
 ['B1','数据与建议可信','本轮改动通过审阅','并发冲突可检测，离线数据不丢失，数据来源和有效期可辨识。',null,'T01–T03'],
 ['B2','工艺口径与模型验收','测点、秤点及样本分组确认','双皮带不互相覆盖；计算口径经现场确认；模型验证无泄漏且可回滚。',null,'T04–T07'],
 ['B3','部署与可维护性','增量同步接口稳定','权限隔离与审计可用，备份恢复可演练，生产部署和查询性能可复现。',null,'T08–T11'],
 ['B4','现场接入与标定','工艺、仪表人员提供窗口','只读接入和影子运行通过；密度时延与增益完成实验验收。',null,'T12'],
];
layout(phases,'建议实施顺序','工作量是假设单名开发者参与的初步估算，不是上线日期承诺。',
    ['批次','目标','进入条件','完成条件','估算人日','任务范围'],phaseRows,[80,180,250,490,95,120],72);
for(let r=5;r<=8;r++) phases.getRange(`E${r}`).formulas=[[`=SUMIF('后续任务'!H5:H${lastTask},A${r},'后续任务'!G5:G${lastTask})`]];
phases.getRange('D10').values=[['总工作量（人日）']];
phases.getRange('E10').formulas=[['=SUM(E5:E8)']];
phases.getRange('E5:E10').setNumberFormat('0.0');
phases.getRange('A12').values=[['说明']];
phases.getRange('B12').values=[['估算假设']];
phases.getRange('C12').values=[['1名开发者，工艺和仪表人员配合']];
phases.getRange('D12').values=[['不含传感器采购、现场实验等待、生产发布审批与样本积累。']];
phases.getRange('B13').values=[['评估依据']];
phases.getRange('C13').values=[['2026-09-21 项目代码与测试']];
phases.getRange('D13').values=[['取值与密度决策、整库迁移、训练交叉验证、数据唯一键、权限与部署脚本。未做现场工艺验收。']];
phases.getRange('A10:F13').format.font.name='Arial';
phases.getRange('A10:F13').format.font.size=11;
phases.getRange('A10:F13').format.wrapText=true;
phases.getRange('A10:F13').format.verticalAlignment='top';
phases.getRange('A12:F13').format.rowHeight=48;

for (const [sheet,range] of [['本轮改动','A1:F10'],['后续任务','A1:I10'],['实施批次','A1:F13']]) {
    const view=await wb.render({sheetName:sheet,range,scale:1,format:'png'});
    await fs.writeFile(path.join(previews,`${sheet}.png`),new Uint8Array(await view.arrayBuffer()));
}
const summary=await wb.inspect({kind:'table',range:'实施批次!A4:F10',include:'values,formulas',tableMaxRows:7,tableMaxCols:6,maxChars:3000});
console.log(summary.ndjson);
const errors=await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:30},summary:'formula errors'});
console.log(errors.ndjson);
const expected=taskRows.reduce((sum,row)=>sum+row[6],0);
if (phases.getRange('E10').values[0][0] !== expected) throw new Error('Effort total mismatch');
await fs.mkdir(path.dirname(output),{recursive:true});
// artifact-tool 的诊断旁文件留在临时目录，仓库只保留交付工作簿。
const exportPath=path.join(previews,path.basename(output));
await (await SpreadsheetFile.exportXlsx(wb)).save(exportPath);
await fs.copyFile(exportPath,output);
console.log(`Exported ${output}; ${taskRows.length} planned tasks, ${expected} person-days`);
