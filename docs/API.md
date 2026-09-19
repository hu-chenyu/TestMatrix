# TestMatrix API v1 接口文档

> **基础地址**：`http://host:5000`（本机调试为 `http://127.0.0.1:5000`）
> **当前版本**：平台版本 v1.0.0，API 版本 v1
> **文档更新**：2026-09-20（Day30），对应 4 个蓝图共 19 个 HTTP 接口
> **鉴权说明**：当前版本无认证，仅内网/本机使用，禁止暴露公网。

本文覆盖 TestMatrix Web 后端全部 HTTP 接口，按蓝图（blueprint）组织：
基础接口（base_bp）、用例管理（cases_bp）、执行管理（executions_bp）、
报告统计（reports_bp）。所有字段名、枚举值、默认值均与源码逐字一致。

---

## 1. 通用约定

### 1.1 统一响应格式

除 `204 No Content` 外，所有接口响应体均为如下三段式 JSON 结构：

```json
{
  "code": 200,
  "message": "ok",
  "data": {}
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| code | int | 业务码，与 HTTP 状态码相同（200/201/202/400/404/405/409/500/503） |
| message | string | 结果描述；成功为固定文案，失败为面向客户端可读的错误信息 |
| data | object/array/null | 业务数据；无数据或错误时为 null（部分错误携带 detail 对象） |

删除接口成功返回 `204`，**无响应体**（响应体为空字符串）。

### 1.2 错误码与错误响应

错误响应同样使用统一三段式结构。各状态码含义如下：

| 状态码 | 含义 | 典型触发场景 |
|---|---|---|
| 400 | 参数错误 / 业务拒绝 | 查询参数非法、JSON 校验失败、空用例集触发执行 |
| 404 | 资源不存在 | 用例编号/批次号不存在、访问未注册路径 |
| 405 | 方法不允许 | 路径存在但 HTTP 方法错误 |
| 409 | 资源冲突 | 创建用例时 case_id 已存在 |
| 500 | 服务器内部错误 | 未捕获异常；生产模式只回"服务器内部错误"，不泄露内部细节 |

`400` 参数校验失败示例（detail 携带字段级错误字典）：

```json
{
  "code": 400,
  "message": "用例入参校验失败: priority: Must be one of: P0, P1, P2, P3.",
  "data": {
    "priority": [
      "Must be one of: P0, P1, P2, P3."
    ]
  }
}
```

`404` 资源不存在示例：

```json
{
  "code": 404,
  "message": "用例不存在",
  "data": {
    "case_id": "TM-UC-9999"
  }
}
```

`405` 方法不允许示例：

```json
{
  "code": 405,
  "message": "请求方法不被允许",
  "data": null
}
```

`409` 资源冲突示例：

```json
{
  "code": 409,
  "message": "用例编号已存在",
  "data": {
    "case_id": "TM-UC-0001"
  }
}
```

`500` 服务器内部错误示例（生产模式响应，不含堆栈与异常细节）：

```json
{
  "code": 500,
  "message": "服务器内部错误",
  "data": null
}
```

### 1.3 分页约定

列表类接口统一使用以下查询参数与响应结构：

| 查询参数 | 类型 | 默认 | 取值范围 | 说明 |
|---|---|---|---|---|
| page | int | 1 | ≥ 1 | 页码，从 1 开始 |
| page_size | int | 20 | 1 ~ 100 | 每页条数，上限 `MAX_PAGE_SIZE=100` |

分页响应 `data` 结构（字段名全接口统一）：

```json
{
  "items": [],
  "total": 0,
  "page": 1,
  "page_size": 20,
  "total_pages": 0
}
```

### 1.4 编码、时间与 SSE 约定

- **字符编码**：所有请求与响应统一使用 UTF-8；响应 `Content-Type` 为
  `application/json`（SSE 接口除外）。
- **时间字段**：统一为 ISO 8601 字符串，如 `2026-09-20T10:30:45`；
  健康检查的 timestamp 带 UTC 时区，如
  `2026-09-20T02:30:45.123456+00:00`；可空时间字段未产生时为 `null`。
- **SSE 接口**：响应类型为 `text/event-stream`，帧格式为
  `event: 事件类型\nid: 序号\ndata: JSON载荷\n\n`；客户端断线重连时可通过
  `Last-Event-ID` 请求头携带最后收到的事件 id，服务端只回放该 id 之后的事件
  （请求头缺失或非法时全量回放）；空闲时服务端发送 `: heartbeat` 注释帧保活。
- **curl 示例约定**：本文所有命令在 Windows PowerShell 中直接运行，统一使用
  系统自带的 `curl.exe`（不要用 `curl` 别名）。JSON 请求体中的双引号统一写为
  `\"`；请求体含中文时建议先执行 `chcp 65001` 切换 UTF-8 代码页，
  或使用 PowerShell 7。

