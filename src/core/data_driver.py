"""
YAML/Excel数据驱动引擎（第二阶段实现）

规划能力:
    - YAML测试数据文件解析（testdata/yaml/*.yaml）
    - Excel测试数据文件解析（testdata/excel/*.xlsx，基于openpyxl）
    - 数据与用例的参数化映射，支持pytest.mark.parametrize自动装配
    - 数据文件格式校验与友好错误提示（基于marshmallow）

第一阶段说明:
    Demo用例中的YAML数据读取由tests/conftest.py的轻量fixture支撑，
    完整数据驱动引擎（Excel解析/格式校验/数据与用例联动）在第二阶段实现。
"""

# TODO(第二阶段): 实现DataDriver类，统一load_yaml/load_excel数据加载入口
