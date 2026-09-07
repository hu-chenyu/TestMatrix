"""
基础接口蓝图

提供:
    GET /              平台首页（版本信息+运行状态）
    GET /health        健康检查接口（后续Day18扩展数据库连通性检查）
    GET /api/version   版本信息接口
"""

from datetime import datetime, timezone

from flask import Blueprint, jsonify

from src.common.env_manager import env_manager

base_bp = Blueprint("base", __name__)


@base_bp.route("/")
def index():
    """
    平台首页接口

    返回:
        JSON: {"code": 200, "message": "TestMatrix API", "data": {"version": "1.0.0", "status": "running"}}
    """
    return jsonify(
        {
            "code": 200,
            "message": "TestMatrix API",
            "data": {"version": "1.0.0", "status": "running"},
        }
    )


@base_bp.route("/health")
def health():
    """
    健康检查接口

    返回当前服务运行状态、时间戳与环境信息。
    后续Day18将扩展数据库连通性检查。

    返回:
        JSON: {"code": 200, "message": "ok", "data": {"status": "healthy", "timestamp": "ISO时间", "env": "当前环境"}}
    """
    return jsonify(
        {
            "code": 200,
            "message": "ok",
            "data": {
                "status": "healthy",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "env": env_manager.current_env,
            },
        }
    )


@base_bp.route("/api/version")
def api_version():
    """
    版本信息接口

    返回:
        JSON: {"code": 200, "message": "version info", "data": {"version": "1.0.0", "api_version": "v1"}}
    """
    return jsonify(
        {
            "code": 200,
            "message": "version info",
            "data": {"version": "1.0.0", "api_version": "v1"},
        }
    )