---

## 2. 基础接口（base_bp，无前缀）

### 2.1 GET /

平台首页接口，返回版本与运行状态。

**请求参数**：无。

**请求示例**：

```bash
curl.exe "http://127.0.0.1:5000/"
```

**成功响应**（200）：

```json
{
  "code": 200,
  "message": "TestMatrix API",
  "data": {
    "version": "1.0.0",
    "status": "running"
  }
}
```

**错误响应**：无业务错误。

### 2.2 GET /health

健康检查接口，内置数据库连通性探测（执行 `SELECT 1`，3 秒超时保护）。
数据库正常返回 200；数据库不可用时服务降级返回 503。

**请求参数**：无。

**请求示例**：

```bash
curl.exe "http://127.0.0.1:5000/health"
```

**成功响应**（200，数据库正常）：

```json
{
  "code": 200,
  "message": "ok",
  "data": {
    "status": "healthy",
    "database": "connected",
    "timestamp": "2026-09-20T02:30:45.123456+00:00",
    "env": "dev"
  }
}
```

**降级响应**（503，数据库故障，error 为异常摘要）：

```json
{
  "code": 503,
  "message": "服务降级：数据库连接失败",
  "data": {
    "status": "degraded",
    "database": "disconnected",
    "error": "数据库探测超时（超过3.0秒）",
    "timestamp": "2026-09-20T02:31:02.882104+00:00",
    "env": "dev"
  }
}
```

### 2.3 GET /api/version

返回平台版本与 API 版本号。

**请求参数**：无。

**请求示例**：

```bash
curl.exe "http://127.0.0.1:5000/api/version"
```

**成功响应**（200）：

```json
{
  "code": 200,
  "message": "version info",
  "data": {
    "version": "1.0.0",
    "api_version": "v1"
  }
}
```

**错误响应**：无业务错误。

---

## 3. 用例管理接口（cases_bp，前缀 /api/cases）

### 3.1 GET /api/cases/

用例列表分页查询，支持六维筛选（全部可选、可任意组合），数据库层完成
limit/offset 分页。注意集合路径末尾带斜杠。

**Query 参数**：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| page | int | 否 | 1 | 页码，必须 ≥ 1 |
| page_size | int | 否 | 20 | 每页条数，1 ~ 100 |
| module | string | 否 | 无 | 模块精确匹配，如 `用户中心` |
| priority | string | 否 | 无 | 优先级筛选，P0/P1/P2/P3，不区分大小写 |
| case_type | string | 否 | 无 | 用例类型，`api` 或 `chip`，非法值返回 400 |
| status | string | 否 | active | `active`/`disabled`/`all`，非法值返回 400 |
| keyword | string | 否 | 无 | 模糊搜索 case_id/name/description 三字段 |

**请求示例**（查 P1 优先级、第 1 页每页 2 条）：

```bash
curl.exe "http://127.0.0.1:5000/api/cases/?priority=P1&page=1&page_size=2"
```

**成功响应**（200）：

