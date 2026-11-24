"""配置加载模块：读取 config.yaml，提供点号路径访问和默认值兜底。"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # 未安装依赖时仍能用默认配置导入模块
    yaml = None

ROOT = Path(__file__).resolve().parent.parent

# 兜底默认配置：与 config.yaml 保持一致，缺文件时也能跑
DEFAULT_CONFIG: dict[str, Any] = {
    "browser": {
        "headless": False,
        "channel": "",
        "mode": "persistent",
        "debug_ports": {"linkedin": 9222, "facebook": 9223},
        "executable": "",
        "profiles": {
            "linkedin": "./profiles/linkedin",
            "facebook": "./profiles/facebook",
        },
        "timeout_ms": 30000,
    },
    "quota": {
        "linkedin": {"post": 2, "comment": 15, "follow": 20},
        "facebook": {"post": 2, "comment": 12, "follow": 12},
    },
    "time_window": {
        "enabled": True,
        "weekdays_only": True,
        "windows": ["09:00-11:30", "14:00-17:30"],
        "jitter_minutes": 40,
        "weekend_multiplier": 0.5,
    },
    "humanize": {
        "enabled": True,
        "intensity": 0.8,
        "warmup": {
            "enabled": True,
            "scroll_screens_min": 2,
            "scroll_screens_max": 4,
            "pause_min_s": 15,
            "pause_max_s": 60,
            "unrelated_click_probability": 0.3,
        },
        "reading_chars_per_second": 15,
        "reading_jitter": 0.4,
        "typing": {"min_delay_ms": 60, "max_delay_ms": 280, "typo_probability": 0.08},
        "idle_between_tasks_min_s": 60,
        "idle_between_tasks_max_s": 300,
        "idle_browse_minutes": 5,
    },
    "human_intervention": {
        "human_ratio": 0.25,
        "confirm_before_post": True,
        "confirm_before_first_follow": True,
        "takeover_every_n": 10,
    },
    "circuit_breaker": {
        "keywords": [
            "验证码", "verify you are human", "account restricted",
            "账号受限", "unusual activity", "异常活动",
        ],
        "cooldown_hours": 24,
    },
    "storage": {
        "database": "./data/social.db",
        "content_dir": "./content",
        "logs_dir": "./data/logs",
    },
    "run": {
        "max_posts_per_platform_per_run": 1,
    },
    "comment": {
        "imperfection_probability": 0.08,
        "primary_language": "en",
        "require_topic_match": True,
    },
    "schedule": {
        "sessions_per_day": [2, 4],
        "session_gap_minutes": [90, 240],
        "skip_day_probability": 0.1,
        "grace_minutes": 45,
        "browse_minutes": [2, 6],
        "comment_probability": 0.55,
        "comments_per_session": [1, 2],
        "post_probability": 0.35,
        "extra_activity_probability": 0.5,
        "comment_keywords": [],
        "warmup_days": 14,
        "warmup_sessions_per_day": [1, 3],
        "warmup_comment_probability": 0.3,
        "warmup_comments_per_session": [0, 1],
    },
    "platforms": {
        "linkedin": {
            "base_url": "https://www.linkedin.com",
            "feed_url": "https://www.linkedin.com/feed",
        },
        "facebook": {
            "base_url": "https://www.facebook.com",
            "feed_url": "https://www.facebook.com",
        },
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并配置，override 覆盖 base。"""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    """配置对象，支持 get('a.b.c') 点号访问。"""

    def __init__(self, data: dict, root: Path = ROOT):
        self._data = data
        self.root = root

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        path = Path(path) if path else ROOT / "config.yaml"
        data = copy.deepcopy(DEFAULT_CONFIG)
        if path.exists():
            if yaml is None:
                import warnings
                warnings.warn("PyYAML 未安装，使用默认配置。请运行 pip install -r requirements.txt")
            else:
                with path.open("r", encoding="utf-8") as f:
                    user_data = yaml.safe_load(f) or {}
                if not isinstance(user_data, dict):
                    raise ValueError(f"配置文件格式错误：{path}")
                data = _deep_merge(data, user_data)
        else:
            import warnings
            warnings.warn(f"配置文件不存在：{path}，使用默认配置")
        return cls(data, ROOT)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, dotted: str, default: str = "") -> Path:
        """把配置里的相对路径解析为项目根目录下的绝对路径。"""
        raw = self.get(dotted, default)
        p = Path(str(raw))
        return p if p.is_absolute() else (self.root / p)

    def as_dict(self) -> dict:
        return copy.deepcopy(self._data)
