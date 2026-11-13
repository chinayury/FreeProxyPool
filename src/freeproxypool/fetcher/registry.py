# -*- coding: utf-8 -*-
"""采集源注册表。

集中管理"有哪些源、叫什么名字"，避免管理后台、网关、调度器各自
用不同方式去 ``os.listdir`` + 反射扫描（那是旧版三处重复实现）。
"""
import importlib
import pkgutil
from typing import Dict, List, Optional, Type

from .baseFetcher import BaseFetcher

_registry: Optional[Dict[str, Type[BaseFetcher]]] = None


def _scan() -> Dict[str, Type[BaseFetcher]]:
    """扫描 sources 包，注册所有声明了 ``name`` 的子类。"""
    from . import sources

    found: Dict[str, Type[BaseFetcher]] = {}
    for module_info in pkgutil.iter_modules(sources.__path__):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module("%s.%s" % (sources.__name__, module_info.name))
        for attr in vars(module).values():
            if (isinstance(attr, type) and issubclass(attr, BaseFetcher)
                    and attr is not BaseFetcher and attr.name):
                found[attr.name] = attr
    return found


def registry() -> Dict[str, Type[BaseFetcher]]:
    """返回全部采集源（按 name 排序的字典）。"""
    global _registry
    if _registry is None:
        _registry = _scan()
    return _registry


def discover() -> List[Type[BaseFetcher]]:
    """返回全部采集源类，按 name 排序。"""
    return sorted(registry().values(), key=lambda cls: cls.name)


def get_fetcher(name: str) -> Optional[Type[BaseFetcher]]:
    """按名称查找采集源类。"""
    return registry().get(name)


def reload_registry() -> None:
    """强制重新扫描（新增源文件后无需重启进程）。"""
    global _registry
    _registry = None


__all__ = ["discover", "get_fetcher", "registry", "reload_registry"]