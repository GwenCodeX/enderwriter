"""EnderWriter 公共层：Schema、Token 估算、ID、日志、最小 JSON Schema 校验器。

此文件是**冻结契约**：所有组件（末影箱 / Memory Keeper / 编排层 / 回写器）
只依赖本文件与 `chest.store` 的公开签名，不得各自重定义数据结构。

设计约束（来自环境实测）：
  * 运行环境无 pytest / requests / fastapi，且不保证有 Ollama。
  * 因此本层与全部核心逻辑只用 Python 3.12 标准库，可完全离线运行。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import unicodedata
from typing import Any, Iterable, Sequence

__all__ = [
    "LAYERS",
    "LAYER_CN",
    "TOKEN_RATIO_ASCII",
    "est_tokens",
    "clip_tokens",
    "make_id",
    "chapter_index",
    "chapter_sort_key",
    "dumps",
    "loads",
    "get_logger",
    "SchemaError",
    "validate",
    "SCHEMAS",
    "schema",
]

# --------------------------------------------------------------------------
# 格子（末影箱的分层）
# --------------------------------------------------------------------------
LAYERS: tuple[str, ...] = (
    "character",
    "world",
    "timeline",
    "foreshadowing",
    "item",
    "style",
    # 断言格：把「事故复盘」转成「可机械判定的护栏」。
    # 与 style 一样属于跨项目资产（默认进全局层），
    # 但内容是 trigger + rule + fix，能被工具直接匹配代码。
    "assertion",
)

LAYER_CN: dict[str, str] = {
    "character": "人物格",
    "world": "世界格",
    "timeline": "时间格",
    "foreshadowing": "伏笔格",
    "item": "物品格",
    "style": "风格格",
    "assertion": "断言格",
    "alias": "称呼格",
}

#: 非 CJK 文本的 token 比例（约 4 字符 1 token，BPE 常见经验值）
TOKEN_RATIO_ASCII = 0.25


# --------------------------------------------------------------------------
# Token 估算（确定性，无第三方依赖）
# --------------------------------------------------------------------------
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x2E80, 0x2EFF),   # CJK 部首补充
    (0x3040, 0x30FF),   # 日文假名
    (0x3400, 0x4DBF),   # CJK 扩展 A
    (0x4E00, 0x9FFF),   # CJK 基本区
    (0xF900, 0xFAFF),   # CJK 兼容表意
    (0xAC00, 0xD7AF),   # 谚文
    (0xFF00, 0xFFEF),   # 全角字符
)


def _is_wide(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def est_tokens(text: str) -> int:
    """估算 token 数。

    规则（刻意保守，宁可高估）：
      * 每个 CJK/全角字符记 1 token；
      * 每 4 个其余非空白字符记 1 token（向上取整）。

    对 Qwen 系 BPE 而言中文约 0.9~1.2 token/字，此估算偏安全。
    """
    if not text:
        return 0
    wide = 0
    narrow = 0
    for ch in text:
        if ch.isspace():
            continue
        if _is_wide(ch):
            wide += 1
        else:
            narrow += 1
    return wide + math.ceil(narrow * TOKEN_RATIO_ASCII)


def clip_tokens(text: str, budget: int, suffix: str = "…") -> str:
    """按 token 预算截断文本（保证不超过 budget）。"""
    if budget <= 0:
        return ""
    if est_tokens(text) <= budget:
        return text
    out: list[str] = []
    used = 0
    tail = est_tokens(suffix)
    for ch in text:
        cost = 1 if _is_wide(ch) else (TOKEN_RATIO_ASCII if not ch.isspace() else 0.0)
        if used + cost > budget - tail:
            break
        out.append(ch)
        used += cost
    return "".join(out) + suffix


# --------------------------------------------------------------------------
# ID 与章节序
# --------------------------------------------------------------------------
def make_id(prefix: str, n: int, width: int = 3) -> str:
    """生成规范 ID，如 make_id('fs', 7) -> 'fs_007'。"""
    return f"{prefix}_{n:0{width}d}"


_CH_RE = re.compile(r"(\d+)")


def chapter_index(chapter_id: str | int | None) -> int:
    """章节 ID -> 整数序号；'ch_042' -> 42；纯数字直接返回；无法解析返回 -1。"""
    if chapter_id is None:
        return -1
    if isinstance(chapter_id, int):
        return chapter_id
    m = _CH_RE.search(str(chapter_id))
    return int(m.group(1)) if m else -1


def chapter_sort_key(chapter_id: str | int | None) -> tuple[int, str]:
    return (chapter_index(chapter_id), str(chapter_id))


# --------------------------------------------------------------------------
# 序列化与日志
# --------------------------------------------------------------------------
def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(s: str | bytes | None, default: Any = None) -> Any:
    if s is None or s == "":
        return default
    if isinstance(s, bytes):
        s = s.decode("utf-8")
    return json.loads(s)


_LOG_INIT = False


def get_logger(name: str) -> logging.Logger:
    global _LOG_INIT
    if not _LOG_INIT:
        level = os.environ.get("EW_LOG", "WARNING").upper()
        logging.basicConfig(
            level=getattr(logging, level, logging.WARNING),
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )
        _LOG_INIT = True
    return logging.getLogger(name)


# --------------------------------------------------------------------------
# 最小 JSON Schema 校验器（draft-07 子集）
# --------------------------------------------------------------------------
class SchemaError(ValueError):
    """校验失败，message 中带 JSON 指针路径。"""


def _type_ok(value: Any, t: str) -> bool:
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "null":
        return value is None
    return True


def _resolve_ref(ref: str, root: dict) -> dict:
    """解析本地 JSON 指针 $ref（'#/definitions/hit' 形态）。"""
    if not ref.startswith("#/"):
        raise SchemaError(f"仅支持本地 $ref，收到：{ref}")
    node: Any = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise SchemaError(f"$ref 无法解析：{ref}")
        node = node[part]
    if not isinstance(node, dict):
        raise SchemaError(f"$ref 目标不是对象：{ref}")
    return node


def _validate(value: Any, sch: dict, path: str, errors: list[str], root: dict | None = None) -> None:
    root = sch if root is None else root
    if "$ref" in sch:
        target = _resolve_ref(sch["$ref"], root)
        merged = {k: v for k, v in sch.items() if k != "$ref"}
        if merged:
            _validate(value, {**target, **merged}, path, errors, root)
        else:
            _validate(value, target, path, errors, root)
        return
    if "const" in sch and value != sch["const"]:
        errors.append(f"{path}: 期望常量 {sch['const']!r}，实际 {value!r}")
        return
    if "enum" in sch and value not in sch["enum"]:
        errors.append(f"{path}: 不在枚举 {sch['enum']!r} 内：{value!r}")
        return

    if "type" in sch:
        types = sch["type"] if isinstance(sch["type"], list) else [sch["type"]]
        if not any(_type_ok(value, t) for t in types):
            errors.append(f"{path}: 期望类型 {types}，实际 {type(value).__name__} ({value!r:.60})")
            return

    if "oneOf" in sch:
        ok = 0
        for sub in sch["oneOf"]:
            errs: list[str] = []
            _validate(value, sub, path, errs, root)
            ok += 0 if errs else 1
        if ok != 1:
            errors.append(f"{path}: oneOf 命中 {ok} 个分支，需恰好 1 个")
        return
    if "anyOf" in sch:
        for sub in sch["anyOf"]:
            errs = []
            _validate(value, sub, path, errs, root)
            if not errs:
                return
        errors.append(f"{path}: 不满足 anyOf 任一分支")
        return

    if isinstance(value, str):
        if "minLength" in sch and len(value) < sch["minLength"]:
            errors.append(f"{path}: 长度 {len(value)} < minLength {sch['minLength']}")
        if "pattern" in sch and not re.search(sch["pattern"], value):
            errors.append(f"{path}: 不匹配 pattern {sch['pattern']!r}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in sch and value < sch["minimum"]:
            errors.append(f"{path}: {value} < minimum {sch['minimum']}")
        if "maximum" in sch and value > sch["maximum"]:
            errors.append(f"{path}: {value} > maximum {sch['maximum']}")
    if isinstance(value, list):
        if "minItems" in sch and len(value) < sch["minItems"]:
            errors.append(f"{path}: 元素数 {len(value)} < minItems {sch['minItems']}")
        if "maxItems" in sch and len(value) > sch["maxItems"]:
            errors.append(f"{path}: 元素数 {len(value)} > maxItems {sch['maxItems']}")
        if "items" in sch:
            for i, item in enumerate(value):
                _validate(item, sch["items"], f"{path}/{i}", errors, root)
    if isinstance(value, dict):
        for req in sch.get("required", []):
            if req not in value:
                errors.append(f"{path}: 缺少必填字段 {req!r}")
        props = sch.get("properties", {})
        for k, v in value.items():
            if k in props:
                _validate(v, props[k], f"{path}/{k}", errors, root)
            else:
                ap = sch.get("additionalProperties", True)
                if ap is False:
                    errors.append(f"{path}: 出现未声明字段 {k!r}")
                elif isinstance(ap, dict):
                    _validate(v, ap, f"{path}/{k}", errors, root)


def validate(value: Any, schema: dict) -> Any:
    """校验并原样返回 value；失败抛 SchemaError（消息含全部错误）。"""
    errors: list[str] = []
    _validate(value, schema, "#", errors)
    if errors:
        raise SchemaError("；".join(errors))
    return value


def validate_or_raise(payload_json: str, schema: dict) -> Any:
    return validate(loads(payload_json), schema)


# --------------------------------------------------------------------------
# §3 规范中的四份 Schema（与 schemas/*.json 内容一致）
# --------------------------------------------------------------------------
SCHEMAS: dict[str, dict] = {
    # 3.1 大模型输出的章节细纲
    "chapter_outline": {
        "type": "object",
        "required": [
            "chapter_id",
            "chapter_goal",
            "active_characters",
            "locations",
            "items",
            "foreshadowing_to_resolve",
            "foreshadowing_to_plant",
            "mood",
            "style_keywords",
        ],
        "additionalProperties": True,
        "properties": {
            "chapter_id": {"type": "string", "pattern": r"^ch_\d+$"},
            "chapter_goal": {"type": "string", "minLength": 1},
            "active_characters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "importance"],
                    "properties": {
                        "name": {"type": "string", "minLength": 1},
                        "importance": {"enum": ["core", "secondary", "minor"]},
                    },
                    "additionalProperties": True,
                },
            },
            "locations": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "importance"],
                    "properties": {
                        "name": {"type": "string", "minLength": 1},
                        "importance": {"enum": ["core", "secondary", "minor"]},
                    },
                    "additionalProperties": True,
                },
            },
            "foreshadowing_to_resolve": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {"id": {"type": "string", "pattern": r"^fs_[A-Za-z0-9_]+$"}},
                    "additionalProperties": True,
                },
            },
            "foreshadowing_to_plant": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["content"],
                    "properties": {"content": {"type": "string", "minLength": 1}},
                    "additionalProperties": True,
                },
            },
            "mood": {"type": "string"},
            "style_keywords": {"type": "array", "items": {"type": "string"}},
        },
    },
    # 3.3 search_memory 工具入参（OpenAI function-calling 形态）
    "search_memory_tool": {
        "type": "object",
        "required": ["entities"],
        "additionalProperties": False,
        "properties": {
            "entities": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
            },
            "query_context": {"type": "string"},
            "layers": {
                "type": "array",
                "items": {"enum": list(LAYERS)},
            },
            "budget": {"type": "integer", "minimum": 1, "maximum": 8000},
            # 按叙事位置检索：只看在这一章之前已经成立的设定。
            # 长篇里同一条设定的内容会随剧情变化，写第 40 章时
            # 不该拿到第 800 章才成立的结论。
            "at_chapter": {"type": "string"},
        },
    },
    # 3.3 Memory Keeper 返回
    "memory_result": {
        "type": "object",
        "required": ["high", "medium", "low", "total_tokens"],
        "additionalProperties": True,
        "properties": {
            "high": {"type": "array", "items": {"$ref": "#/definitions/hit"}},
            "medium": {"type": "array", "items": {"$ref": "#/definitions/hit"}},
            "low": {"type": "array", "items": {"$ref": "#/definitions/hit"}},
            "total_tokens": {"type": "integer", "minimum": 0},
            "elapsed_ms": {"type": "number"},
            "normalized": {"type": "object"},
            "trace": {"type": "array", "items": {"type": "string"}},
        },
        "definitions": {
            "hit": {
                "type": "object",
                "required": ["layer", "entity", "content", "version", "last_updated", "confidence"],
                "additionalProperties": True,
                "properties": {
                    "layer": {"enum": list(LAYERS)},
                    "entity": {"type": "string"},
                    "content": {"type": "string"},
                    "version": {"type": "integer", "minimum": 1},
                    "last_updated": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "band": {"enum": ["high", "medium", "low"]},
                    "in_context": {"type": "boolean"},
                },
            }
        },
    },
    # 3.5 回写报告
    "writeback_report": {
        "type": "object",
        "required": ["chapter_id", "updated", "warnings"],
        "additionalProperties": True,
        "properties": {
            "chapter_id": {"type": "string", "pattern": r"^ch_\d+$"},
            "updated": {
                "type": "object",
                "required": ["characters", "items", "timeline", "foreshadowing"],
                "properties": {
                    "characters": {"type": "array", "items": {"type": "string"}},
                    "items": {"type": "array", "items": {"type": "string"}},
                    "timeline": {"type": "array", "items": {"type": "string"}},
                    "foreshadowing": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["id", "status"],
                            "properties": {
                                "id": {"type": "string"},
                                "status": {
                                    "enum": ["planted", "reinforced", "resolved", "abandoned"]
                                },
                                "content": {"type": "string"},
                            },
                            "additionalProperties": True,
                        },
                    },
                    "world": {"type": "array", "items": {"type": "string"}},
                    "style": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": True,
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
            "patrol": {"type": "array", "items": {"type": "string"}},
            "created": {"type": "array", "items": {"type": "string"}},
        },
    },
}


def schema(name: str) -> dict:
    try:
        return SCHEMAS[name]
    except KeyError:
        raise KeyError(f"未知 Schema：{name}；可选：{sorted(SCHEMAS)}") from None


def export_schemas(dest_dir: str) -> list[str]:
    """把内置 Schema 落盘为 JSON 文件（供外部工具/Prompt 使用）。"""
    os.makedirs(dest_dir, exist_ok=True)
    written: list[str] = []
    for name, sch in SCHEMAS.items():
        p = os.path.join(dest_dir, f"{name}.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(sch, fh, ensure_ascii=False, indent=2)
        written.append(p)
    return written


def normalize_name(name: str) -> str:
    """实体名归一化：NFKC + 去空白 + 小写化（保留中文）。"""
    return unicodedata.normalize("NFKC", str(name)).strip().lower().replace(" ", "")


def dedupe(seq: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def keyword_table(outline: dict, extra: Sequence[str] = ()) -> list[str]:
    """§3.1：关键词表从细纲直接派生，不需要额外推理。"""
    terms: list[str] = []
    terms.extend(c["name"] for c in outline.get("active_characters", []))
    terms.extend(outline.get("locations", []))
    terms.extend(i["name"] for i in outline.get("items", []))
    terms.extend(f.get("id", "") for f in outline.get("foreshadowing_to_resolve", []))
    terms.extend(f.get("content", "") for f in outline.get("foreshadowing_to_plant", []))
    terms.extend(outline.get("style_keywords", []))
    terms.extend(extra)
    return [t for t in dedupe(terms) if t]
