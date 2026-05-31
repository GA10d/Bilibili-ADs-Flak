"""白名单管理器 —— 维护免删用户 UID 和评论列表，持久化到 data/whitelist.json。

格式:
{
  "uids": ["123456", "789012"],
  "names": {"123456": "用户名A"},
  "comments": {
    "BVxxx": {
      "304183614816": {"content": "评论内容", "username": "用户"}
    }
  }
}
"""

from __future__ import annotations

import json
from pathlib import Path

from src.config import APP_ROOT


class WhitelistManager:
    """管理白名单 UID 集合，线程安全（仅在主线程使用）。"""

    def __init__(self, path: Path | None = None):
        if path is None:
            path = APP_ROOT / "data" / "whitelist.json"
        self._path = path
        self._data: dict = {"uids": [], "names": {}, "comments": {}}
        self._load()

    # ---- 读写 ----

    def _load(self):
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
                if "names" not in self._data:
                    self._data["names"] = {}
                if "comments" not in self._data:
                    self._data["comments"] = {}
            except (json.JSONDecodeError, OSError):
                self._data = {"uids": [], "names": {}, "comments": {}}

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

    def contains_comment(self, bv_id: str, rpid: int | str) -> bool:
        """检查指定 BV 下的评论是否在评论白名单中。"""
        return str(rpid) in self._data.get("comments", {}).get(bv_id, {})

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

    def get_comment_info(self, bv_id: str | None = None) -> list[dict]:
        """返回评论白名单详情。

        bv_id 为 None 时返回全部；否则只返回指定 BV 下的评论。
        """
        comments = self._data.get("comments", {})
        result = []
        bv_items = [(bv_id, comments.get(bv_id, {}))] if bv_id else comments.items()
        for bv, rpid_map in bv_items:
            for rpid, meta in rpid_map.items():
                result.append({
                    "bv_id": bv,
                    "rpid": rpid,
                    "username": meta.get("username", ""),
                    "content": meta.get("content", ""),
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
        self._data["uids"] = []
        self._data["names"] = {}
        self._save()

    def add_comment(
        self,
        bv_id: str,
        rpid: int | str,
        *,
        content: str = "",
        username: str = "",
    ) -> bool:
        """添加指定 BV 下的评论到白名单。返回是否新增。"""
        rpid = str(rpid)
        comments = self._data.setdefault("comments", {})
        bv_comments = comments.setdefault(bv_id, {})
        is_new = rpid not in bv_comments
        bv_comments[rpid] = {"content": content, "username": username}
        self._save()
        return is_new

    def remove_comment(self, bv_id: str, rpid: int | str) -> bool:
        """从评论白名单移除一条评论。返回是否确实删除了。"""
        rpid = str(rpid)
        comments = self._data.setdefault("comments", {})
        bv_comments = comments.get(bv_id, {})
        if rpid in bv_comments:
            bv_comments.pop(rpid, None)
            if not bv_comments:
                comments.pop(bv_id, None)
            self._save()
            return True
        return False

    def clear_comments(self, bv_id: str | None = None):
        """清空评论白名单。bv_id 为 None 时清空全部。"""
        if bv_id is None:
            self._data["comments"] = {}
        else:
            self._data.setdefault("comments", {}).pop(bv_id, None)
        self._save()
