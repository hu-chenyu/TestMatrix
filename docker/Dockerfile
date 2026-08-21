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

# 先单独复制依赖清单安装，利用Docker层缓存加速重复构建
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制项目源码与配置
COPY src/ ./src/
COPY tests/ ./tests/
COPY testdata/ ./testdata/
COPY pytest.ini .

# 创建运行产物目录（日志/Allure结果/报告的宿主机挂载点）
RUN mkdir -p output/logs output/allure_results output/reports

# 默认执行冒烟测试（可通过 docker run --entrypoint 覆盖为其他命令）
CMD ["pytest", "-m", "smoke", "--alluredir=output/allure_results"]
