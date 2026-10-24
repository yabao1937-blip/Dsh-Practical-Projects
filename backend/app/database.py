"""数据库连接与会话管理"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import QueuePool

from .config import DATABASE_URL, REMOTE_READONLY

_is_sqlite = DATABASE_URL.startswith("sqlite")

# timeout：SQLite 拿不到写锁时等待而不是立刻抛 database is locked（默认只有 5 秒）
if REMOTE_READONLY:
    from remote_sqlite import connect as remote_connect
    # Bound laptop concurrency below the host bridge's four-snapshot limit.
    # Check-in rollback closes the remote snapshot; idle pooled facades hold no data.
    engine = create_engine('sqlite://', creator=lambda: remote_connect(REMOTE_READONLY),
                           poolclass=QueuePool, pool_size=3, max_overflow=0,
                           pool_timeout=3, pool_reset_on_return='rollback')
else:
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False, "timeout": 15} if _is_sqlite else {},
    )
from .telemetry.metadata import install_codec
import os
# Deploy compatible readers first. Compact writes are a separate activation so
# a failed first update can roll source back without stranding newly saved data.
_point_storage = os.environ.get('DMCS_POINT_METADATA_STORAGE', 'legacy')
if _point_storage not in ('legacy', 'compact-v1'):
    raise ValueError('Unknown point metadata storage format')
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


if _is_sqlite and not REMOTE_READONLY:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        """SQLite 并发与可靠性设置（2026-09 加入）。

        背景：uvicorn 是多线程的，前端 saveStore 会整库 PUT，同时还有
        GET /state、/overview/dashboard 在读。默认 rollback journal 下读写互斥，
        并发时直接 `database is locked`（原实现只设了 check_same_thread=False，
        那只解决"跨线程使用同一连接"，不解决锁竞争）。

        - journal_mode=WAL：读不阻塞写、写不阻塞读（并发表现的关键）
        - busy_timeout=15000：等锁而不是立刻报错
        - synchronous=NORMAL：WAL 下的推荐档位（崩溃不损坏库，最多丢最后一个事务）

        注意：WAL 需要本地磁盘（网络盘/部分虚拟盘不支持），并会生成 .db-wal / .db-shm
        两个伴生文件（已加入 .gitignore）。设置失败只降级，不让服务起不来。
        """
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=15000")
            cur.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        finally:
            cur.close()


install_codec(engine, REMOTE_READONLY, compact_writes=_point_storage == 'compact-v1')


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
