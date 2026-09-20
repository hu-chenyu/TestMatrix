"""
Redis缓存层模块（第三阶段Day31交付）

定位:
    - 面向读多写少的纯查询接口做结果加速（用例分页列表、
      报告五类统计聚合），命中缓存时免掉数据库limit/offset分页
      与跨表聚合开销
    - 默认关闭（TM_REDIS_ENABLED=false）: 未启用时全部读写方法
      均为no-op，业务链路行为与接入前逐字节一致，零侵入可灰度
    - 故障静默降级: Redis连接异常/命令异常一律在本层消化为
      warning日志，get按未命中处理、set/失效直接跳过，绝不允许
      缓存故障冒泡导致API返回500（缓存是加速能力不是主链路）

key命名规范:
    - 统一前缀tm，结构为 tm:{业务域}:{业务子域}:[{业务参数}]
    - 用例列表: tm:cases:list:{查询参数sha1}（参数排序后哈希，
      不同筛选/分页组合天然隔离，key长度固定）
    - 报告统计: tm:reports:{指标名}[:{limit}]，共5个固定key
    - 失效按前缀SCAN批量删除: tm:cases:list: 与 tm:reports:

TTL策略:
    - 统一过期秒数由TM_CACHE_TTL配置（默认300秒），写缓存时
      用SETEX原子写入，到期自动失效兜底一致性
    - 主动失效: 用例写操作（创建/更新/删除/导入）成功后清用例
      列表前缀；执行批次终态（finished/failed）后清报告前缀；
      TTL只作兜底，不依赖短TTL掩盖失效遗漏
    - 空结果同样缓存（空列表/空字典），防冷数据被反复穿透打库

测试约定:
    - TM_REDIS_URL以fake://开头时使用fakeredis内存实例
      （FakeRedis(decode_responses=True)），测试全程零真实
      Redis进程依赖；测试fixture通过reset_backend()在用例间
      重建实例保证数据隔离

使用示例:
    from src.core.cache import cache_client, cases_list_key

    key = cases_list_key({"status": "active", "page": 1})
    data = cache_client.get_json(key)
    if data is None:
        data = CaseManager.list_cases_paged(...)
        cache_client.set_json(key, data)
"""

import hashlib
import json
from typing import Any, Optional

import redis

from src.common.env_manager import env_manager
from src.common.logger import LogManager

logger = LogManager.get_logger()

# 缓存key统一前缀与业务域前缀（key构造的单一事实来源，禁止散落硬编码）
KEY_PREFIX = "tm"
CASES_LIST_PREFIX = f"{KEY_PREFIX}:cases:list:"
REPORTS_PREFIX = f"{KEY_PREFIX}:reports:"

# fake://协议标记: URL以此开头时构造fakeredis内存实例（测试专用）
FAKE_SCHEME = "fake://"


