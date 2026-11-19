"""测试辅助：独立暴力枚举参考实现（与产品代码完全分开编写）。

用途：
- brute_force_best_plan: 枚举全部候选子集，用朴素笛卡尔积枚举（仅小实例）
  独立核对 planner 的最优权重与字典序平手规则；
- enumerate_integer_feasible: 枚举变量全部整数取值，核对引擎的可行性结论。
"""

from __future__ import annotations

import itertools
from typing import Callable

from privacy_publisher.model import Request, candidate_true_value

PROTECTED = frozenset({1, 2})


def _all_integer_assignments(bounds):
    ranges = [range(lo, hi + 1) for lo, hi in bounds]
    return itertools.product(*ranges)


def enumerate_integer_feasible(equations, bounds):
    """朴素枚举所有整数赋值，返回满足等式的赋值列表（仅限小值域测试）。"""
    feasible = []
    for assignment in _all_integer_assignments(bounds):
        ok = True
        for eq in equations:
            if sum(c * assignment[i] for i, c in enumerate(eq.coeffs)) != eq.rhs:
                ok = False
                break
        if ok:
            feasible.append(assignment)
    return feasible


def brute_force_best_plan(request: Request, extra_equations=()):
    """独立暴力规划：2^k 子集 × 全整数赋值枚举。

    extra_equations: 历史台账等式（Equation，已按 atom_order 对齐）。
    返回 (best_weight, best_sorted_names) 或 None；仅用于原子数 ≤ 4 的夹具。
    """
    atom_order = tuple(sorted(request.atom_values))
    n = len(atom_order)
    assert n <= 4, "暴力参考只允许小实例"
    index = {name: i for i, name in enumerate(atom_order)}
    protected_idx = [
        i for i, name in enumerate(atom_order)
        if request.atom_values[name] in PROTECTED
    ]
    bounds = [
        (0, 0) if name in request.structural_zeros else (0, 20)
        for name in atom_order
    ]

    def subset_equations(chosen):
        eqs = list(extra_equations)
        if request.public_total:
            coeffs = [1] * n
            eqs.append(_Eq(tuple(coeffs), sum(request.atom_values.values())))
        for cand in chosen:
            coeffs = [0] * n
            for a in cand.atoms:
                coeffs[index[a]] += 1
            eqs.append(_Eq(tuple(coeffs),
                           candidate_true_value(cand, request.atom_values)))
        return eqs

    def is_safe(chosen):
        # 硬性禁止：受保护原子的单点候选
        for cand in chosen:
            for i in protected_idx:
                if cand.atoms == frozenset({atom_order[i]}):
                    return False
        eqs = subset_equations(chosen)
        feasible = enumerate_integer_feasible(eqs, bounds)
        if not feasible:
            return False
        # 受保护原子必须在可行赋值集合中出现至少两个不同整数值
        for i in protected_idx:
            values = {assignment[i] for assignment in feasible}
            if len(values) < 2:
                return False
        return True

    required_names = {c.name for c in request.candidates if c.required}
    best = None  # (weight, sorted_names, subset)
    for mask in range(1 << len(request.candidates)):
        chosen = tuple(
            request.candidates[i]
            for i in range(len(request.candidates))
            if mask & (1 << i)
        )
        names = {c.name for c in chosen}
        if not required_names.issubset(names):
            continue
        if not is_safe(chosen):
            continue
        weight = sum(c.weight for c in chosen)
        sequence = tuple(sorted(names))
        if best is None or weight > best[0] or (
            weight == best[0] and sequence < best[1]
        ):
            best = (weight, sequence, chosen)
    if best is None:
        return None
    return best[0], list(best[1])


class _Eq:
    """与 privacy_publisher.model.Equation 同构的轻量本地类型。"""

    def __init__(self, coeffs, rhs):
        self.coeffs = coeffs
        self.rhs = rhs