```json
{
  "code": 200,
  "message": "ok",
  "data": {
    "items": [
      {
        "id": 1,
        "case_id": "TM-UC-0001",
        "name": "用户登录成功校验",
        "module": "用户中心",
        "priority": "P1",
        "case_type": "api",
        "status": "active",
        "description": "跨API集成测试种子用例",
        "creator": "admin",
        "created_at": "2026-09-20T10:15:30",
        "updated_at": "2026-09-20T10:15:30"
      },
      {
        "id": 2,
        "case_id": "TM-UC-0002",
        "name": "用户登录密码错误校验",
        "module": "用户中心",
        "priority": "P1",
        "case_type": "api",
        "status": "active",
        "description": "跨API集成测试种子用例",
        "creator": "admin",
        "created_at": "2026-09-20T10:15:31",
        "updated_at": "2026-09-20T10:15:31"
      }
    ],
    "total": 2,
    "page": 1,
    "page_size": 2,
    "total_pages": 1
  }
}
```

**错误响应**（400，枚举值非法）：

```json
{
  "code": 400,
  "message": "case_type只能为api或chip",
  "data": null
}
```

其余 400 文案：`page和page_size必须为正整数`、
`page必须为大于等于1的整数`、`page_size必须在1到100之间`、
`status只能为active、disabled或all`。

### 3.2 POST /api/cases/

创建用例，请求体经 marshmallow 校验，成功返回 201 与完整用例对象。

**请求体（JSON）**：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| case_id | string | 是 | — | 业务编号，长度 1 ~ 64，全库唯一 |
| name | string | 是 | — | 用例名称，长度 1 ~ 200 |
| module | string | 否 | default | 模块名，长度 ≤ 64 |
| priority | string | 否 | P2 | P0/P1/P2/P3 |
| case_type | string | 否 | api | `api` 或 `chip` |
| status | string | 否 | active | `active` 或 `disabled` |
| description | string | 否 | 空字符串 | 用例描述 |
| creator | string | 否 | admin | 创建人，长度 ≤ 64 |

**请求示例**：

```bash
curl.exe -X POST "http://127.0.0.1:5000/api/cases/" -H "Content-Type: application/json" -d "{\"case_id\":\"TM-UC-0001\",\"name\":\"用户登录成功校验\",\"module\":\"用户中心\",\"priority\":\"P1\",\"case_type\":\"api\"}"
```

**成功响应**（201）：

```json
{
  "code": 201,
  "message": "created",
  "data": {
    "id": 1,
    "case_id": "TM-UC-0001",
    "name": "用户登录成功校验",
    "module": "用户中心",
    "priority": "P1",
    "case_type": "api",
    "status": "active",
    "description": "",
    "creator": "admin",
    "created_at": "2026-09-20T10:15:30",
    "updated_at": "2026-09-20T10:15:30"
  }
}
```

**错误响应**：

- `400` 请求体非 JSON：`{"code":400,"message":"请求体必须为JSON格式","data":null}`
- `400` 字段校验失败：message 形如
  `用例入参校验失败: name: Length must be between 1 and 200.`，
  data 为字段级错误字典（示例见 1.2 节）。
- `409` 编号重复：

```json
{
  "code": 409,
  "message": "用例编号已存在",
  "data": {
    "case_id": "TM-UC-0001"
  }
}
```

### 3.3 GET /api/cases/{case_id}

查询单用例详情。

**路径参数**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| case_id | string | 是 | 业务用例编号，如 `TM-UC-0001` |

**请求示例**：

```bash
curl.exe "http://127.0.0.1:5000/api/cases/TM-UC-0001"
```

**成功响应**（200）：

```json
{
  "code": 200,
  "message": "ok",
  "data": {
    "id": 1,
    "case_id": "TM-UC-0001",
    "name": "用户登录成功校验",
    "module": "用户中心",
    "priority": "P1",
    "case_type": "api",
    "status": "active",
    "description": "跨API集成测试种子用例",
    "creator": "admin",
    "created_at": "2026-09-20T10:15:30",
    "updated_at": "2026-09-20T10:15:30"
  }
}
```

**错误响应**（404，用例不存在）：

```json
{
  "code": 404,
  "message": "用例不存在",
  "data": {
    "case_id": "TM-UC-9999"
  }
}
```

### 3.4 PUT /api/cases/{case_id}

更新用例（partial 更新，只更新传入字段）。case_id 业务编号不可修改，
请求体中回传 case_id 会被静默忽略。

