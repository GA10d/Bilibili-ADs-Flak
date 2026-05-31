"""从浏览器自动抓取 B站 Cookie（SESSDATA + bili_jct）。

支持 Chrome / Edge / Firefox，Windows 平台。
"""

import json
import sqlite3
import ctypes
import ctypes.wintypes
import os
from pathlib import Path
from typing import NamedTuple

from loguru import logger


class CookiePair(NamedTuple):
    sessdata: str
    bili_jct: str


# ---- 浏览器 Cookie 路径 ----

def _chrome_cookie_path() -> Path | None:
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    if not base.exists():
        return None
    # 优先 Default，否则找第一个 Profile
    for profile in ["Default", "Profile 1", "Profile 2"]:
        p = base / profile / "Network" / "Cookies"
        if p.exists():
            return p
    return None


def _edge_cookie_path() -> Path | None:
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "User Data"
    if not base.exists():
        return None
    for profile in ["Default", "Profile 1"]:
        p = base / profile / "Network" / "Cookies"
        if p.exists():
            return p
    return None


def _firefox_cookie_path() -> Path | None:
    base = Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox" / "Profiles"
    if not base.exists():
        return None
    for d in base.iterdir():
        if d.is_dir() and (d.name.endswith(".default-release") or d.name.endswith(".default")):
            p = d / "cookies.sqlite"
            if p.exists():
                return p
    return None


# ---- Windows DPAPI 解密（Chrome / Edge） ----

def _decrypt_chrome(encrypted_value: bytes) -> str:
    """解密 Chrome/Edge 用 DPAPI 加密的 Cookie 值。"""
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    blob_in = DATA_BLOB(len(encrypted_value), ctypes.cast(
        ctypes.create_string_buffer(encrypted_value, len(encrypted_value)),
        ctypes.POINTER(ctypes.c_char),
    ))
    blob_out = DATA_BLOB()

    if crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        result = ctypes.string_at(blob_out.pbData, blob_out.cbData).decode("utf-8")
        kernel32.LocalFree(blob_out.pbData)
        return result

    raise RuntimeError("DPAPI 解密失败")


# ---- 主逻辑 ----

def _read_chrome_edge(db_path: Path) -> dict[str, str]:
    """从 Chrome/Edge 加密 Cookie 数据库读取 B站 相关值。"""
    # 复制数据库避免锁定问题
    import shutil
    import tempfile
    tmp = Path(tempfile.gettempdir()) / "baf_cookies_temp.db"
    shutil.copy2(db_path, tmp)

    try:
        conn = sqlite3.connect(str(tmp))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT name, encrypted_value FROM cookies WHERE host_key = '.bilibili.com' OR host_key = 'bilibili.com'"
        ).fetchall()
    finally:
        conn.close()
        tmp.unlink(missing_ok=True)

    cookies = {}
    for row in rows:
        name = row["name"]
        try:
            value = _decrypt_chrome(row["encrypted_value"])
            cookies[name] = value
        except Exception:
            pass  # 跳过无法解密的条目

    return cookies


def _read_firefox(db_path: Path) -> dict[str, str]:
    """从 Firefox Cookie 数据库读取 B站 相关值。"""
    import shutil
    import tempfile
    tmp = Path(tempfile.gettempdir()) / "baf_cookies_temp.db"
    shutil.copy2(db_path, tmp)

    try:
        conn = sqlite3.connect(str(tmp))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT name, value FROM moz_cookies WHERE host = '.bilibili.com' OR host = 'bilibili.com'"
        ).fetchall()
    finally:
        conn.close()
        tmp.unlink(missing_ok=True)

    return {row["name"]: row["value"] for row in rows}


def import_cookies(browser: str = "auto") -> CookiePair:
    """从浏览器导入 B站 登录 Cookie。

    Args:
        browser: "chrome" | "edge" | "firefox" | "auto"（自动检测第一个可用的）

    Returns:
        CookiePair(sessdata, bili_jct)

    Raises:
        RuntimeError: 未找到浏览器 Cookie 或未登录 B站
    """
    detectors = {
        "chrome": (_chrome_cookie_path, _read_chrome_edge),
        "edge": (_edge_cookie_path, _read_chrome_edge),
        "firefox": (_firefox_cookie_path, _read_firefox),
    }

    if browser == "auto":
        order = ["chrome", "edge", "firefox"]
    else:
        order = [browser]

    for name in order:
        if name not in detectors:
            continue
        path_fn, read_fn = detectors[name]
        db_path = path_fn()
        if db_path is None:
            logger.debug(f"未找到 {name} Cookie 数据库")
            continue

        logger.info(f"正在从 {name} 读取 Cookie…")
        try:
            cookies = read_fn(db_path)
        except Exception as e:
            logger.warning(f"读取 {name} Cookie 失败: {e}")
            continue

        sessdata = cookies.get("SESSDATA")
        bili_jct = cookies.get("bili_jct")

        if sessdata and bili_jct:
            logger.info(f"成功从 {name} 获取 SESSDATA + bili_jct")
            return CookiePair(sessdata=sessdata, bili_jct=bili_jct)
        else:
            logger.warning(f"{name} 中未找到 B站 登录 Cookie（是否已登录？）")

    raise RuntimeError(
        "未找到 B站 登录 Cookie。请先在 Chrome / Edge / Firefox 登录 bilibili.com 后再试。"
    )


def save_to_env(cookies: CookiePair, env_path: Path | None = None) -> Path:
    """将 B站 Cookie 写入 .env 文件（保留其他已有配置不覆盖）。"""
    if env_path is None:
        env_path = Path(__file__).parent.parent / ".env"

    # 读取已有内容
    existing: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                existing[key.strip()] = value.strip()

    # 更新 B站 相关字段
    existing["BAF_AUTH_MODE"] = "cookie"
    existing["BAF_SESSDATA"] = cookies.sessdata
    existing["BAF_BILI_JCT"] = cookies.bili_jct

    # 写回（保持原有顺序：先 B站 后其他）
    lines: list[str] = []
    bilibili_keys = {"BAF_AUTH_MODE", "BAF_SESSDATA", "BAF_BILI_JCT"}

    # 先写 B站 字段
    if "# bilibili related" not in " ".join(existing):
        lines.append("# bilibili related")
    for key in ("BAF_AUTH_MODE", "BAF_SESSDATA", "BAF_BILI_JCT"):
        lines.append(f"{key}={existing[key]}")

    # 再写其他已有字段
    other = {k: v for k, v in existing.items() if k not in bilibili_keys}
    if other:
        lines.append("")
        lines.append("# LLM related")
        for k, v in other.items():
            lines.append(f"{k}={v}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info(f"已更新: {env_path}")
    return env_path
