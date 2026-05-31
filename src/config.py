"""全局配置模块。使用纯 dataclass，无额外依赖。"""

import os
from dataclasses import dataclass, field
from pathlib import Path


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
    delay_base: float = 1.5            # 基础延时（秒）
    delay_jitter: float = 0.5          # 随机抖动（秒）
    request_timeout: int = 15          # 单次请求超时（秒）
    max_retries: int = 3               # 网络错误最大重试次数
    page_size: int = 20                # 每页评论数（B站 API 默认 20，最大 20）

    # ========== 输出 ==========
    output_dir: str = "data"
    output_format: str = "json"        # "json" | "csv"

    # ========== 检查点 ==========
    checkpoint_dir: str = "data/checkpoints"

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
        """从 .env 文件 + 环境变量创建 Config（BAF_ 前缀覆盖）。

        优先级：.env 文件 < 系统环境变量
        """
        env_vars: dict[str, str] = {}

        # 1. 读取项目根目录的 .env 文件
        env_file = Path(__file__).parent.parent / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env_vars[key.strip()] = value.strip()

        # 2. 系统环境变量覆盖 .env
        env_vars.update(os.environ)

        # 3. 匹配 BAF_ 前缀的键
        kwargs = {}
        for field_name in cls.__dataclass_fields__:
            env_key = f"BAF_{field_name.upper()}"
            if env_key in env_vars:
                raw = env_vars[env_key]
                raw = raw.strip("'\"").strip()  # 去引号
                field_type = cls.__dataclass_fields__[field_name].type
                if field_type is int:
                    kwargs[field_name] = int(raw)
                elif field_type is float:
                    kwargs[field_name] = float(raw)
                else:
                    kwargs[field_name] = raw
        return cls(**kwargs)
