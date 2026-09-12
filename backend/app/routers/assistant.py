"""AI 解读助手(只读)。

定位与硬约束:
- 职责 = 解释系统概念/算法/指标 + 解读实时数据快照;**不执行任何操作**。
- 实现上从结构上保证"只读":
  · LLM 没有任何工具/函数调用——输出只是文字,不存在执行路径;
  · 本路由只做 SELECT(load_store / coarse_models 查询),不写任何表、不落对话日志
    (对话历史只存浏览器 localStorage);
  · LLM 凭证只在服务端使用,浏览器永远拿不到 key。

LLM 配置(环境变量,请求时读取,重启后生效):
  ASSISTANT_API_KEY / ASSISTANT_BASE_URL / ASSISTANT_MODEL  —— 显式覆盖
  ZAI_API_KEY        → https://api.z.ai/api/coding/paas/v4 + glm-4.6(编程套餐通道)
  DEEPSEEK_API_KEY   → https://api.deepseek.com + deepseek-chat
均未配置时 /ask 返回 503 并给出配置指引;/status 返回 configured=false。
"""
import json
import os
import urllib.request
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import CoarseModel
from ..services import resolvers
from ..services.state import load_store

router = APIRouter(prefix="/assistant", tags=["AI解读助手(只读)"])

MAX_QUESTION = 2000
MAX_HISTORY = 6          # 送入模型的历史轮数(前端可存更多)
MAX_TOKENS = 1500
TIMEOUT_S = 120

# ---------------- 系统知识(静态,随版本更新) ----------------
KNOWLEDGE = """【系统知识】重介密控系统:选煤厂重介分选密度控制与精煤灰分监控。
· 总精煤灰分 = 重介精煤 + 浮精 + 粗精煤泥 按煤量加权;目标总灰分默认 8.50%(容差±0.1%,可调)。
· 三张数据表:表1粗精煤泥多因素(10特征:原煤灰分/带煤量/系统开关A·B·401·402/脱粉473·474/停机标志/精磁尾液位,目标=315灰分);
  表2浮精(灰分/煤量/压滤机);表3灰分密度(皮带501=总混配→在线总灰分;皮带502=仅重介精煤→在线重介灰分,是重介灰分权威在线来源)。
· 建议密度算法:灰分偏差→专家表(死区±0.05%不调;0.15%→调0.01;0.25%→调0.02;>0.25%按斜率0.1外推)→限幅[1.35,1.60],灰分偏高→降密度,可逐步自动执行(每步≤0.01)。
· 两版口径:总灰分版=实测总灰分−目标;重介版=目标重介灰分(由目标总灰分按煤量反推动态分解)与实测重介灰分比较。公式合成口径下两版数学等价。
· K=Δρ/ΔA(密度→重介灰分增益):现行经验值0.075(仅用于调密后预测展示);常规闭环操作数据不可辨识K,需密度阶跃实验标定(实验方案在docs/)。
· 粗精煤泥灰分模型:MLR(岭回归,LOOCV选λ)与PLS(选分量A)双模型,按Q²/q2Time选生产模型;R²=训练解释力,Q²=留一泛化,q2Time=时序前向预测力,合格率=|预测−实测|≤容差(0.8%)比例。
· 推测简报:三表齐全(各自最近记录距该小时≤24h)才成行;粗灰用递归软测量(实测锚定+预测增量)。
· 取值链通则:手动(有效期内) > 自动(仪表/公式/录入) > 默认。
· 决策日志:自动记录每次密度决策时的工况上下文与45分钟后的灰分响应,为K(工况)标定积累数据。
· 密度趋势与口径的已知边界:系统"当前密度"取自最新记录(非实时),现场调密度后需在系统内录入,否则死区判断会用旧密度。"""


def _llm_config():
    """按环境变量解析 (base_url, model, key);未配置返回 None。"""
    if os.environ.get("ASSISTANT_API_KEY"):
        return (os.environ.get("ASSISTANT_BASE_URL") or "https://api.z.ai/api/coding/paas/v4",
                os.environ.get("ASSISTANT_MODEL") or "glm-4.6",
                os.environ["ASSISTANT_API_KEY"])
    if os.environ.get("ZAI_API_KEY"):
        return ("https://api.z.ai/api/coding/paas/v4", "glm-4.6", os.environ["ZAI_API_KEY"])
    if os.environ.get("DEEPSEEK_API_KEY"):
        return ("https://api.deepseek.com", "deepseek-chat", os.environ["DEEPSEEK_API_KEY"])
    return None


@router.get("/status")
def assistant_status():
    cfg = _llm_config()
    return {"configured": cfg is not None,
            "provider": (cfg[0].split("//")[1].split("/")[0] if cfg else None),
            "model": (cfg[1] if cfg else None)}


