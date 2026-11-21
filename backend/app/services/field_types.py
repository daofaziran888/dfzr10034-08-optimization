"""字段类型归一与兼容性判断。

字段发现会收到不同加载器对同一列的类型声明，写法可能是中文类型名，
也可能是 ``varchar(64)`` 这类带精度的数据库类型。这里把它们折算成
稳定的类型族，供去重、冲突识别和字典发布共用。
"""
from __future__ import annotations

import re
from typing import Any

# 类型别名 → 类型族。键统一使用小写，比较前会去掉精度和空白。
_TYPE_ALIASES: dict[str, str] = {
    # 字符
    "字符型": "string", "字符": "string", "字符串": "string", "文本": "string",
    "string": "string", "str": "string", "char": "string", "varchar": "string",
    "nvarchar": "string", "text": "string", "clob": "string",
    # 整数
    "整数": "integer", "整型": "integer", "长整型": "integer", "短整型": "integer",
    "integer": "integer", "int": "integer", "bigint": "integer",
    "smallint": "integer", "tinyint": "integer", "long": "integer", "serial": "integer",
    # 数值
    "数值": "decimal", "数字": "decimal", "小数": "decimal", "金额": "decimal",
    "decimal": "decimal", "numeric": "decimal", "number": "decimal",
    "float": "decimal", "double": "decimal", "real": "decimal", "money": "decimal",
    # 布尔
    "布尔": "boolean", "布尔型": "boolean", "boolean": "boolean", "bool": "boolean",
    # 日期时间
    "日期": "date", "date": "date",
    "时间": "datetime", "日期时间": "datetime", "时间戳": "datetime",
    "datetime": "datetime", "timestamp": "datetime", "time": "datetime",
    # 半结构化 / 二进制
    "json": "json", "对象": "json", "jsonb": "json",
    "二进制": "binary", "binary": "binary", "blob": "binary", "bytea": "binary",
}

# 允许互相兼容的类型族：同族天然兼容；整数与数值之间允许数值加宽；
# 日期与时间戳同属时间语义，允许兼容发布。
_COMPATIBLE_FAMILIES: set[frozenset[str]] = {
    frozenset({"integer", "decimal"}),
    frozenset({"date", "datetime"}),
}

_PRECISION_RE = re.compile(r"\s*\([^)]*\)\s*$")


def normalize_type(value: Any) -> str:
    """把任意类型写法归一为类型族；无法识别时返回小写原文。"""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    lowered = _PRECISION_RE.sub("", text).strip().casefold()
    return _TYPE_ALIASES.get(lowered, lowered)


def types_compatible(left: Any, right: Any) -> bool:
    """判断两个类型声明是否兼容。

    空类型不参与冲突判断（视为未知，而不是冲突）。
    """
    a = normalize_type(left)
    b = normalize_type(right)
    if not a or not b:
        return True
    if a == b:
        return True
    return frozenset({a, b}) in _COMPATIBLE_FAMILIES


def types_conflict(left: Any, right: Any) -> bool:
    """两个已声明类型是否构成不可兼容冲突。"""
    a = normalize_type(left)
    b = normalize_type(right)
    if not a or not b:
        return False
    return not types_compatible(a, b)
