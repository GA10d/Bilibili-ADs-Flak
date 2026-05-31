"""白名单管理器 —— 维护免删用户UID列表，持久化到 data/whitelist.json。

格式: {"uids": ["123456", "789012"], "names": {"123456": "用户名A"}}
"""

from __future__ import annotations

import json
from pathlib import Path


class WhitelistManager:
    """管理白名单 UID 集合，线程安全（仅在主线程使用）。"""

    def __init__(self, path: Path | None = None):
        if path is None:
            path = Path(__file__).parent.parent / "data" / "whitelist.json"
        self._path = path
        self._data: dict = {"uids": [], "names": {}}
        self._load()

    # ---- 读写 ----

    def _load(self):
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
                if "names" not in self._data:
                    self._data["names"] = {}
            except (json.JSONDecodeError, OSError):
                self._data = {"uids": [], "names": {}}

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ---- 查询 ----

    def contains(self, uid: int | str) -> bool:
        """检查 uid 是否在白名单中。"""
        return str(uid) in self._data["uids"]

    def get_uids(self) -> list[str]:
        """返回所有白名单 UID 列表。"""
        return list(self._data["uids"])

    def get_info(self) -> list[dict]:
        """返回白名单详情：[{"uid": "...", "name": "..."}, ...]"""
        result = []
        for uid in self._data["uids"]:
            result.append({
                "uid": uid,
                "name": self._data["names"].get(uid, ""),
            })
        return result

    # ---- 修改 ----

    def add(self, uid: int | str, name: str = "") -> bool:
        """添加 uid 到白名单。已存在则只更新名称。返回是否新增。"""
        uid = str(uid)
        is_new = uid not in self._data["uids"]
        if is_new:
            self._data["uids"].append(uid)
        if name:
            self._data["names"][uid] = name
        self._save()
        return is_new

    def remove(self, uid: int | str) -> bool:
        """从白名单移除 uid。返回是否确实删除了。"""
        uid = str(uid)
        if uid in self._data["uids"]:
            self._data["uids"].remove(uid)
            self._data["names"].pop(uid, None)
            self._save()
            return True
        return False

    def update_name(self, uid: int | str, name: str):
        """更新白名单用户备注名。"""
        uid = str(uid)
        if uid in self._data["uids"]:
            self._data["names"][uid] = name
            self._save()

    def clear(self):
        """清空白名单。"""
        self._data = {"uids": [], "names": {}}
        self._save()
