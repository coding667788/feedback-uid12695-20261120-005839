"""输入数据模型、校验与规范化哈希。

所有数据均为程序生成的虚构统计；结构零只由输入 structural_zeros 明示，
真实值为 0 但未在 structural_zeros 中声明的原子仍是普通变量（取值范围 [0, 20]）。
"""

from __future__ import annotations

import hashlib
import json
import weakref
from dataclasses import dataclass
from typing import Any

MAX_ATOMS = 10
MAX_CANDIDATES = 16
ATOM_VALUE_CAP = 20


class ValidationError(ValueError):
    """输入本身不合法（与“拒绝发布”不同：拒绝是合法输入下无安全方案）。"""


@dataclass(frozen=True)
class Candidate:
    """发布请求中的候选单元：若干原子之和，带公开名称、权重与必发标记。"""

    name: str
    atoms: frozenset[str]
    weight: float
    required: bool = False


@dataclass(frozen=True)
class Request:
    """一次 plan/publish 请求的完整输入。"""

    request_id: str
    data_version: str
    atom_values: dict[str, int]
    structural_zeros: frozenset[str]
    candidates: tuple[Candidate, ...]
    public_total: bool  # 不可抑制的公开总数：True 时强制发布全部原子之和


@dataclass(frozen=True)
class Equation:
    """公开等式：sum(coeffs[i] * x_i) = rhs。

    label 仅用于私有诊断，不进入任何公开输出。
    紧凑非零项通过 equation_terms() 获取（模块级缓存，求解器热路径复用）。
    """

    label: str
    coeffs: tuple[int, ...]  # 按变量索引顺序（与求解器变量表一致）
    rhs: int


_EQUATION_TERMS_CACHE: "weakref.WeakKeyDictionary[Equation, tuple]" = (
    weakref.WeakKeyDictionary()
)


def equation_terms(eq: "Equation") -> tuple[tuple[int, int], ...]:
    """返回等式的紧凑非零项 ((变量索引, 系数), ...)，带缓存。"""
    cached = _EQUATION_TERMS_CACHE.get(eq)
    if cached is None:
        cached = tuple((i, c) for i, c in enumerate(eq.coeffs) if c != 0)
        _EQUATION_TERMS_CACHE[eq] = cached
    return cached


@dataclass(frozen=True)
class PriorRelease:
    """台账中同数据版本的一次历史发布（公开等式的来源）。"""

    seq: int
    equations: tuple[Equation, ...]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def parse_request(raw: dict[str, Any]) -> Request:
    """解析并严格校验 JSON 形式的请求字典。"""
    _require(isinstance(raw, dict), "请求必须是 JSON 对象")

    request_id = raw.get("request_id")
    _require(isinstance(request_id, str) and request_id.strip(), "request_id 必须是非空字符串")

    data_version = raw.get("data_version")
    _require(isinstance(data_version, str) and data_version.strip(), "data_version 必须是非空字符串")

    atom_values = raw.get("atom_values")
    _require(isinstance(atom_values, dict) and atom_values, "atom_values 必须是非空对象")
    _require(len(atom_values) <= MAX_ATOMS, f"原子单元最多 {MAX_ATOMS} 个")
    parsed_values: dict[str, int] = {}
    for name, value in atom_values.items():
        _require(isinstance(name, str) and name, "原子名称必须是非空字符串")
        # bool 是 int 的子类，显式排除以免 True/False 混入数值
        _require(isinstance(value, int) and not isinstance(value, bool),
                 f"原子 {name} 的值必须是整数")
        _require(0 <= value <= ATOM_VALUE_CAP,
                 f"原子 {name} 的值必须落在 [0, {ATOM_VALUE_CAP}]")
        parsed_values[name] = value

    structural = raw.get("structural_zeros", [])
    _require(isinstance(structural, list), "structural_zeros 必须是数组")
    zeros: set[str] = set()
    for name in structural:
        _require(isinstance(name, str), "structural_zeros 元素必须是字符串")
        _require(name in parsed_values, f"结构零 {name} 不在原子名单中")
        _require(parsed_values[name] == 0,
                 f"结构零 {name} 的真实值必须为 0；不得把非零单元声明为结构零")
        zeros.add(name)

    candidates_raw = raw.get("candidates")
    _require(isinstance(candidates_raw, list) and candidates_raw, "candidates 必须是非空数组")
    _require(len(candidates_raw) <= MAX_CANDIDATES, f"候选单元最多 {MAX_CANDIDATES} 个")
    candidates: list[Candidate] = []
    seen_names: set[str] = set()
    for idx, item in enumerate(candidates_raw):
        _require(isinstance(item, dict), f"候选 #{idx} 必须是对象")
        name = item.get("name")
        _require(isinstance(name, str) and name.strip(), f"候选 #{idx} 缺少公开名称")
        _require(name not in seen_names, f"候选公开名称重复: {name}")
        seen_names.add(name)

        atoms = item.get("atoms")
        _require(isinstance(atoms, list) and atoms, f"候选 {name} 的 atoms 必须是非空数组")
        atom_set: set[str] = set()
        for a in atoms:
            _require(isinstance(a, str), f"候选 {name} 的原子引用必须是字符串")
            _require(a in parsed_values, f"候选 {name} 引用了未知原子 {a}")
            atom_set.add(a)

        weight = item.get("weight", 1)
        _require(isinstance(weight, (int, float)) and not isinstance(weight, bool),
                 f"候选 {name} 的权重必须是数值")
        _require(weight >= 0, f"候选 {name} 的权重不能为负")

        required = item.get("required", False)
        _require(isinstance(required, bool), f"候选 {name} 的 required 必须是布尔值")
        candidates.append(Candidate(
            name=name, atoms=frozenset(atom_set),
            weight=float(weight), required=required,
        ))

    public_total = raw.get("public_total", False)
    _require(isinstance(public_total, bool), "public_total 必须是布尔值")

    return Request(
        request_id=request_id,
        data_version=data_version,
        atom_values=parsed_values,
        structural_zeros=frozenset(zeros),
        candidates=tuple(candidates),
        public_total=public_total,
    )


def canonical_hash(payload: Any) -> str:
    """对任意 JSON 可序列化内容计算确定性的 SHA-256 摘要（用于精确重放判定）。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def candidate_true_value(candidate: Candidate, atom_values: dict[str, int]) -> int:
    return sum(atom_values[a] for a in candidate.atoms)


def write_json_atomic(path, payload: Any) -> None:
    """同目录临时文件 + os.replace，保证公开/私有文件的原子落盘。"""
    import os
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp_path = os.path.join(directory, f".{os.path.basename(path)}.tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)


def read_json(path) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
