"""Run the locked GPT coal-response experiment. Production database is read-only.

  backend/.venv/Scripts/python.exe -X utf8 backend/scripts/evaluate_gpt_coal_response.py
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
from app.gpt_coarse.experiments import coal_response as cr
from app.gpt_coarse.experiments.coal_response_evaluation import historical_evaluation, recent_evaluation

OUT = ROOT/'docs/verification/gpt-coal-response-20261013'
OLD = ROOT/'backend/models/gpt_expert9/20261009.json'
RECENT = ROOT/'backend/data/gpt_coarse/recent_assays/20261013/joined.json'
PROTOCOL = OUT/'protocol.json'
ARTIFACT = ROOT/'backend/models/gpt_coal_response/20261013/offline.json'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')


def protected_state():
    paths = [ROOT/'backend/data/train_js.json', ROOT/'backend/models/coarse_slime_ash_model.json',
             ROOT/'frontend/js/app.js', ROOT/'frontend/js/gpt-expert9.js', OLD,
             ROOT/'backend/models/gpt_expert8/20261012/model.json',
             ROOT/'backend/models/gpt_expert8/20261012/production.json']
    paths += sorted((ROOT/'backend/app/services').glob('*.py'))
    paths += sorted((ROOT/'backend/app/gpt_coarse').glob('*.py'))
    state = dict(files={p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in paths})
    with sqlite3.connect((ROOT/'backend/data/dense_medium.db').as_uri()+'?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        state['tables'] = {}
        for table in ('coarse_models', 'regression_models', 'coarse_model_history', 'gpt_coarse_model_revisions'):
            records = db.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()
            state['tables'][table] = dict(rows=len(records), sha256=digest(json.dumps(records, sort_keys=True).encode()))
        current = db.execute('SELECT id, artifact_id, activated_at, artifact FROM gpt_coarse_model_revisions '
                             'ORDER BY activated_at DESC, id DESC LIMIT 1').fetchone()
    artifact = json.loads(current[3])
    frozen = json.loads((ROOT/'backend/models/gpt_expert8/20261012/model.json').read_text(encoding='utf-8'))
    if artifact['model'] != frozen['model']:
        raise ValueError('Current production EXPERT8 differs from the frozen baseline')
    state['active_model'] = dict(revision=current[0], artifact_id=current[1], activated_at=current[2])
    return state


LABELS = dict(EXPERT8='现用 EXPERT8 方法', mean='训练均值（参照）', bias='已出化验偏差修正',
              K1='单条液位曲线', K2='两类液位响应', K3='三类液位响应')


def render(report):
    selected = report['selected']
    stages = [('历史拟合', report['historical_fit']['results']),
              ('三折调参验证', report['tuning_validation']['pooled']),
              ('历史向前检验', report['forward_time_check']['results']),
              ('近期近似样本', report['recent_exploration']['results'])]
    tables = ''
    for stage, results in stages:
        lines = []
        for family, config in selected.items():
            m = results[config['name']]
            m = m.get('metrics', m)
            lines.append(f'<tr><th>{LABELS[family]}</th><td>{m["n"]}</td><td>{m["mae"]:.3f}</td>'
                         f'<td>{m["day_balanced_mae"]:.3f}</td><td>{m["rmse"]:.3f}</td><td>{m["bias"]:+.3f}</td>'
                         f'<td>{m["classified_n"]}</td><td>{m["fallback_n"]}</td></tr>')
        tables += f'<h2>{stage}</h2><div class="scroll"><table><thead><tr><th>方法</th><th>样本</th><th>MAE</th>' \
                  '<th>日期均衡 MAE</th><th>RMSE</th><th>高估/低估</th><th>判定类型</th><th>回退K1</th></tr></thead><tbody>' \
                  +''.join(lines)+'</tbody></table></div>'
    folds = ''.join(f'<tr><td>{i+1}</td><td>{f["train_n"]} / {f["nominal_train_n"]}</td>'
                    f'<td>{f["training_max_available_at"][:16]}</td><td>{f["validation_first_time"][:16]}<br>'
                    f'{f["validation_last_time"][:16]}</td><td>{f["validation_n"]}</td></tr>'
                    for i, f in enumerate(report['tuning_validation']['folds']))
    model = report['historical_fit']['model']
    curves = ''
    for family in ('K1', 'K2', 'K3'):
        m = model['curves'][selected[family]['name']]
        for i, (curve, support) in enumerate(zip(m['curves'], m['support'])):
            a, b = curve
            curves += f'<tr><th>{LABELS[family]} · 响应{i+1}</th><td>{a-b:.2f}</td><td>{a:.2f}</td><td>{a+b:.2f}</td>' \
                      f'<td>{support["rows"]} / {support["days"]}</td><td>{support["level_range"]}</td>' \
                      f'<td>{"足够" if support["supported"] else "不足"}</td>' \
                      f'<td>{"已收敛" if m["converged"] else "未收敛，不用于分类"}</td></tr>'
    evidence = ''.join('<li>'+LABELS[k]+f'：滚动验证较K1改善 {v["validation_gain_over_K1"]*100:.1f}%，' \
                      f'{v["improved_folds"]}/3折改善；历史向前检验{ "改善" if v["forward_improved"] else "没有改善" }；' \
                      f'工程门槛{ "通过" if v["meets_engineering_gate"] else "未通过" }。</li>'
                      for k, v in report['complexity_evidence'].items())
    recent = report['recent_exploration']['results']
    sample_rows = ''
    for i, p in enumerate(recent[selected['EXPERT8']['name']]['predictions']):
        values = [recent[selected[f]['name']]['predictions'][i]['value'] for f in ('EXPERT8', 'bias', 'K1', 'K2', 'K3')]
        sample_rows += '<tr>'+''.join(f'<td>{t}</td>' for t in [p['sample_at'][:16].replace('T', ' '),
                       f'{p["level"]:.2f}', f'{p["measured"]:.2f}', *[f'{v:.2f}' for v in values]])+'</tr>'
    notes = ''.join(f'<li>{escape(n)}</li>' for n in report['limitations'])
    trials = ''.join(f'<tr><td>{escape(name)}</td><td>{m["day_balanced_mae"]:.4f}</td>'
                     f'<td>{m["classified_n"]}</td><td>{m["fallback_n"]}</td></tr>'
                     for name, m in report['tuning_validation']['pooled'].items())
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>粗精煤泥灰分 · 煤质响应离线比较</title><style>
body{{margin:36px auto;max-width:1140px;padding:0 24px 48px;background:#f5f4f0;color:#283551;font:16px/1.65 "Microsoft YaHei",sans-serif}}
h1{{font-size:30px;margin:12px 0}}h2{{font-size:21px;margin-top:32px}}header{{border-top:5px solid #333af0;padding-top:18px}}
.tag{{color:#333af0;font-size:14px}}.panel{{background:white;border:1px solid #d5dae5;padding:18px 24px;margin:20px 0}}
.scroll{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;background:white;font-size:14px}}th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid #dce0ea;white-space:nowrap}}thead{{background:#e9e9fa}}tbody th{{font-weight:500}}
.note{{color:#68758b}}a{{color:#333af0}}li{{margin:8px 0}}code{{font-size:13px;word-break:break-all}}summary{{cursor:pointer;color:#333af0}}
</style><header><span class="tag">GPT 独立离线试验 · 现用模型未切换</span><h1>是否需要引入“煤质响应类型”？</h1>
<p>{escape(report['conclusion'])}</p></header>
<div class="panel"><b>判断标准</b><p>首先检验单条液位曲线是否就能解释偏差，再看两类、三类响应有没有额外收益。
八个输入保持相同，其他七因子的修正共用各训练窗重新拟合的 EXPERT8。类型由此前已出结果的化验推断，不能用正在预测的灰分反推类型。</p><ul>{evidence}</ul></div>
<p class="note">以下误差单位为灰分百分点，越低越好；高估/低估为预测减化验。日期均衡 MAE 让不同日期权重相同。
K2/K3回退时使用K1，回退带来的改善不算煤质识别收益。历史资料均已参与过开发，本报告没有新的独立现场验收数据。</p>
{tables}
<h2>近期五条：逐条查看</h2><div class="scroll"><table><thead><tr><th>北京时间</th><th>提前液位</th><th>化验</th><th>EXPERT8</th><th>偏差修正</th><th>K1</th><th>K2</th><th>K3</th></tr></thead><tbody>{sample_rows}</tbody></table></div>
<p class="note">原表21条只有5条能近似配对，严格30秒匹配为0条。非液位读数距采样46–57秒；液位实际领先约109–119秒。
这五条全部是A关、B开、401开、402开、两台脱粉关，旧训练表A关仅1条；误差既可能来自曲线，也可能来自工况差异和配对误差。</p>
<h2>拟合曲线与样本支持</h2><div class="scroll"><table><thead><tr><th>模型 / 响应</th><th>液位25</th><th>液位50</th><th>液位75</th><th>样本/日期</th><th>训练液位范围</th><th>支持</th><th>拟合状态</th></tr></thead><tbody>
<tr><th>原专家基准</th><td>23.00</td><td>15.50</td><td>8.00</td><td>—</td><td>25–75</td><td>经验基准</td><td>—</td></tr>{curves}</tbody></table></div>
<p class="note">上表为全量历史拟合的曲线部分，尚未加其他七因子的±2修正；不是物理煤种鉴定，也不是置信区间。
新曲线将两个专家点当作软约束，可能明显偏离；这里只作对照，不自动改变专家经验或现场设置。</p>
<details><summary>所有候选的调参结果（含未入选候选）</summary><p>同一族按日期均衡MAE选择；多类候选如果一直回退K1，它与K1持平并不能证明分类有效。
弱先验的两类候选确有部分样本产生类型判断，也一并列出，不能只展示最终回退方案。</p><table><thead><tr><th>候选</th><th>日期均衡MAE</th><th>类型判定条数</th><th>回退K1条数</th></tr></thead><tbody>{trials}</tbody></table></details>
<details><summary>时间验证与取数口径</summary><p>旧记录按生产日+2天00:00才视为化验可用，保守覆盖午夜日期不明的记录。
每折训练先剔除验证开始时尚不可用的标签；验证期仅校准状态可随已出化验更新，模型系数冻结。
近期记录按实际采样次日00:00模拟化验可用，不声称系统当时已收到结果。证据超过14天失效。</p>
<div class="scroll"><table><thead><tr><th>折</th><th>实际/名义训练条数</th><th>最晚标签可用</th><th>验证区间</th><th>验证条数</th></tr></thead><tbody>{folds}</tbody></table></div>
<p>历史向前检验：实际训练{report['forward_time_check']['train_n']}条，名义训练{report['forward_time_check']['nominal_train_n']}条；尾部{report['forward_time_check']['validation_n']}条评分。</p></details>
<h2>适用限制与下一步</h2><ul>{notes}</ul><p>下一阶段需要同步保存采样时间、化验出结果/录入时间、提前90秒液位及其质量、其他七因子，以及煤源/煤批（若可得）。先做旁路预测，累计新的完整样本后再决定是否替换在线模型。</p>
<p><a href="report.json">完整评分与逐条证据 JSON</a> · <a href="protocol.json">首次拟合前固定的试验协议</a></p>
<p class="note">生成于 {report['generated_at']}；文件与模型表前后指纹一致：{report['protected_unchanged']}。</p></html>'''


def main():
    before = protected_state()
    protocol_hash = digest(PROTOCOL.read_bytes())
    artifact = json.loads(OLD.read_text(encoding='utf-8'))
    rows = cr.historical_rows(artifact['rows'])
    print('Fitting historical temporal windows; recent assays excluded from selection.', flush=True)
    report = historical_evaluation(rows)
    report['recent_exploration'] = recent_evaluation(json.loads(RECENT.read_text(encoding='utf-8')), rows, report)
    passed = [k for k, v in report['complexity_evidence'].items() if v['meets_engineering_gate']]
    report['conclusion'] = ('多类曲线在这次历史重放达到工程比较门槛，但尚不能证明是实际煤质，也不足以切换在线模型。' if passed else
                            '这次比较尚不支持把数据可靠地分成不同煤质。先保留单曲线或偏差修正候选，在线继续使用现有 EXPERT8。')
    report['limitations'] = [
        '177条历史记录已经参与开发；拟合、三折调参和历史向前检验分别报告，均不是新的独立验收。',
        '固定液位基准的系统偏差、开停机组合、采样误差可能被聚类误认为煤质；不能把统计分组叫作已确认煤种。',
        '近期21条仅5条近似完整、0条严格完整，且新工况在旧表极少；五条不参加任何参数选择或模型拟合。',
        '历史化验报告时间未知，可用时间是保守假设；后续真实录入不能回填为采样时刻。',
        '候选响应类型需至少15条/6天/液位跨度15的训练支持，门控需14天内至少3条已出化验、跨度10、后验≥0.75；不足或外推回退K1。',
        '后验概率只是模型内相对证据，未经概率校准，不能解释为真实煤质置信度。',
        '专家点75→8、25→23在候选曲线中是软先验；偏离幅度必须结合现场实验核实，不能直接转为调密依据。',
        '未修改GPT在线预测、手工补录参与方式、建议密度、DS模型、DS对拍数据、共享求解器或页面。',
    ]
    after = protected_state()
    if before != after or protocol_hash != digest(PROTOCOL.read_bytes()):
        raise ValueError('Protected state or locked protocol changed while evaluating')
    report.update(generated_at=datetime.now(timezone.utc).isoformat(), status='offline_only_not_activated',
                  provenance=dict(protocol_sha256=protocol_hash, historical_sha256=digest(OLD.read_bytes()),
                                  recent_join_sha256=digest(RECENT.read_bytes()),
                                  experiment_sources={p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in (
                                      Path(__file__), ROOT/'backend/app/gpt_coarse/experiments/coal_response.py',
                                      ROOT/'backend/app/gpt_coarse/experiments/coal_response_evaluation.py')}),
                  protected_before=before, protected_after=after, protected_unchanged=True)
    write_json(OUT/'report.json', report)
    write_json(ARTIFACT, dict(kind=cr.KIND, status='offline_only_not_activated', protocol_sha256=protocol_hash,
                             selected=report['selected'], model=report['historical_fit']['model']))
    (OUT/'report.html').write_text(render(report), encoding='utf-8')
    print(json.dumps(dict(selected=report['selected'], complexity=report['complexity_evidence'],
                          forward={k:v['metrics'] for k,v in report['forward_time_check']['results'].items()},
                          recent={k:v['metrics'] for k,v in report['recent_exploration']['results'].items()},
                          protected_unchanged=True), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