# ---------------- 实时快照(只读查询) ----------------
def build_snapshot(db: Session) -> dict:
    store = load_store(db)
    g_state = {"rho_cur": resolvers.resolve_density(store),
               "heavy_ash": resolvers.get_heavy_ash(store),
               "actual_total": resolvers.resolve_total_ash(store),
               "scheme": "heavy" if store.get("guideScheme") == "heavy" else "total",
               "tol": store.get("ashTargetTol", 0.1)}
    from ..services.density import compute_density_guidance
    guide = compute_density_guidance(g_state, store.get("ashTarget", 8.50))

    model_info = None
    m = db.query(CoarseModel).filter(CoarseModel.is_current.is_(True)).first()
    if m is not None:
        met = m.metrics or {}
        model_info = {"method": m.method, "n": m.n, "range": m.train_range,
                      "trained_at": m.trained_at,
                      "r2": met.get("r2"), "q2": met.get("q2"), "q2Time": met.get("q2Time"),
                      "passRate": met.get("passRate")}

    from ..services.density_model import fit_density_gain
    k = fit_density_gain(store)

    snap = {
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "record_counts": {"coarse": len(store.get("coarseCoal") or []),
                          "float": len(store.get("floatCoal") or []),
                          "ash_density": sum(1 for l in store.get("calcLogs") or []
                                             if l.get("calc_type") == "ash_density")},
        "density_current": guide.get("rhoCur"),
        "heavy_ash": guide.get("heavyAsh"),
        "total_ash_actual": guide.get("actualTotal"),
        "target_total": store.get("ashTarget", 8.50),
        "guidance": {"scheme": guide.get("scheme"), "deltaA": guide.get("deltaA"),
                     "suggested_density": guide.get("rhoNew"), "direction": guide.get("direction"),
                     "reason": guide.get("reason")},
        "coarse_model": model_info,
        "k_gain": {"valid": k.get("valid"), "k": k.get("k"), "n": k.get("n"),
                   "source": k.get("source")},
        "decision_log_tail": [
            {"ts": e.get("ts"), "trigger": e.get("trigger"),
             "rhoCur": e.get("rhoCur"), "rhoNew": e.get("rhoNew"),
             "responded": bool(e.get("response"))}
            for e in (store.get("densityDecisionLog") or [])[-5:]],
    }
    return snap


SYSTEM_PROMPT = """你是「重介密控系统」的解读助手。你只做解释,不做操作。

规则:
1. 你没有任何工具,不能执行任何操作,也绝不能假装已经执行;不要输出"我已修改/已为你调整"之类的话。
2. 可以解释:系统概念、算法原理(专家表/两版口径/K/MLR/PLS/R²/Q²/简报规则/取值链)、
   指标含义、实时快照里每个数的意义、页面功能与数据来源。
3. 回答基于【系统知识】与【实时快照】,数字引用以快照为准;不确定就明说;涉及现场工艺判断时建议咨询工艺人员。
4. 中文回答,简洁分点,面向选煤厂工程师;不要输出 HTML,只输出纯文本(Markdown 列表可用)。
5. 用户若要求你执行操作(改数据/调密度/训练模型),说明你只负责解释,并告诉他对应功能在哪个页面由他自己操作。"""


class AskIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=MAX_QUESTION)
    history: list[dict] = Field(default=[], max_length=MAX_HISTORY * 2)


@router.post("/ask")
def ask(body: AskIn, db: Session = Depends(get_db)):
    cfg = _llm_config()
    if cfg is None:
        raise HTTPException(503, "AI 助手未配置 LLM 凭证:请设置环境变量 ZAI_API_KEY(或 "
                                 "ASSISTANT_API_KEY/ASSISTANT_BASE_URL/ASSISTANT_MODEL)后重启后端")
    base_url, model, key = cfg
    snap = build_snapshot(db)
    msgs = [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": KNOWLEDGE + "\n\n【实时快照】\n" +
             json.dumps(snap, ensure_ascii=False, default=str)}]
    for h in (body.history or [])[-MAX_HISTORY * 2:]:
        role = h.get("role")
        content = str(h.get("content") or "")[:2000]
        if role in ("user", "assistant") and content:
            msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": body.question})

    payload = json.dumps({"model": model, "messages": msgs, "stream": True,
                          "max_tokens": MAX_TOKENS, "temperature": 0.3}).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT_S)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:200]
        raise HTTPException(502, f"LLM 服务返回错误 HTTP {e.code}: {detail}")
    except Exception as e:
        raise HTTPException(502, f"无法连接 LLM 服务: {e}")

    def gen():
        try:
            for raw in resp:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0]["delta"].get("content")
                except Exception:
                    continue
                if delta:
                    yield delta
        finally:
            resp.close()

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")
