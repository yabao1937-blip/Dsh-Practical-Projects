"""整库快照（GET /state 读取、PUT /state 整体重写，供前端数据层切换）"""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from ..auth import require_force_allowed, require_write
from ..database import get_db
from ..services import migrate
from ..services.state import load_store

router = APIRouter(prefix="/state", tags=["状态"])


@router.get("")
def get_state(db: Session = Depends(get_db)):
    """返回前端 App.store 等价结构（DB → store 桥接）"""
    return load_store(db)


@router.put("", dependencies=[Depends(require_write)])
async def put_state(request: Request, store: dict,
                    force: bool = Query(False, description="跳过防回退守卫(清空/恢复备份用)")):
    """整体重写库（前端 saveStore 提交整库快照）。

    默认带防回退守卫：入库记录数少于现库时拒绝（旧浏览器镜像不能洗掉服务器新数据）。
    写权限：需要 X-DMCS-Token（本机回环可免）；
    **force=true 只接受本机来源** —— 整库覆盖/清空不能从同网段别的机器发起。
    """
    if force:
        await require_force_allowed(request)
    return migrate.replace(store, force)
