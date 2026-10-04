# ==============================================================================
# TestMatrix 平台镜像构建文件
# 构建命令（在项目根目录执行）:
#   docker build -t testmatrix:latest -f docker/Dockerfile .
# 运行测试容器:
#   docker run --rm -v ${PWD}/output:/app/output testmatrix:latest
# ==============================================================================

# 基础镜像: Python 3.11精简版（与项目Python版本要求一致）
FROM python:3.11-slim

# 镜像元信息
LABEL maintainer="TestMatrix" \
      description="TestMatrix 通用自动化测试效能平台"

# 环境变量: 禁用字节码缓存、日志实时输出、时区上海
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

# 工作目录
WORKDIR /app

# 依赖镜像源可配置（Day44 P3-27）：原先硬编码单一国内镜像，
# 境外环境构建会极慢，且第三方可替换源与"依赖全量 pin + pip-audit
# 分级门禁"的供应链口径不匹配。默认走官方源，需要加速时构建期传参：
#   docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
#              -t testmatrix:latest -f docker/Dockerfile .
ARG PIP_INDEX_URL=https://pypi.org/simple

# 先单独复制依赖清单安装，利用Docker层缓存加速重复构建
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i ${PIP_INDEX_URL}

# 复制项目源码与配置
COPY src/ ./src/
COPY tests/ ./tests/
COPY testdata/ ./testdata/
COPY pytest.ini .

# 创建运行产物目录（日志/Allure结果/报告的宿主机挂载点）
RUN mkdir -p output/logs output/allure_results output/reports

# 降权到非 root 运行（Day44 P3-27）：容器内 root 创建的产物在宿主机上
# 归 root 所有，本机清理需 sudo；且一旦镜像基础层被突破，root 权限会
# 直接放大影响面。挂载点需先移交属主，否则非 root 写不进去。
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

# 默认执行冒烟测试（可通过 docker run --entrypoint 覆盖为其他命令）
CMD ["pytest", "-m", "smoke", "--alluredir=output/allure_results"]
