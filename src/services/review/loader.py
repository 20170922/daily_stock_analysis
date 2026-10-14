# -*- coding: utf-8 -*-
"""复盘结构配置加载（改配置不改代码）。

目录 ``review_structures/*.yaml`` 每个文件一种复盘结构，环境变量
``MARKET_REVIEW_STRUCTURE`` 按 ``name`` 选中，``legacy`` 表示走老链路。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

STRUCTURES_DIR = Path(__file__).resolve().parents[3] / "review_structures"
LEGACY_NAME = "legacy"


def available_structures() -> List[str]:
    """Return sorted structure names found on disk."""
    if not STRUCTURES_DIR.is_dir():
        return []
    names = []
    for path in sorted(STRUCTURES_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 - broken file skipped, listed nowhere
            logger.warning("[ReviewStructure] 解析失败已跳过: %s", path.name)
            continue
        name = str(data.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def load_structure(name: str) -> Optional[Dict[str, Any]]:
    """Load one structure config by name, None when missing/invalid."""
    key = (name or "").strip()
    if not key or key == LEGACY_NAME:
        return None
    path = STRUCTURES_DIR / f"{key}.yaml"
    # 允许 name 与文件名不一致：遍历一次按 name 匹配。
    candidates = [path] if path.is_file() else sorted(STRUCTURES_DIR.glob("*.yaml"))
    for candidate in candidates:
        try:
            data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 - fail-open, try next file
            continue
        if str(data.get("name") or "").strip() == key:
            if not isinstance(data.get("sections"), list) or not data["sections"]:
                logger.warning("[ReviewStructure] %s 缺少 sections，已忽略", candidate.name)
                return None
            return data
    logger.warning("[ReviewStructure] 未找到复盘结构: %r", key)
    return None


def resolve_structure_name(raw: Any, default: str = "deep_limitup_cn") -> str:
    """Normalize MARKET_REVIEW_STRUCTURE value."""
    name = str(raw or "").strip().lower()
    return name or default
