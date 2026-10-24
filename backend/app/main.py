"""重介密控系统后端（前后端分离，coal_records 统一主表）"""
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from html import escape

from . import auth
from . import models  # noqa: F401  确保建表前模型已注册
from .telemetry import models as telemetry_models  # noqa: F401
from .hourly_brief import models as hourly_brief_models  # noqa: F401
from .decision_hourly import models as decision_hourly_models  # noqa: F401
from .storage_retention import models as storage_retention_models  # noqa: F401
from .storage_retention.router import router as retention_router
from .storage_retention.worker import lifespan as retention_lifespan
from .gpt_coarse import models as gpt_coarse_models  # noqa: F401
from .gpt_coarse.router import router as gpt_coarse_router
from .decision_hourly.worker import lifespan as decision_lifespan
from .routers.decision import router as decision_router
from .decision_hourly.router import router as evidence_router
from .config import APP_VERSION, FRONTEND_DIR, REMOTE_READONLY
from .database import Base, engine
from .observability import install_observability
from .routers import (assistant, health, manual_measurements,
                      samples, settings, state, telemetry, training)

from .schema_upgrade import upgrade_schema
from .telemetry.views import ensure_sqlite_views
from .remote_readonly.server import router as readonly_db_router, close_default_manager
from .remote_readonly.operations import router as readonly_operations_router
from .remote_readonly.startup import router as readonly_bootstrap_router

if not REMOTE_READONLY:
    Base.metadata.create_all(bind=engine)
    upgrade_schema(engine)
    ensure_sqlite_views(engine)


@asynccontextmanager
async def application_lifespan(app):
    if REMOTE_READONLY:
        # Local Python executes reads/calculations; production alone owns all writers.
        yield
    else:
        try:
            async with decision_lifespan(app):
                async with retention_lifespan(engine):
                    yield
        finally:
            close_default_manager()

app = FastAPI(
    title="重介密控系统",
    description="前后端分离后端：密度推荐 + 精煤灰分监控（coal_records 统一主表）",
    version=APP_VERSION,
    lifespan=application_lifespan,
)
install_observability(app)

# 2026-09 删除 CORSMiddleware(allow_origins=["*"])：
# 前端由本服务**同源**托管（见文件末尾 mount "/"），api.js 的 base 是相对路径 '/api/v1'，
# 前端永不跨源 → CORS 中间件实际无用，却把"任意来源可读写"当作默认暴露面。
# 将来前端若独立部署（换域名/端口），再按白名单显式开启。
#
# 2026-09-11 补上真正的访问控制（见 app/auth.py）：
#   · 所有写接口要 X-DMCS-Token（token 随首页以 <meta> 下发，浏览器零配置）；
#   · 本机回环免 token（现场脚本/E2E/pytest 从本机发起）；
#   · PUT /state?force=true（整库覆盖/清空）**只接受本机来源**，拿到 token 也不能从别的机器洗库。

app.include_router(health.router, prefix="/api/v1")
app.include_router(retention_router, prefix="/api/v1")
app.include_router(gpt_coarse_router, prefix="/api/v1")
app.include_router(decision_router, prefix="/api/v1")
app.include_router(evidence_router, prefix="/api/v1")
app.include_router(assistant.router, prefix="/api/v1")   # AI解读助手(只读)
app.include_router(settings.router, prefix="/api/v1")
app.include_router(samples.router, prefix="/api/v1")
app.include_router(manual_measurements.router, prefix="/api/v1")
app.include_router(state.router, prefix="/api/v1")
app.include_router(training.router, prefix="/api/v1")
app.include_router(telemetry.router, prefix="/api/v1")
if REMOTE_READONLY:
    app.include_router(readonly_operations_router, prefix='/api/v1')
    app.include_router(readonly_bootstrap_router, prefix='/api/v1')
if not REMOTE_READONLY:
    app.include_router(readonly_db_router,prefix='/api/v1')


@app.get('/api/v1/runtime-mode')
def runtime_mode():
    return dict(mode='readonly-test' if REMOTE_READONLY else 'production',
                data_server=REMOTE_READONLY['server_url'] if REMOTE_READONLY else None)

# 首页显式注册：**必须放在 mount("/") 之前**才能生效。
# 作用：把写接口用的 token 作为 <meta> 注入页面，浏览器端零配置即可带上
# X-DMCS-Token（见 frontend/js/api.js），操作员不需要手工输入任何东西。
# 其余静态资源仍由下面的 mount 提供。
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> HTMLResponse:
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    # 读 auth 模块属性（而不是 `from .auth import WRITE_TOKEN` 的值快照）：
    # 后者会把 token 固化在导入时刻，测试改不动、也无法在运行时换 token。
    if auth.auth_enabled():
        meta = f'<meta name="dmcs-token" content="{auth.WRITE_TOKEN}">'
        html = html.replace("<head>", "<head>\n    " + meta, 1)
    if REMOTE_READONLY:
        html = html.replace('<head>','<head><meta name="dmcs-readonly" content="true">',1)
        banner = ('<div id="dmcs-readonly-status" role="status" style="position:sticky;top:0;z-index:10000;padding:8px;'
                  'background:#fff1c7;color:#704b00;text-align:center;font-weight:bold">'
                  '测试服 · 本地前后端代码 · 主机数据只读：'+escape(REMOTE_READONLY['server_url'])+'</div>')
        html = html.replace('<body>','<body>'+banner,1)
    return HTMLResponse(html)


# 静态托管前端（同源，规避 file:// 与 CORS）；挂在所有 API 路由之后
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


@app.middleware("http")
async def no_cache_static_assets(request, call_next):
    """静态前端强制每次校验（ETag/Last-Modified 命中会返回 304，开销极小）。

    为什么必须加：index.html 没法用 `?v=N` 做缓存失效——JS/CSS 靠版本号参数换了 URL，
    但 HTML 自己的 URL 永远不变。StaticFiles 默认只发 ETag/Last-Modified、不发 Cache-Control，
    浏览器于是走**启发式缓存**（约取 Last-Modified 之后 10% 的时间当新鲜期），
    这期间直接吃本地副本、连校验请求都不发，表现为「前端改了但页面没更新」，
    只能靠用户手动 Ctrl+F5 —— 每次部署都要解释一遍。加了 no-cache 后：
    文件没变 → 304（几十字节）；文件变了 → 立刻拿到新的。

    只作用于静态资源；/api 不在此列（JSON 响应另有语义，且带 no-store 会禁用正常缓存策略）。
    """
    if (REMOTE_READONLY and request.method not in ('GET','HEAD','OPTIONS')
            and not (request.method == 'POST' and request.url.path == '/api/v1/decision/snapshots')):
        return JSONResponse({'detail':{'error_code':'readonly_test_server',
                             'message':'测试服只读主机数据；补录、设置、导入和采集操作请在生产主机执行'}},status_code=403)
    resp = await call_next(request)
    if not request.url.path.startswith("/api"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp
