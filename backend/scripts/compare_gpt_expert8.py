"""Train an offline eight-factor candidate and paired report; never activates it.

Run from the repository root:
  backend/.venv/Scripts/python.exe backend/scripts/compare_gpt_expert8.py
"""
from datetime import datetime, timezone
import hashlib
from html import escape
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'backend'))

from app.gpt_coarse.experiments import expert8
from app.gpt_coarse.experiments.comparison import compare

ORIGINAL = ROOT/'backend/models/gpt_expert9/20261009.json'
MODEL_PATH = ROOT/'backend/models/gpt_expert8/20261012/model.json'
REPORT_DIR = ROOT/'docs/verification/gpt-expert8-20261012'
DATABASE = ROOT/'backend/data/dense_medium.db'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def serialized(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')


def protected_state():
    paths = [ORIGINAL, ROOT/'backend/data/train_js.json', ROOT/'backend/models/coarse_slime_ash_model.json',
             ROOT/'frontend/js/app.js', ROOT/'frontend/js/gpt-expert9.js']
    paths += sorted((ROOT/'backend/app/services').glob('*.py'))
    paths += sorted((ROOT/'backend/app/gpt_coarse').glob('*.py'))
    result = dict(files={p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in paths})
    # Do not open ORM sessions or hash the whole growing telemetry database.
    with sqlite3.connect(DATABASE.as_uri()+'?mode=ro', uri=True) as db:
        db.execute('BEGIN')
        result['model_tables'] = {}
        for table in ('coarse_models', 'regression_models', 'coarse_model_history', 'gpt_coarse_model_revisions'):
            data = db.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()
            result['model_tables'][table] = dict(rows=len(data), sha256=digest(serialized(data)))
        current = db.execute('SELECT id, artifact_id, activated_at, artifact FROM gpt_coarse_model_revisions '
                             'ORDER BY activated_at DESC, id DESC LIMIT 1').fetchone()
    if not current:
        raise ValueError('没有已激活 GPT 模型，无法确认现用对比对象')
    result['active_model'] = dict(revision=current[0], artifact_id=current[1], activated_at=current[2])
    if json.loads(current[3]) != json.loads(ORIGINAL.read_text(encoding='utf-8')):
        raise ValueError('数据库现用 GPT 模型与冻结文件不同，停止比较以免选错基线')
    return result


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')


def render(report, model):
    h, v, f = report['historical_fit'], report['tuning_validation']['main'], report['forward_time_check']
    score_rows = ''.join(
        f'<tr><th>{label}</th><td>{section["old"]["n"]}</td>'
        f'<td>{section["old"]["mae"]:.4f}</td><td>{section["new"]["mae"]:.4f}</td>'
        f'<td>{section["delta_new_minus_old"]["mae"]:+.4f}</td>'
        f'<td>{section["old"]["rmse"]:.4f}</td><td>{section["new"]["rmse"]:.4f}</td></tr>'
        for label, section in [('历史拟合（全量训练后回算）', h), ('滚动验证（3折合并）', v), ('历史向前检验（前142条训练）', f)])
    fold_rows = ''.join(
        f'<tr><td>{fold["train_end"]}</td><td>{fold["validation_start"]} — {fold["validation_end"]}</td>'
        f'<td>{fold["train_n"]} / {fold["validation_n"]}</td>'
        f'<td>{fold["old"]["mae"]:.4f}</td><td>{fold["new"]["mae"]:.4f}</td></tr>' for fold in v['folds'])
    alpha_rows = ''.join(
        f'<tr><td>{trial["alpha"]}</td><td>{trial["old"]["day_balanced_mae"]:.4f}</td>'
        f'<td>{trial["new"]["day_balanced_mae"]:.4f}</td></tr>' for trial in report['tuning_validation']['trials'])
    changes = sorted(h['predictions'], key=lambda p: abs(p['new_minus_old']), reverse=True)[:8]
    examples = ''.join(
        f'<tr><td>{p["source_row"]}</td><td>{escape(p["timestamp"])}</td><td>{p["measured"]:.2f}</td>'
        f'<td>{p["old"]:.3f}</td><td>{p["new"]:.3f}</td><td>{p["new_minus_old"]:+.3f}</td></tr>' for p in changes)
    limitations = ''.join(f'<li>{escape(t)}</li>' for t in report['limitations'])
    sha = report['provenance']['source_workbook_sha256']
    return f'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>粗精煤泥灰分 · 八因子与九因子对比</title>
<style>
:root{{font-family:"Microsoft YaHei",system-ui,sans-serif;color:#283551;background:#f4f3ef;line-height:1.7}}
body{{max-width:1120px;margin:40px auto;padding:0 24px 48px}}header{{border-top:5px solid #343cf4;padding-top:18px}}
h1{{font-size:28px;margin:8px 0 16px}}h2{{font-size:20px;margin-top:32px}}p{{margin:10px 0}}
.tag{{color:#333bed;background:#e5e5fc;padding:5px 12px;font-size:13px;border-radius:4px}}
.lead{{font-size:18px}}.note{{color:#68748c}}.panel{{padding:18px 22px;background:white;border:1px solid #d5d9e4;border-radius:6px;margin:18px 0}}
.tables{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:14px;background:white}}
td,th{{padding:12px 14px;border-bottom:1px solid #dce0eb;text-align:left;white-space:nowrap}}
thead th{{background:#ececf9;color:#37406e}}tbody th{{font-weight:500}}code{{font-size:13px;overflow-wrap:anywhere}}
a{{color:#333bed}}details summary{{cursor:pointer;color:#333bed}}li{{margin:6px 0}}.small{{font-size:13px}}
@media(max-width:700px){{body{{margin:18px auto;padding:0 14px}}h1{{font-size:23px}}}}
</style>
<header><span class="tag">离线候选 · 未切换现用模型</span>
<h1>粗精煤泥灰分：去掉原煤灰分后的效果对比</h1>
<p class="lead">整体效果基本持平。历史拟合略差，滚动验证与历史向前检验略好；差异不足以声称精度提升。</p>
<p class="note">训练时间：{escape(report['trained_at'])} · 对比对象：现用 GPT 九因子专家模型 · 候选：GPT 八因子专家模型</p></header>
<div class="panel"><strong>这次只改变一个条件</strong>
<p>移除原煤灰分。保留小时带煤量、A/B/401/402 系统、473/474 脱粉、精磁尾液位，共 8 个输入。</p>
<p>仍以液位 25 → 灰分 23%、75 → 8% 为典型工况基准，其他因素合计最多修正 ±2 个百分点。
表中液位已提前 90 秒，本次训练没有再次移位。</p>
<p>保留原有 177 条正常生产记录（51 个日期）、同一训练/验证日期划分、日期均权、Huber 损失与岭参数 0.1。
原表 185 条中的 8 条停机记录继续排除。两版都不把历史315化验结果作为输入。</p></div>
<h2>三种评价分别报告</h2>
<p class="note">MAE 为平均绝对误差，RMSE 对较大误差更敏感，单位均为“灰分百分点”；越小越好。差值 = 新版 − 旧版。</p>
<div class="tables"><table><thead><tr><th>评价口径</th><th>样本</th><th>旧版 MAE</th><th>新版 MAE</th><th>MAE 差值</th><th>旧版 RMSE</th><th>新版 RMSE</th></tr></thead><tbody>{score_rows}</tbody></table></div>
<p>历史向前检验：用截至 {f['train_end']} 的 {f['train_n']} 条记录训练，预测 {f['start']} 至 {f['end']} 的 {f['new']['n']} 条。
两版误差不超过 ±1 个百分点的比例均为 {f['new']['pass1']:.2f}%。</p>
<p>全量历史样本上，新旧预测平均绝对变化 <strong>{h['prediction_change']['mean_absolute']:.3f}</strong> 个百分点，最大变化
<strong>{h['prediction_change']['maximum_absolute']:.3f}</strong> 个百分点。原煤灰分在这套专家基准模型中的边际作用较小，不能据此推断它在生产中没有影响。</p>
<p class="note">同一历史向前区间，纯专家基准 MAE {f['expert_only']['mae']:.3f}，训练窗口标签均值基准 MAE {f['training_mean']['mae']:.3f}。
这些基线与 R² 等完整指标见 JSON；当前数据并不支持宣称模型已具有较高现场精度。</p>
<h2>切分与参数保持一致</h2>
<div class="tables"><table><thead><tr><th>训练截止</th><th>验证日期</th><th>训练 / 验证条数</th><th>旧版 MAE</th><th>新版 MAE</th></tr></thead><tbody>{fold_rows}</tbody></table></div>
<p>补充复核原参数网格：两版均选择 0.1。主对比固定使用现用模型的 0.1，没有为了改善八因子成绩额外调参。</p>
<div class="tables"><table><thead><tr><th>岭参数</th><th>旧版日期均权验证 MAE</th><th>新版日期均权验证 MAE</th></tr></thead><tbody>{alpha_rows}</tbody></table></div>
<h2>使用边界与后续判断</h2><ul>{limitations}</ul>
<p>建议保留当前线上九因子模型。八因子版本已独立保存，待收集新的现场化验结果后，在同一批新样本上并行比较，再决定是否替换。</p>
<details class="panel"><summary>查看历史拟合中变化最大的 8 条记录</summary><div class="tables"><table><thead><tr><th>Excel 行号</th><th>采样时间</th><th>表中灰分</th><th>旧预测</th><th>新预测</th><th>变化</th></tr></thead><tbody>{examples}</tbody></table></div></details>
<h2>产物与复现</h2>
<p><a href="../../../backend/models/gpt_expert8/20261012/model.json">八因子候选模型</a> · <a href="comparison.json">完整指标、逐条预测和训练窗口模型</a></p>
<p class="small">在项目根目录执行：<code>backend/.venv/Scripts/python.exe backend/scripts/compare_gpt_expert8.py</code></p>
<p class="small">输入取自现用模型保存的冻结样本，原始 Excel SHA-256 已核对一致：<code>{sha}</code>。</p>
<p class="small">运行前后已核对现用 GPT 模型、DS 模型表及保存模型、DS 原版对拍基线和共享训练代码的摘要保持一致。
脚本只读 SQLite，没有模型激活操作；后台服务、采集和页面无需重启或修改。</p>
</html>'''


def main():
    before = protected_state()
    artifact = json.loads(ORIGINAL.read_text(encoding='utf-8'))
    source = Path(artifact['source']['source'])
    actual_sha = digest(source.read_bytes())
    if actual_sha != artifact['source']['sha256']:
        raise ValueError('原始 Excel 已变化；本轮必须使用与现用模型一致的源数据')
    model, report = compare(artifact)
    after = protected_state()
    if before != after:
        raise ValueError('比较期间生产模型或保护文件发生变化，停止保存并检查')
    timestamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    report.update(trained_at=timestamp,
                  provenance=dict(original_artifact=ORIGINAL.relative_to(ROOT).as_posix(),
                                  original_artifact_sha256=digest(ORIGINAL.read_bytes()),
                                  source_workbook=str(source), source_workbook_sha256=actual_sha,
                                  accepted_rows=len(artifact['rows']), original_audit=artifact['source']),
                  isolation=dict(unchanged=True, before=before, after=after))
    candidate = dict(trained_at=timestamp, status='candidate_only_not_activated', model=model,
                     source=report['provenance'],
                     level_alignment='Workbook rows already use level at t-90s; do not shift again.',
                     report='docs/verification/gpt-expert8-20261012/comparison.json',
                     rows=[{k: v for k, v in row.items() if k != 'raw_ash'} for row in artifact['rows']])
    write_json(MODEL_PATH, candidate)
    report['candidate_artifact_sha256'] = digest(MODEL_PATH.read_bytes())
    write_json(REPORT_DIR/'comparison.json', report)
    (REPORT_DIR/'comparison.html').write_text(render(report, model), encoding='utf-8')
    print(json.dumps(dict(model=str(MODEL_PATH), report=str(REPORT_DIR/'comparison.html'),
                          status=candidate['status'], protected_state_unchanged=True,
                          historical={k: report['historical_fit'][k] for k in ('old', 'new')},
                          forward={k: report['forward_time_check'][k] for k in ('old', 'new')}), ensure_ascii=False))


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    main()