**路径参数**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| case_id | string | 是 | 业务用例编号 |

**请求体（JSON，全部字段可选，至少传一个）**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| name | string | 否 | 长度 1 ~ 200 |
| module | string | 否 | 长度 ≤ 64 |
| priority | string | 否 | P0/P1/P2/P3 |
| case_type | string | 否 | `api` 或 `chip` |
| status | string | 否 | `active` 或 `disabled` |
| description | string | 否 | 用例描述 |
| creator | string | 否 | 长度 ≤ 64 |

**请求示例**（禁用用例）：

```bash
curl.exe -X PUT "http://127.0.0.1:5000/api/cases/TM-UC-0001" -H "Content-Type: application/json" -d "{\"status\":\"disabled\"}"
```

**成功响应**（200，data 为更新后的完整用例对象）：

```json
{
  "code": 200,
  "message": "ok",
  "data": {
    "id": 1,
    "case_id": "TM-UC-0001",
    "name": "用户登录成功校验",
    "module": "用户中心",
    "priority": "P1",
    "case_type": "api",
    "status": "disabled",
    "description": "跨API集成测试种子用例",
    "creator": "admin",
    "created_at": "2026-09-20T10:15:30",
    "updated_at": "2026-09-20T10:20:05"
  }
}
```

**错误响应**：

- `400` 请求体非 JSON：`请求体必须为JSON格式`
- `400` 空更新（无任何可更新字段）：

```json
{
  "code": 400,
  "message": "至少提供一个待更新字段",
  "data": null
}
```

- `404` 用例不存在：`{"code":404,"message":"用例不存在","data":{"case_id":"TM-UC-9999"}}`

### 3.5 DELETE /api/cases/{case_id}

删除用例（物理删除，行级删除）。成功返回 204 且无响应体。

**路径参数**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| case_id | string | 是 | 业务用例编号 |

**请求示例**：

```bash
curl.exe -X DELETE "http://127.0.0.1:5000/api/cases/TM-UC-0002" -i
```

**成功响应**（204，无响应体）：

```text
HTTP/1.1 204 NO CONTENT
```

**错误响应**（404，用例不存在，仍为 JSON 响应）：

```json
{
  "code": 404,
  "message": "用例不存在",
  "data": {
    "case_id": "TM-UC-9999"
  }
}
```

### 3.6 POST /api/cases/import

批量导入用例：上传 YAML/Excel 文件，服务端解析后按 case_id 幂等
upsert 入库（存在则更新业务字段，不存在则插入），临时文件请求结束即清理。

**请求格式**：`multipart/form-data`

**表单字段**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| file | file | 是 | 用例数据文件，仅支持 `.yaml`/`.yml`/`.xlsx` |

**Query 参数**：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| sheet_name | string | 否 | 无 | Excel sheet 名（仅 .xlsx 生效），缺省读活动 sheet |
| creator | string | 否 | admin | 创建人，仅首次插入时写入 |

**请求示例**（在 cases.yaml 所在目录执行）：

```bash
curl.exe -X POST "http://127.0.0.1:5000/api/cases/import" -F "file=@cases.yaml"
```

**成功响应**（200，file_name 为原始上传文件名）：

```json
{
  "code": 200,
  "message": "ok",
  "data": {
    "file_name": "cases.yaml",
    "total": 4,
    "inserted": 4,
    "updated": 0
  }
}
```

**错误响应**（400）：

- 未上传文件：`{"code":400,"message":"未上传用例数据文件","data":null}`
- 后缀不支持：

```json
{
  "code": 400,
  "message": "不支持的文件格式: .txt，仅支持 .yaml/.yml/.xlsx",
  "data": null
}
```

- 数据校验失败：message 透传 DataDriver 校验信息，含文件路径、行号与字段名。

---

## 4. 执行管理接口（executions_bp，前缀 /api/executions）

### 4.1 GET /api/executions/

执行批次列表（分页），只包含已完成批次（存在 defect_statistics 汇总记录的
批次）；按 created_at 倒序、execution_id 倒序排列。注意集合路径末尾带斜杠。

