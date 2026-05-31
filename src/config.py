"""全局配置模块。使用纯 dataclass，无额外依赖。"""

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent
APP_ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else PROJECT_ROOT
ENV_FILE = APP_ROOT / ".env"


def resource_path(relative_path: str) -> Path:
    """Return a source-tree or PyInstaller bundled resource path."""
    bundle_root = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
    return bundle_root / relative_path

DEFAULT_ENV_VARS: tuple[tuple[str, str, str], ...] = (
    ("BAF_AUTH_MODE", "anonymous", "anonymous | cookie"),
    ("BAF_SESSDATA", "", "Bilibili SESSDATA, filled by Cookie import"),
    ("BAF_BILI_JCT", "", "Bilibili bili_jct/csrf, filled by Cookie import"),
    ("DEEPSEEK_API_KEY", "", "DeepSeek API key"),
    ("DEEPSEEK_MODEL", "deepseek-chat", "deepseek-chat | deepseek-reasoner"),
)


def ensure_env_file(env_file: Path = ENV_FILE) -> bool:
    """Ensure .env exists and contains all known GUI/runtime keys.

    Existing values are preserved. Returns True when the file was created or
    amended, which lets callers optionally log the first-run setup.
    """
    changed = False
    existing_keys: set[str] = set()

    if env_file.exists():
        lines = env_file.read_text(encoding="utf-8").splitlines()
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, _ = stripped.partition("=")
            existing_keys.add(key.strip())
    else:
        lines = [
            "# Bilibili ADs Flak configuration",
            "# Values here are local secrets/settings and should not be committed.",
            "",
        ]
        changed = True

    missing = [(key, value, hint) for key, value, hint in DEFAULT_ENV_VARS if key not in existing_keys]
    if missing:
        if lines and lines[-1].strip():
            lines.append("")
        if not any(line.strip() == "# Runtime configuration" for line in lines):
            lines.append("# Runtime configuration")
        for key, value, hint in missing:
            lines.append(f"# {hint}")
            lines.append(f"{key}={value}")
        changed = True

    if changed:
        env_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return changed


def _apply_field(kwargs: dict, field_name: str, raw: str) -> None:
    """类型转换并写入 kwargs。"""
    field_info = Config.__dataclass_fields__.get(field_name)
    if field_info is None:
        return
    raw = raw.strip("'\"").strip()
    field_type = field_info.type
    if field_type is int:
        kwargs[field_name] = int(raw)
    elif field_type is float:
        kwargs[field_name] = float(raw)
    else:
        kwargs[field_name] = raw


@dataclass
class Config:
    """应用全局配置，所有模块共享。

    支持环境变量覆盖（前缀 BAF_），例如：
        BAF_AUTH_MODE=cookie
        BAF_DELAY_BASE=2.0
    """

    # ========== 认证 ==========
    auth_mode: str = "anonymous"       # "anonymous" | "cookie"
    sessdata: str | None = None
    bili_jct: str | None = None

    # ========== 爬取 ==========
    max_reply_depth: int = 2           # 递归深度（1=仅一级评论）
    delay_base: float = 1.0            # 基础延时（秒）
    delay_jitter: float = 0.5          # 随机抖动（秒）
    request_timeout: int = 15          # 单次请求超时（秒）
    max_retries: int = 3               # 网络错误最大重试次数
    page_size: int = 20                # 每页评论数（B站 API 默认 20，最大 20）

    # ========== 输出 ==========
    output_dir: str = "data"
    output_format: str = "json"        # "json" | "csv"

    # ========== 检查点 ==========
    checkpoint_dir: str = "data/checkpoints"

    # ========== LLM ==========
    deepseek_api_key: str | None = None
    deepseek_model: str = "deepseek-chat"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_timeout: int = 60           # API 超时（秒）
    deepseek_max_retries: int = 3

    # ========== 日志 ==========
    log_dir: str = "logs"
    log_level: str = "INFO"

    # ---- 属性 ----

    @property
    def output_path(self) -> Path:
        return Path(self.output_dir)

    @property
    def checkpoint_path(self) -> Path:
        return Path(self.checkpoint_dir)

    @property
    def log_path(self) -> Path:
        return Path(self.log_dir)

    # ---- 工厂方法 ----

    @classmethod
    def from_env(cls) -> "Config":
        """从 .env 文件 + 环境变量创建 Config（BAF_ 前缀 + DEEPSEEK_API_KEY）。"""
        env_vars: dict[str, str] = {}

        # 1. 读取项目根目录的 .env 文件
        env_file = ENV_FILE
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env_vars[key.strip()] = value.strip()

        # 2. 系统环境变量覆盖 .env
        env_vars.update(os.environ)

        # 3. 匹配所有已知配置键
        kwargs = {}

        # BAF_ 前缀的字段（bilibili 相关）
        for field_name in cls.__dataclass_fields__:
            env_key = f"BAF_{field_name.upper()}"
            if env_key in env_vars:
                _apply_field(kwargs, field_name, env_vars[env_key])

        # DeepSeek 相关字段（LLM 相关，直接从 env 读取）
        for field_name in ("deepseek_api_key", "deepseek_model", "deepseek_base_url"):
            raw = env_vars.get(field_name.upper())
            if raw:
                _apply_field(kwargs, field_name, raw)

        return cls(**kwargs)
