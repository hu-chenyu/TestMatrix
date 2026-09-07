"""
执行记录API蓝图（占位）

后续Day实现:
    - GET    /api/executions       执行记录列表
    - POST   /api/executions       触发执行
    - GET    /api/executions/<id>  执行详情
"""

from flask import Blueprint, jsonify

executions_bp = Blueprint(
    "executions", __name__, url_prefix="/api/executions"
)


@executions_bp.route("/")
def executions_placeholder():
    """执行记录API占位接口"""
    return jsonify(
        {"code": 200, "message": "executions API placeholder", "data": None}
    )