**Query 参数**：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| page | int | 否 | 1 | 页码，必须 ≥ 1 |
| page_size | int | 否 | 20 | 每页条数，1 ~ 100 |

**请求示例**：

```bash
curl.exe "http://127.0.0.1:5000/api/executions/?page=1&page_size=20"
```

**成功响应**（200，items 元素为批次汇总对象）：

```json
{
  "code": 200,
  "message": "ok",
  "data": {
    "items": [
      {
        "execution_id": "RUN-20260920-103045-a1b2",
        "total_cases": 4,
        "passed": 2,
        "failed": 2,
        "error": 0,
        "skipped": 0,
        "pass_rate": 0.5,
        "created_at": "2026-09-20T10:30:46"
      }
    ],
    "total": 1,
    "page": 1,
    "page_size": 20,
    "total_pages": 1
  }
}
```

**错误响应**（400，分页参数非法）：

```json
{
  "code": 400,
  "message": "page_size必须在1到100之间",
  "data": null
}
```

### 4.2 GET /api/executions/{execution_id}

批次详情：summary 汇总统计 + items 单用例执行明细（按 id 升序，即执行先后
顺序）；失败/错误用例的 error_message 完整返回不截断。

**路径参数**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| execution_id | string | 是 | 执行批次号，如 `RUN-20260920-103045-a1b2` |

**请求示例**：

```bash
curl.exe "http://127.0.0.1:5000/api/executions/RUN-20260920-103045-a1b2"
```

**成功响应**（200）：

```json
{
  "code": 200,
  "message": "ok",
  "data": {
    "summary": {
      "execution_id": "RUN-20260920-103045-a1b2",
      "total_cases": 4,
      "passed": 2,
      "failed": 2,
      "error": 0,
      "skipped": 0,
      "pass_rate": 0.5,
      "created_at": "2026-09-20T10:30:46"
    },
    "items": [
      {
        "id": 1,
        "execution_id": "RUN-20260920-103045-a1b2",
        "case_id": "TM-UC-0001",
        "case_name": "用户登录成功校验",
        "result": "failed",
        "start_time": "2026-09-20T10:30:45",
        "end_time": "2026-09-20T10:30:45",
        "duration": 0.01,
        "error_message": "模拟执行失败: 断言不通过",
        "environment": "dev",
        "executor": "web",
        "created_at": "2026-09-20T10:30:45"
      },
      {
        "id": 2,
        "execution_id": "RUN-20260920-103045-a1b2",
        "case_id": "TM-UC-0002",
        "case_name": "用户登录密码错误校验",
        "result": "passed",
        "start_time": "2026-09-20T10:30:45",
        "end_time": "2026-09-20T10:30:45",
        "duration": 0.01,
        "error_message": null,
        "environment": "dev",
        "executor": "web",
        "created_at": "2026-09-20T10:30:45"
      }
    ]
  }
}
```

说明：result 取值 `passed`/`failed`/`error`/`skipped`；start_time、end_time、
error_message 为可空字段，无值时为 `null`。

**错误响应**（404，批次不存在或未完成，均无汇总记录）：

```json
{
  "code": 404,
  "message": "执行批次不存在",
  "data": {
    "execution_id": "RUN-00000000-000000-0000"
  }
}
```

### 4.3 POST /api/executions/trigger

触发用例异步执行。服务端落 pending 批次行后由 daemon 后台线程执行，接口
立即返回 202 不阻塞；触发来源固定为 `web`，不由请求体决定。请求体全部
字段可选，空请求体即全量回归。

**请求体（JSON，全部可选）**：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| module | string | 否 | 无 | 模块筛选 |
| priority | string 或 array[string] | 否 | 无 | 优先级筛选，支持单值或数组 |
| tags | string 或 array[string] | 否 | 无 | 标签筛选，支持单值或数组 |
| case_type | string | 否 | api | `api` 或 `chip`，非法值 400 |
| executor | string | 否 | 读 TM_EXECUTOR 环境变量，兜底 simulated | `simulated` 或 `pytest`，非法值 400 |
| environment | string | 否 | dev | 执行环境 |
| remark | string | 否 | 无 | 批次备注 |

