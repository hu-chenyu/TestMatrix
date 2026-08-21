// ==============================================================================
// TestMatrix 性能测试Demo脚本（k6，非核心扩展示例）
// 使用方式:
//   k6 run examples/performance/k6_api_demo.js
//   自定义被测地址: k6 run -e K6_BASE_URL=http://目标服务地址 examples/performance/k6_api_demo.js
// k6安装指引: https://k6.io/docs/get-started/installation/
// ==============================================================================

import http from 'k6/http';
import { check, sleep } from 'k6';

// 压测配置: 阶梯加压模型（加压 -> 稳定 -> 降压）
export const options = {
  stages: [
    { duration: '30s', target: 20 },  // 30秒内 ramp-up 至20并发
    { duration: '1m', target: 20 },   // 20并发稳定压测1分钟
    { duration: '30s', target: 0 },   // 30秒内 ramp-down 至0
  ],
  thresholds: {
    // 性能门禁: 不达标则k6退出码非0，可直接接入CI卡点
    http_req_duration: ['p(95)<500'],  // 95分位响应耗时须低于500ms
    http_req_failed: ['rate<0.01'],    // 请求失败率须低于1%
  },
};

// 被测服务基础地址（通过 -e K6_BASE_URL=xxx 覆盖）
const BASE_URL = __ENV.K6_BASE_URL || 'http://127.0.0.1:5000';

// 默认压测场景: 健康检查接口（联调真实业务接口时替换为对应path与断言）
export default function () {
  const response = http.get(`${BASE_URL}/api/ping`);
  check(response, {
    '状态码为200': (r) => r.status === 200,
    '业务码为0': (r) => r.json('code') === 0,
  });
  sleep(1);
}
