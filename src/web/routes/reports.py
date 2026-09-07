"""
报告API蓝图（占位）

后续Day实现:
    - GET    /api/reports          报告列表
    - GET    /api/reports/<id>     报告详情
    - GET    /api/reports/<id>/export  报告导出
"""

from flask import Blueprint, jsonify

reports_bp = Blueprint("reports", __name__, url_prefix="/api/reports")


@reports_bp.route("/")
def reports_placeholder():
    """报告API占位接口"""
    return jsonify(
        {"code": 200, "message": "reports API placeholder", "data": None}
    )