**请求示例**（触发"用户中心"模块，指定模拟执行器）：

```bash
curl.exe -X POST "http://127.0.0.1:5000/api/executions/trigger" -H "Content-Type: application/json" -d "{\"module\":\"用户中心\",\"executor\":\"simulated\"}"
```

**成功响应**（202，批次状态机起点为 pending）：

```json
{
  "code": 202,
  "message": "执行批次已受理，后台执行中",
  "data": {
    "execution_id": "RUN-20260920-103045-a1b2",
    "status": "pending",
    "total_cases": 2
  }
}
```

批次号格式为 `RUN-YYYYMMDD-HHMMSS-xxxx`，xxxx 为 4 位随机十六进制字符。

**错误响应**（400）：

- 请求体不是 JSON 对象：

```json
{
  "code": 400,
  "message": "请求体必须为JSON对象",
  "data": null
}
```

- executor 非法：

```json
{
  "code": 400,
  "message": "executor非法: 'real'，合法取值: ['simulated', 'pytest']",
  "data": null
}
```

- case_type 非法：`{"code":400,"message":"case_type非法: 'hw'，合法取值: api/chip","data":null}`
- 筛选后无可用用例：message 为 `无符合条件的用例可执行`。

### 4.4 GET /api/executions/{execution_id}/status

查询批次状态机当前状态与完成后冗余的结果计数；直查批次元信息表，
执行中批次同样可查（冗余统计字段为初始值 0）。

**路径参数**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| execution_id | string | 是 | 执行批次号 |

**请求示例**：

```bash
curl.exe "http://127.0.0.1:5000/api/executions/RUN-20260920-103045-a1b2/status"
```

**成功响应**（200，finished 状态示例）：

```json
{
  "code": 200,
  "message": "ok",
  "data": {
    "execution_id": "RUN-20260920-103045-a1b2",
    "status": "finished",
    "total_cases": 4,
    "passed": 2,
    "failed": 2,
    "error": 0,
    "skipped": 0,
    "pass_rate": 0.5,
    "error_message": null,
    "started_at": "2026-09-20T10:30:45",
    "finished_at": "2026-09-20T10:30:46",
    "created_at": "2026-09-20T10:30:45"
  }
}
```

status 取值：`pending`（已受理未开始）/`running`（执行中）/
`finished`（正常完成）/`failed`（批次级异常终止，此时 error_message 携带
`异常类型: 信息`，started_at/finished_at 按实际进度可为 null）。

**错误响应**（404）：

```json
{
  "code": 404,
  "message": "执行批次不存在",
  "data": {
    "execution_id": "RUN-00000000-000000-0000"
  }
}
```

### 4.5 GET /api/executions/{execution_id}/events

批次执行事件 SSE 实时流（`text/event-stream`）。运行中批次实时推送事件，
收到终态事件后服务端主动关闭流；已终态批次按快照/DB 重建方式有限补发后
关闭流，不会挂死连接。批次不存在时返回普通 JSON 404。

**路径参数**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| execution_id | string | 是 | 执行批次号 |

**请求头**：

| 头部 | 类型 | 必填 | 说明 |
|---|---|---|---|
| Last-Event-ID | int | 否 | 最后收到的事件 id，断线重连回放该 id 之后的事件；缺失/非法时全量回放 |

**请求示例**：

```bash
curl.exe -N "http://127.0.0.1:5000/api/executions/RUN-20260920-103045-a1b2/events"
```

断线回放示例：

```bash
curl.exe -N -H "Last-Event-ID: 3" "http://127.0.0.1:5000/api/executions/RUN-20260920-103045-a1b2/events"
```

**成功响应**（`200 OK`，`Content-Type: text/event-stream`，帧序列示例）：

