"""
SQLAlchemy数据模型模块

定义测试平台3张核心表:
    test_cases        测试用例表（用例元信息管理）
    test_executions   测试执行记录表（单用例执行明细）
    defect_statistics 缺陷统计表（批次级执行汇总指标）

使用SQLAlchemy 2.0声明式风格（DeclarativeBase + Mapped + mapped_column），
同时兼容SQLite与MySQL（字段类型选取两者通用类型）。
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """
    全部ORM模型的声明式基类

    所有数据模型继承此类，统一由 db_session.DatabaseSession.init_db()
    负责建表与升级管理。
    """


class TestCase(Base):
    """
    测试用例表（test_cases）

    管理平台登记的用例元信息，支撑用例检索、分级调度与统计。

    表字段说明:
        id          自增主键
        case_id     业务用例编号（唯一），如 TM-API-0001
        name        用例名称
        module      所属业务模块（如 用户中心/订单/板卡通信）
        priority    优先级 P0-P3（P0最高，冒烟必跑）
        case_type   用例类型: api=HTTP接口 / chip=芯片板卡
        status      用例状态: active=启用 / disabled=停用
        description 用例描述与验证点说明
        creator     创建人
        created_at  创建时间（数据库时间自动填充）
        updated_at  更新时间（行更新时自动刷新）
    """

    __tablename__ = "test_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="自增主键")
    case_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, comment="业务用例编号，如TM-API-0001"
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="用例名称")
    module: Mapped[str] = mapped_column(String(64), nullable=False, default="default", comment="所属业务模块")
    priority: Mapped[str] = mapped_column(String(8), nullable=False, default="P2", comment="优先级P0-P3")
    case_type: Mapped[str] = mapped_column(String(16), nullable=False, default="api", comment="用例类型api/chip")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", comment="状态active/disabled")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", comment="用例描述")
    creator: Mapped[str] = mapped_column(String(64), nullable=False, default="admin", comment="创建人")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )

    # 复合索引: 按模块+状态检索用例是平台高频查询场景
    __table_args__ = (
        Index("idx_module_status", "module", "status"),
        {"comment": "测试用例元信息表"},
    )

    def __repr__(self) -> str:
        """
        模型可读化表示（调试与日志打印用）

        返回:
            str: 形如 TestCase(case_id=TM-API-0001, name=登录校验, priority=P0) 的字符串
        """
        return (
            f"TestCase(case_id={self.case_id!r}, name={self.name!r}, "
            f"module={self.module!r}, priority={self.priority!r})"
        )


class TestExecution(Base):
    """
    测试执行记录表（test_executions）

    记录每次测试批次中单条用例的执行明细，
    支撑执行历史追溯、失败用例分析与趋势统计。

    表字段说明:
        id            自增主键
        execution_id  执行批次号（同一次测试运行的所有用例记录共享），如 RUN-20260822-153000-8f3a
        case_id       关联的业务用例编号
        case_name     用例名称（冗余存储，防止用例表变更影响历史记录）
        result        执行结果: passed / failed / error / skipped
        start_time    用例开始执行时间
        end_time      用例结束执行时间
        duration      执行耗时（秒，浮点，支持亚秒精度）
        error_message 失败/错误时的异常堆栈信息
        environment   执行环境: dev / test / prod
        executor      执行人（人工姓名或CI标识，如 jenkins）
        created_at    记录落库时间
    """

    __tablename__ = "test_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="自增主键")
    execution_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="执行批次号，如RUN-20260822-153000-8f3a"
    )
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="业务用例编号")
    case_name: Mapped[str] = mapped_column(String(200), nullable=False, default="", comment="用例名称")
    result: Mapped[str] = mapped_column(String(16), nullable=False, comment="结果passed/failed/error/skipped")
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=True, comment="用例开始时间")
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=True, comment="用例结束时间")
    duration: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="执行耗时（秒）")
    error_message: Mapped[str] = mapped_column(Text, nullable=True, comment="失败/错误异常信息")
    environment: Mapped[str] = mapped_column(String(16), nullable=False, default="dev", comment="执行环境")
    executor: Mapped[str] = mapped_column(String(64), nullable=False, default="local", comment="执行人/CI标识")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), comment="落库时间"
    )

    # 复合索引: 按批次号聚合统计、按结果筛选失败记录为高频查询
    __table_args__ = (
        Index("idx_execution_result", "execution_id", "result"),
        {"comment": "测试执行明细记录表"},
    )

    def __repr__(self) -> str:
        """
        模型可读化表示（调试与日志打印用）

        返回:
            str: 形如 TestExecution(execution_id=RUN-xxx, case_id=TM-API-0001, result=passed) 的字符串
        """
        return (
            f"TestExecution(execution_id={self.execution_id!r}, "
            f"case_id={self.case_id!r}, result={self.result!r}, duration={self.duration}s)"
        )


class DefectStatistic(Base):
    """
    缺陷统计表（defect_statistics）

    记录每次测试批次的汇总指标（用例总数/各结果计数/通过率），
    支撑Web平台看板、质量趋势与缺陷密度分析。

    表字段说明:
        id           自增主键
        execution_id 执行批次号（唯一，与test_executions表批次号关联）
        total_cases  批次用例总数
        passed       通过数
        failed       失败数（断言不通过，属于缺陷疑似）
        error        错误数（环境/代码异常，非功能缺陷）
        skipped      跳过数
        pass_rate    通过率（0.0-1.0浮点）
        remark       备注信息（如触发方式、特殊说明）
        created_at   统计记录生成时间
    """

    __tablename__ = "defect_statistics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="自增主键")
    execution_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, comment="执行批次号（唯一）"
    )
    total_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="用例总数")
    passed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="通过数")
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="失败数")
    error: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="错误数")
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="跳过数")
    pass_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="通过率0.0-1.0")
    remark: Mapped[str] = mapped_column(Text, nullable=True, comment="备注信息")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), comment="统计生成时间"
    )

    def __repr__(self) -> str:
        """
        模型可读化表示（调试与日志打印用）

        返回:
            str: 形如 DefectStatistic(execution_id=RUN-xxx, total=50, pass_rate=0.94) 的字符串
        """
        return (
            f"DefectStatistic(execution_id={self.execution_id!r}, "
            f"total_cases={self.total_cases}, pass_rate={self.pass_rate})"
        )