# ===========================================================================
# key构造纯函数（无副作用，便于测试直接断言）
# ===========================================================================
def cases_list_key(params: dict) -> str:
    """
    构造用例列表分页查询缓存key

    将规范化后的查询参数（module/priority/case_type/status/
    keyword/page/page_size）按key排序后JSON序列化，再做sha1
    摘要，保证: ①参数排列顺序不同但语义相同的请求命中同一key；
    ②任一参数不同则key必不同；③key长度固定不泄露查询内容。

    参数:
        params (dict): 规范化查询参数字典（缺失维度统一传None）

    返回:
        str: "tm:cases:list:{40位sha1十六进制摘要}"

    异常:
        无（入参均为str/int/None等JSON可序列化的基础类型）
    """
    # sort_keys保证字典序稳定；separators压缩空白，保证哈希输入确定
    payload = json.dumps(
        params, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"{CASES_LIST_PREFIX}{digest}"


def reports_summary_key() -> str:
    """
    构造报告全局汇总缓存key（固定key，无查询参数）

    参数:
        无

    返回:
        str: "tm:reports:summary"

    异常:
        无
    """
    return f"{REPORTS_PREFIX}summary"


def reports_trend_key(limit: int) -> str:
    """
    构造报告通过率趋势缓存key（limit不同则key不同）

    参数:
        limit (int): 返回最近N个批次的条数

    返回:
        str: "tm:reports:trend:{limit}"

    异常:
        无
    """
    return f"{REPORTS_PREFIX}trend:{limit}"


def reports_module_distribution_key() -> str:
    """
    构造模块执行分布缓存key（固定key，无查询参数）

    参数:
        无

    返回:
        str: "tm:reports:module_distribution"

    异常:
        无
    """
    return f"{REPORTS_PREFIX}module_distribution"


def reports_failed_top_key(limit: int) -> str:
    """
    构造失败用例Top榜缓存key（limit不同则key不同）

    参数:
        limit (int): 返回Top N失败用例的条数

    返回:
        str: "tm:reports:failed_top:{limit}"

    异常:
        无
    """
    return f"{REPORTS_PREFIX}failed_top:{limit}"


def reports_quality_metrics_key() -> str:
    """
    构造质量度量缓存key（固定key，无查询参数）

    参数:
        无

    返回:
        str: "tm:reports:quality_metrics"

    异常:
        无
    """
    return f"{REPORTS_PREFIX}quality_metrics"


class CacheClient:
    """
    Redis缓存客户端（懒连接 + 默认关闭 + 故障静默降级）

    职责:
        - 按TM_REDIS_ENABLED开关决定是否启用，关闭时所有方法no-op
        - 懒构建后端实例: 首次读写时才连接（fakeredis内存实例或
          redis.from_url真实客户端），应用启动不强制Redis可达
        - get_json/set_json做JSON序列化封装（ensure_ascii=False，
          中文原样存储）；delete_pattern用SCAN游标迭代批量删除，
          禁用KEYS（大库KEYS阻塞Redis主循环）
        - 所有命令异常仅记warning日志: get按未命中返回None，
          set/失效静默跳过，业务零感知

    属性:
        _backend (redis.Redis | fakeredis.FakeRedis | None):
            懒加载的后端实例，None表示尚未构建
    """

    def __init__(self) -> None:
        """
        初始化缓存客户端（只置空后端，不建立任何连接）

        参数:
            无

        返回:
            无

        异常:
            无
        """
        self._backend: Optional[Any] = None

    # ------------------------------------------------------------------
    # 配置属性（实时读env_manager，环境变量可被monkeypatch热替换）
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """
        缓存总开关（TM_REDIS_ENABLED，默认false关闭）

        参数:
            无

        返回:
            bool: 是否启用缓存（true/1/yes/on为真）
        """
        return env_manager.get_bool("TM_REDIS_ENABLED", False)

    @property
    def redis_url(self) -> str:
        """
        Redis连接URL（TM_REDIS_URL，默认本机6379的0号库）

        参数:
            无

        返回:
            str: 连接URL；fake://开头为测试内存实例约定
        """
        return env_manager.get(
            "TM_REDIS_URL", "redis://127.0.0.1:6379/0"
        )

    @property
    def default_ttl(self) -> int:
        """
        默认缓存TTL秒数（TM_CACHE_TTL，默认300秒）

        参数:
            无

        返回:
            int: 过期秒数
        """
        return env_manager.get_int("TM_CACHE_TTL", 300)

    # ------------------------------------------------------------------
    # 后端构建（懒连接 + fake协议测试支持）
    # ------------------------------------------------------------------
    def _get_backend(self) -> Optional[Any]:
        """
        获取缓存后端实例（懒加载并缓存复用）

        构建规则:
            - enabled=false: 直接返回None（全部读写no-op）
            - URL以fake://开头: 构建fakeredis内存实例
              （decode_responses=True，读写均为str避免bytes/str
              混用导致的类型错乱；每个实例数据独立）
            - 其余URL: redis.from_url构建真实客户端
              （socket_timeout=2秒防命令长时间挂起；from_url本身
              不建连，首次命令才真正连通，应用启动不依赖Redis）

        参数:
            无

        返回:
            Any | None: 后端实例；未启用或fakeredis依赖缺失时为None

        异常:
            无（ImportError降级为None并记warning，连接异常在
                具体命令处由RedisError兜底）
        """
        # 开关关闭: 不构建也不复用历史后端（热关闭立即生效）
        if not self.enabled:
            return None
        if self._backend is None:
            url = self.redis_url
            if url.startswith(FAKE_SCHEME):
                # 延迟导入: fakeredis仅测试/本地使用，生产环境可不安装
                try:
                    import fakeredis

                    self._backend = fakeredis.FakeRedis(
                        decode_responses=True
                    )
                    logger.debug("缓存后端已构建 | 类型: fakeredis内存实例")
                except ImportError as exc:
                    # 依赖缺失按未启用处理，绝不因缓存影响启动
                    logger.warning(
                        f"缓存已启用但fakeredis未安装，缓存降级no-op | {exc}"
                    )
                    return None
            else:
                # 真实Redis客户端（decode_responses=True统一str读写）
                self._backend = redis.from_url(
                    url,
                    decode_responses=True,
                    socket_timeout=2,
                )
                logger.debug(f"缓存后端已构建 | 类型: redis | URL: {url}")
        return self._backend

    def reset_backend(self) -> None:
        """
        重置后端实例（仅测试场景使用）

        关闭并丢弃已缓存的后端，下次访问时按最新环境变量重新构建；
        function级fixture在用例前后调用，保证fake内存实例跨用例
        数据隔离（见tests/test_cache_layer_demo.py）。

        参数:
            无

        返回:
            无

        异常:
            无（close异常静默忽略）
        """
        if self._backend is not None:
            try:
                self._backend.close()
            except Exception as exc:  # noqa: BLE001 测试重置不关心关闭异常
                logger.debug(f"缓存后端关闭异常已忽略 | {exc}")
        self._backend = None

    # ------------------------------------------------------------------
    # JSON读写
    # ------------------------------------------------------------------
    def get_json(self, key: str) -> Optional[Any]:
        """
        读取缓存并JSON反序列化

        参数:
            key (str): 缓存key

        返回:
            Any | None: 命中返回反序列化后的对象（dict/list等）；
                        未启用、未命中或任何Redis/反序列化异常时
                        返回None（按未命中处理，调用方正常回源）

        异常:
            无（异常全部内部消化）
        """
        backend = self._get_backend()
        if backend is None:
            # 未启用: no-op，不打命中/未命中日志避免噪音
            return None
        try:
            raw_value = backend.get(key)
        except redis.RedisError as exc:
            # Redis故障: 静默降级为未命中，只打一条warning
            logger.warning(f"缓存读取异常，按未命中降级 | key={key} | {exc}")
            return None
        if raw_value is None:
            logger.debug(f"缓存未命中 | key={key}")
            return None
        try:
            value = json.loads(raw_value)
        except (ValueError, TypeError) as exc:
            # 脏数据不拖垮接口: 反序列化失败同样按未命中回源
            logger.warning(f"缓存值反序列化失败，按未命中降级 | key={key} | {exc}")
            return None
        logger.debug(f"缓存命中 | key={key}")
        return value

    def set_json(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """
        JSON序列化写入缓存并设置TTL（SETEX原子操作）

        空列表/空字典等空结果同样写入（防缓存穿透: TTL到期前
        不再反复查库）。

        参数:
            key (str): 缓存key
            value (Any): 待缓存对象（JSON可序列化，一般为dict/list）
            ttl (int | None): 过期秒数，None时用TM_CACHE_TTL默认值

        返回:
            无

        异常:
            无（未启用或任何Redis异常时静默no-op）
        """
        backend = self._get_backend()
        if backend is None:
            return
        # 显式TTL须为正整数，否则回落默认TTL
        effective_ttl = ttl if isinstance(ttl, int) and ttl > 0 else self.default_ttl
        try:
            # ensure_ascii=False: 中文原样存储，可读且省字节
            payload = json.dumps(value, ensure_ascii=False)
            backend.setex(key, effective_ttl, payload)
        except redis.RedisError as exc:
            logger.warning(f"缓存写入异常，已跳过 | key={key} | {exc}")

    # ------------------------------------------------------------------
    # 前缀失效（SCAN迭代，禁用KEYS）
    # ------------------------------------------------------------------
    def delete_pattern(self, prefix: str) -> None:
        """
        按前缀批量删除缓存key（SCAN游标迭代 + 逐个DEL）

        使用SCAN而非KEYS: KEYS在大key量下阻塞Redis主循环，
        SCAN是非阻塞游标式迭代（弱一致视图，迭代期间新增的key
        是否删到不保证，但失效场景只要求删除迭代开始前已存在的
        匹配key，TTL同时兜底）。

        参数:
            prefix (str): key前缀（内部自动补*通配符）

        返回:
            无

        异常:
            无（未启用或任何Redis异常时静默no-op）
        """
        backend = self._get_backend()
        if backend is None:
            return
        pattern = f"{prefix}*"
        try:
            deleted_count = 0
            # scan_iter内部封装SCAN游标分批迭代，match做服务端过滤
            for matched_key in backend.scan_iter(match=pattern):
                backend.delete(matched_key)
                deleted_count += 1
            logger.debug(
                f"缓存失效 | 前缀={prefix} | 删除key数={deleted_count}"
            )
        except redis.RedisError as exc:
            logger.warning(
                f"缓存批量失效异常，已跳过 | 前缀={prefix} | {exc}"
            )

    def invalidate_cases_list(self) -> None:
        """
        失效全部用例列表缓存（用例写操作成功后调用）

        参数:
            无

        返回:
            无

        异常:
            无（delete_pattern内部兜底）
        """
        self.delete_pattern(CASES_LIST_PREFIX)

    def invalidate_reports(self) -> None:
        """
        失效全部报告统计缓存（执行批次终态后调用）

        参数:
            无

        返回:
            无

        异常:
            无（delete_pattern内部兜底）
        """
        self.delete_pattern(REPORTS_PREFIX)


# 模块级单例: 全项目统一导入同一实例，复用连接池与fake实例状态
cache_client = CacheClient()