```text
event: batch_start
id: 1
data: {"total_cases": 4, "executor_kind": "simulated"}

event: case_finished
id: 2
data: {"case_id": "TM-UC-0001", "case_name": "用户登录成功校验", "result": "failed", "duration": 0.01, "error_message": "模拟执行失败: 断言不通过"}

event: case_finished
id: 3
data: {"case_id": "TM-UC-0002", "case_name": "用户登录密码错误校验", "result": "passed", "duration": 0.01, "error_message": null}

: heartbeat

event: batch_finished
id: 5
data: {"total": 4, "passed": 2, "failed": 2, "error": 0, "skipped": 0, "pass_rate": 0.5}
```

**事件类型与载荷**：

| 事件 | 载荷字段 |
|---|---|
| batch_start | total_cases、executor_kind |
| case_finished | case_id、case_name、result、duration、error_message |
| batch_finished | total、passed、failed、error、skipped、pass_rate |
| batch_failed | error_message |

`: heartbeat` 为心跳注释帧（空闲约 15 秒发送），无 id 行，客户端静默忽略。

**错误响应**（404，普通 JSON，非 SSE 帧）：

```json
{
  "code": 404,
  "message": "执行批次不存在",
  "data": {
    "execution_id": "RUN-00000000-000000-0000"
  }
}
```

---

## 5. 报告统计接口（reports_bp，前缀 /api/reports）

### 5.1 GET /api/reports/summary

全局执行汇总，基于 defect_statistics 全表聚合；空库安全降级
（计数全 0、overall_pass_rate 为 0.0、latest_batch 为 null）。
overall_pass_rate 为加权口径：累计 passed / 累计执行用例次。

**请求参数**：无。

**请求示例**：

```bash
curl.exe "http://127.0.0.1:5000/api/reports/summary"
```

**成功响应**（200）：

```json
{
  "code": 200,
  "message": "ok",
  "data": {
    "total_batches": 1,
    "total_executed": 4,
    "passed": 2,
    "failed": 2,
    "error": 0,
    "skipped": 0,
    "overall_pass_rate": 0.5,
    "latest_batch": {
      "execution_id": "RUN-20260920-103045-a1b2",
      "pass_rate": 0.5,
      "created_at": "2026-09-20T10:30:46"
    }
  }
}
```

### 5.2 GET /api/reports/trend

通过率趋势，返回最近 N 个已完成批次，按时间升序（最早在前）。

**Query 参数**：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| limit | int | 否 | 20 | 返回批次数，1 ~ 100 |

**请求示例**：

```bash
curl.exe "http://127.0.0.1:5000/api/reports/trend?limit=20"
```

**成功响应**（200，data 为数组，空表返回 []）：

```json
{
  "code": 200,
  "message": "ok",
  "data": [
    {
      "execution_id": "RUN-20260920-103045-a1b2",
      "pass_rate": 0.5,
      "total_cases": 4,
      "passed": 2,
      "failed": 2,
      "error": 0,
      "created_at": "2026-09-20T10:30:46"
    }
  ]
}
```

**错误响应**（400）：

```json
{
  "code": 400,
  "message": "limit必须在1到100之间",
  "data": null
}
```

非整数入参的文案为 `limit必须为正整数`。

### 5.3 GET /api/reports/module-distribution

模块执行分布，按模块聚合各结果计数；历史明细对应用例已被物理删除时，
该明细归入 `unknown` 模块，不丢历史数据。排序：total 降序 → module 升序。

**请求参数**：无。

**请求示例**：

```bash
curl.exe "http://127.0.0.1:5000/api/reports/module-distribution"
```

**成功响应**（200，data 为数组，空表返回 []）：

```json
{
  "code": 200,
  "message": "ok",
  "data": [
    {
      "module": "用户中心",
      "total": 2,
      "passed": 1,
      "failed": 1,
      "error": 0,
      "skipped": 0,
      "pass_rate": 0.5
    },
    {
      "module": "订单中心",
      "total": 2,
      "passed": 1,
      "failed": 1,
      "error": 0,
      "skipped": 0,
      "pass_rate": 0.5
    }
  ]
}
```

### 5.4 GET /api/reports/failed-top

失败用例 Top 榜：统计 result 为 failed/error 的明细，按 case_id 聚合计数，
返回失败次数最多的 N 条，并携带最近一次失败时间与错误信息。
排序：fail_count 降序 → case_id 升序。

