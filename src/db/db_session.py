"""
数据库会话管理模块

功能:
    - SQLite/MySQL双数据库模式，通过TM_DB_TYPE环境变量一键切换
    - 引擎与Session工厂懒加载+进程级单例，避免重复建连开销
    - session_scope上下文管理器: 自动提交、异常回滚、用后关闭
    - init_db建表方法: 开发环境快速初始化全部表结构
    - health_check连通性检查: Web平台健康探活与部署前自检

使用示例:
    from src.db.db_session import DatabaseSession
    from src.db.models import TestCase

    # 方式一: 上下文管理器（推荐，自动提交/回滚/关闭）
    with DatabaseSession.session_scope() as session:
        case = TestCase(case_id="TM-API-0001", name="登录接口校验")
        session.add(case)

    # 方式二: 手动管理
    session = DatabaseSession.get_session()
    try:
        session.query(TestCase).filter_by(case_id="TM-API-0001").first()
        session.commit()
    finally:
        session.close()
"""

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional
from urllib.parse import quote_plus

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from src.common.env_manager import env_manager
from src.common.logger import LogManager

logger = LogManager.get_logger()

# 项目根目录: 本模块位于 src/db/ 下，向上两级即为项目根
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class DatabaseSession:
    """
    数据库会话管理器

    管理数据库引擎与Session工厂的进程级唯一实例，
    屏蔽SQLite/MySQL差异，对上层提供统一的数据访问入口。

    属性:
        _engine (Engine | None): 数据库引擎单例（懒加载）
        _session_factory (sessionmaker | None): Session工厂单例（懒加载）
        _lock (threading.Lock): 初始化锁，保证多线程首次建连安全
    """

    _engine: Optional[Engine] = None
    _session_factory: Optional[sessionmaker] = None
    _lock = threading.Lock()

    # ------------------------------------------------------------------
    # 连接配置
    # ------------------------------------------------------------------
    @classmethod
    def _build_db_url(cls) -> str:
        """
        根据环境配置构建数据库连接URL

        配置读取规则:
            - TM_DB_TYPE=sqlite: 使用TM_DB_SQLITE_PATH路径（默认output/testmatrix.db）
            - TM_DB_TYPE=mysql:  组装TM_DB_MYSQL_*系列配置为PyMySQL连接串

        参数:
            无

        返回:
            str: SQLAlchemy连接URL
                 sqlite示例: sqlite:///D:/projects/TestMatrix/output/testmatrix.db
                 mysql示例:  mysql+pymysql://user:pass@127.0.0.1:3306/testmatrix?charset=utf8mb4

        异常:
            ValueError: TM_DB_TYPE非法（非sqlite/mysql）时抛出
        """
        db_type = str(env_manager.get("TM_DB_TYPE", "sqlite")).lower()

        if db_type == "sqlite":
            # SQLite: 相对路径基于项目根解析，目录不存在时自动创建
            db_path = env_manager.get("TM_DB_SQLITE_PATH", "output/testmatrix.db")
            path = Path(db_path)
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            path.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite:///{path.as_posix()}"
            logger.debug(f"数据库URL构建完成[SQLite] | 文件: {path}")
            return url

        if db_type == "mysql":
            # MySQL: 密码含特殊字符时需URL编码，防止连接串解析错乱
            host = env_manager.get("TM_DB_MYSQL_HOST", "127.0.0.1")
            port = env_manager.get_int("TM_DB_MYSQL_PORT", 3306)
            user = env_manager.get("TM_DB_MYSQL_USER", "root")
            password = env_manager.get("TM_DB_MYSQL_PASSWORD", "")
            database = env_manager.get("TM_DB_MYSQL_DATABASE", "testmatrix")
            url = (
                f"mysql+pymysql://{user}:{quote_plus(password)}@{host}:{port}/"
                f"{database}?charset=utf8mb4"
            )
            logger.debug(f"数据库URL构建完成[MySQL] | 目标: {host}:{port}/{database}")
            return url

        raise ValueError(
            f"非法数据库类型: {db_type}，TM_DB_TYPE仅支持 sqlite / mysql"
        )

    # ------------------------------------------------------------------
    # 引擎与会话管理
    # ------------------------------------------------------------------
    @classmethod
    def get_engine(cls) -> Engine:
        """
        获取数据库引擎（进程级单例，首次调用时初始化）

        参数:
            无

        返回:
            Engine: SQLAlchemy引擎实例

        异常:
            OperationalError: 数据库连接不可达时抛出（如MySQL服务未启动）
            ValueError: 数据库类型配置非法时抛出
        """
        if cls._engine is None:
            with cls._lock:
                # 双重检查: 拿到锁后可能已被其他线程初始化
                if cls._engine is None:
                    url = cls._build_db_url()
                    # pool_pre_ping: 每次取连接先探活，自动剔除失效连接（MySQL长连接断开后自愈）
                    # pool_recycle: 连接最大存活3600秒，防止MySQL 8小时wait_timeout断连问题
                    cls._engine = create_engine(
                        url,
                        pool_pre_ping=True,
                        pool_recycle=3600,
                        echo=False,
                        future=True,
                    )
                    cls._session_factory = sessionmaker(
                        bind=cls._engine, expire_on_commit=False, future=True
                    )
                    logger.info(
                        f"数据库引擎初始化完成 | 类型: {url.split('://')[0]} | "
                        f"环境: {env_manager.current_env}"
                    )
        return cls._engine

    @classmethod
    def get_session_factory(cls) -> sessionmaker:
        """
        获取Session工厂（随引擎一并初始化的单例）

        参数:
            无

        返回:
            sessionmaker: Session工厂，调用其返回Session实例

        异常:
            ValueError: 数据库类型配置非法时抛出（透传get_engine异常）
        """
        cls.get_engine()
        return cls._session_factory

    @classmethod
    def get_session(cls) -> Session:
        """
        获取一个新的数据库Session（调用方负责关闭）

        参数:
            无

        返回:
            Session: SQLAlchemy会话实例

        异常:
            ValueError: 数据库类型配置非法时抛出（透传get_engine异常）
        """
        return cls.get_session_factory()()

    @classmethod
    @contextmanager
    def session_scope(cls) -> Generator[Session, None, None]:
        """
        会话上下文管理器（推荐的数据访问方式）

        生命周期约定:
            - 正常退出: 自动commit提交事务
            - 异常退出: 自动rollback回滚事务并记录ERROR日志
            - 任意退出: 自动close归还连接

        参数:
            无

        返回:
            Generator[Session]: yield出的Session实例

        异常:
            SQLAlchemyError: 提交失败时抛出原始异常（回滚已自动完成）

        使用示例:
            with DatabaseSession.session_scope() as session:
                session.add(TestCase(case_id="TM-API-0001", name="登录校验"))
        """
        session = cls.get_session()
        try:
            yield session
            session.commit()
        except SQLAlchemyError as exc:
            session.rollback()
            logger.error(f"数据库事务异常，已回滚 | {exc}")
            raise
        except Exception:
            # 非SQL异常同样回滚，防止会话残留脏状态
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------
    # 运维工具
    # ------------------------------------------------------------------
    @classmethod
    def init_db(cls) -> None:
        """
        初始化数据库表结构（开发/部署环境快速建表）

        幂等操作: 已存在的表不会重复创建，仅新增缺失的表。
        生产环境表结构变更请使用迁移工具（如Alembic，第三阶段接入）。

        参数:
            无

        返回:
            无

        异常:
            OperationalError: 数据库不可达时抛出
        """
        # 延迟导入避免db包初始化时的循环依赖
        from src.db.models import Base

        engine = cls.get_engine()
        Base.metadata.create_all(engine)
        table_names = ", ".join(Base.metadata.tables.keys())
        logger.info(f"数据库表结构初始化完成 | 表: {table_names}")

    @classmethod
    def health_check(cls) -> bool:
        """
        数据库连通性健康检查（部署自检与Web平台探活）

        参数:
            无

        返回:
            bool: 连接与查询均正常返回True；任何异常返回False（异常不向上抛出）

        异常:
            无（全部异常内部消化为False返回）
        """
        try:
            engine = cls.get_engine()
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.debug("数据库健康检查通过")
            return True
        except (OperationalError, SQLAlchemyError, ValueError) as exc:
            logger.error(f"数据库健康检查失败 | {exc}")
            return False

    @classmethod
    def reset(cls) -> None:
        """
        重置引擎与会话工厂（仅测试场景使用，释放后下次访问将重新建连）

        参数:
            无

        返回:
            无

        异常:
            无
        """
        with cls._lock:
            if cls._engine is not None:
                cls._engine.dispose()
                logger.info("数据库引擎已释放")
            cls._engine = None
            cls._session_factory = None
