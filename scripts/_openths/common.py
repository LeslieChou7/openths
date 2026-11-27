from typing import Any


class QueryFailure(Exception):
    """仅携带本工具定义的安全错误类别。"""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def select(values: Any, index: int) -> Any:
    """拒绝空列表、越界或隐式选择。"""
    if not 0 <= index < len(values):
        raise QueryFailure("selection")
    return values[index]


def unique_object(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate key")
    return obj


def invalid_number(value):
    raise ValueError("non-finite number")
