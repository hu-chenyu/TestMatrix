"""
用例管理API蓝图（占位）

后续Day19-20实现:
    - GET    /api/cases           用例列表（分页/筛选）
    - POST   /api/cases           创建用例
    - GET    /api/cases/<id>      用例详情
    - PUT    /api/cases/<id>      更新用例
    - DELETE /api/cases/<id>      删除用例
"""

from flask import Blueprint, jsonify

cases_bp = Blueprint("cases", __name__, url_prefix="/api/cases")


@cases_bp.route("/")
def cases_placeholder():
    """用例API占位接口"""
    return jsonify(
        {"code": 200, "message": "cases API placeholder", "data": None}
    )