"""重介密控系统后端（前后端分离，coal_records 统一主表）"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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