**Query 参数**：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| limit | int | 否 | 10 | 返回条数，1 ~ 100 |

**请求示例**：

```bash
curl.exe "http://127.0.0.1:5000/api/reports/failed-top?limit=10"
```

**成功响应**（200，data 为数组，无失败记录返回 []）：

```json
{
  "code": 200,
  "message": "ok",
  "data": [
    {
      "case_id": "TM-OD-0001",
      "case_name": "订单创建成功校验",
      "fail_count": 1,
      "last_failed_at": "2026-09-20T10:30:45",
      "last_error_message": "模拟执行失败: 断言不通过"
    },
    {
      "case_id": "TM-UC-0001",
      "case_name": "用户登录成功校验",
      "fail_count": 1,
      "last_failed_at": "2026-09-20T10:30:45",
      "last_error_message": "模拟执行失败: 断言不通过"
    }
  ]
}
```

**错误响应**（400）：limit 非整数返回 `limit必须为正整数`，
越界返回 `limit必须在1到100之间`。

### 5.5 GET /api/reports/quality-metrics

质量度量指标，全部来自平台现有三张表数据，空库各指标返回 0.0；
code_coverage 为预留契约字段，当前恒为 null。

**指标口径**：

| 字段 | 口径 |
|---|---|
| case_execution_coverage | 执行明细 distinct case_id 数 / active 用例总数 |
| defect_density | failed+error 明细数 / 全部明细数（按执行次数） |
| problem_case_ratio | 出过 failed/error 的 distinct case_id 数 / active 用例总数 |
| execution_efficiency.avg_duration_sec | 全部明细耗时均值（秒，保留 2 位） |
| execution_efficiency.p95_duration_sec | P95 耗时；样本不足 20 条时取最大值近似 |
| execution_efficiency.avg_cases_per_batch | 明细总数 / 批次数 |
| code_coverage | 预留字段，当前恒为 null |

**请求参数**：无。

**请求示例**：

```bash
curl.exe "http://127.0.0.1:5000/api/reports/quality-metrics"
```

**成功响应**（200）：

```json
{
  "code": 200,
  "message": "ok",
  "data": {
    "case_execution_coverage": 1.0,
    "defect_density": 0.5,
    "problem_case_ratio": 0.5,
    "execution_efficiency": {
      "avg_duration_sec": 0.01,
      "p95_duration_sec": 0.01,
      "avg_cases_per_batch": 4.0
    },
    "code_coverage": null
  }
}
```

---

## 附录 A：SimulatedExecutor 模拟执行结果规则

默认执行器（simulated）不运行真实测试，按 case_id 字符中的数字决定结果，
规则固定且可预测，便于联调与数据核对：

1. 提取 case_id 中的全部数字字符，取最后一个数字；
2. 末位数字为**偶数** → `passed`，为**奇数** → `failed`；
3. case_id 不含数字时按偶数处理 → `passed`；
4. 每条用例模拟约 0.01 秒耗时；failed 时 error_message 固定为
   `模拟执行失败: 断言不通过`，passed 时 error_message 为 null。

示例：`TM-UC-0001` 末位为 1（奇数）→ failed；
`TM-UC-0002` 末位为 2（偶数）→ passed。

## 附录 B：trigger 异步执行与轮询流程

trigger 接口为异步语义，完整调用流程如下：

1. `POST /api/executions/trigger` 提交筛选条件，立即得到
   `202` 响应，data 中记录 execution_id、status=pending、total_cases；
2. 按固定间隔轮询 `GET /api/executions/{execution_id}/status`
   （建议间隔 0.3 ~ 1 秒），观察 status 由 pending → running 迁移；
3. status 到达终态 `finished` 或 `failed` 后停止轮询：
   - finished：读取计数与 pass_rate，再调
     `GET /api/executions/{execution_id}` 查看明细；
   - failed：读取 error_message 排查批次级异常；
4. 需要实时观察执行过程时，可在触发后连接
   `GET /api/executions/{execution_id}/events`（SSE），断线重连带上
   `Last-Event-ID` 请求头即可从断点续传，无需重新全量拉取。
