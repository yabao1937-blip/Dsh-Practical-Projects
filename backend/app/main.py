"""重介密控系统后端（前后端分离，coal_records 统一主表）"""
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import auth
from . import models  # noqa: F401  确保建表前模型已注册
from .config import FRONTEND_DIR
from .database import Base, engine
from .routers import (health, import_api, inputs, manual_entries, migration, overview,
                      records, samples, settings, state, training)

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="重介密控系统",
    description="前后端分离后端：密度推荐 + 精煤灰分监控（coal_records 统一主表）",
    version="0.2.0",
)

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
app.include_router(migration.router, prefix="/api/v1")
app.include_router(settings.router, prefix="/api/v1")
app.include_router(inputs.router, prefix="/api/v1")
app.include_router(records.router, prefix="/api/v1")
app.include_router(samples.router, prefix="/api/v1")
app.include_router(manual_entries.router, prefix="/api/v1")
app.include_router(overview.router, prefix="/api/v1")
app.include_router(state.router, prefix="/api/v1")
app.include_router(import_api.router, prefix="/api/v1")
app.include_router(training.router, prefix="/api/v1")

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
    resp = await call_next(request)
    if not request.url.path.startswith("/api"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp
