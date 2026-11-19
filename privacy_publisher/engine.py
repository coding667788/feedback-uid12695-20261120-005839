"""约束求解引擎。

公开等式统一写成线性形式  sum(coeffs[i] * x_i) = rhs ，变量为整数原子。

两类剪枝（均带计数器，禁止无剪枝穷举 21^10）：

1. 前缀可行性剪枝 prefix_feasible：
   按固定的变量顺序 0..n-1，已赋值前缀视为常量，对每条等式计算剩余后缀
   在当前值域上的和区间，rhs 落在区间外即剪枝。这是经典的前缀和可行性检查，
   不依赖传播收紧，单独计数。
2. 区间传播 propagate：
   对每条等式做和区间运算，反过来收紧每个变量的整数上下界，到达不动点；
   出现空域即剪枝（传播剪枝单独计数）。

整数 DFS 在此之上按最小值域（MRV）选变量分支。
另提供精确有理数（Fraction）实数松弛求解 real_feasible，用于方法论对照：
某些非全幺模约束组实数可行却整数不可行（见 tests 中的奇环实例）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from .model import Equation, equation_terms


@dataclass
class SolveStats:
    """一次完整规划过程中累积的求解/剪枝计数。"""

    nodes: int = 0
    prefix_prunes: int = 0
    propagation_prunes: int = 0
    integer_solves: int = 0
    memo_hits: int = 0
    real_solves: int = 0
    solutions_found: int = 0

    def as_dict(self) -> dict:
        return {
            "nodes": self.nodes,
            "prefix_prunes": self.prefix_prunes,
            "propagation_prunes": self.propagation_prunes,
            "integer_solves": self.integer_solves,
            "memo_hits": self.memo_hits,
            "real_solves": self.real_solves,
            "solutions_found": self.solutions_found,
        }


# --------------------------------------------------------------------------- #
# 整数区间传播
# --------------------------------------------------------------------------- #

def _constraint_index(equations: list[Equation], n_vars: int):
    """构造（或从缓存取）等式组的静态索引：紧凑项与变量→约束映射。

    同一等式组在保护探测中被反复求解，索引只依赖等式结构而与域无关，
    因此按等式元组身份缓存复用。
    """
    key = tuple(equations)
    cached = _INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    terms_list = [equation_terms(eq) for eq in equations]
    var_to_eqs: list[list[int]] = [[] for _ in range(n_vars)]
    for eq_idx, terms in enumerate(terms_list):
        for i, _ in terms:
            var_to_eqs[i].append(eq_idx)
    cached = (terms_list, var_to_eqs)
    _INDEX_CACHE[key] = cached
    return cached


_INDEX_CACHE: dict = {}


def propagate(equations: list[Equation], domains: list[list[int]],
              index=None):
    """整数区间传播（队列式 AC-3 风格增量传播，原地修改 + 变更日志）。

    每条约束维护参与变量的和区间；变量域收紧时只重新处理涉及它的约束。
    成功返回 (domains, changelog)，changelog 记录每个被改变变量的原域，
    供调用方回溯恢复；矛盾时恢复所有已做修改并返回 (None, None)。
    index 可传入 _constraint_index 的结果以跳过静态索引重建。
    """
    if index is None:
        terms_list, var_to_eqs = _constraint_index(equations, len(domains))
    else:
        terms_list, var_to_eqs = index
    changelog: list[tuple[int, list[int]]] = []

    def rollback():
        for i, original in reversed(changelog):
            domains[i] = original

    def sum_interval(terms):
        lo_sum = hi_sum = 0
        for i, c in terms:
            lo, hi = domains[i]
            if c > 0:
                lo_sum += c * lo
                hi_sum += c * hi
            else:  # 通用符号处理（本应用系数实际为 0/1）
                lo_sum += c * hi
                hi_sum += c * lo
        return lo_sum, hi_sum

    # 初始时所有约束都待处理
    queue = list(range(len(equations)))
    queued = [True] * len(equations)
    head = 0
    while head < len(queue):
        eq_idx = queue[head]
        head += 1
        queued[eq_idx] = False
        eq = equations[eq_idx]
        terms = terms_list[eq_idx]
        lo_sum, hi_sum = sum_interval(terms)
        if lo_sum > eq.rhs or hi_sum < eq.rhs:
            rollback()
            return None, None
        for i, c in terms:
            lo, hi = domains[i]
            if c > 0:
                others_min = lo_sum - c * lo
                others_max = hi_sum - c * hi
                # c*x in [rhs-others_max, rhs-others_min]
                new_lo = max(lo, _ceil_div(eq.rhs - others_max, c))
                new_hi = min(hi, _floor_div(eq.rhs - others_min, c))
            else:
                others_min = lo_sum - c * hi
                others_max = hi_sum - c * lo
                # c<0 时不等式反向
                new_hi = min(hi, _floor_div(eq.rhs - others_min, c))
                new_lo = max(lo, _ceil_div(eq.rhs - others_max, c))
            if new_lo > new_hi:
                rollback()
                return None, None
            if new_lo != lo or new_hi != hi:
                changelog.append((i, domains[i]))
                domains[i] = [new_lo, new_hi]
                # 域收紧：把涉及该变量的其他约束重新入队
                for affected in var_to_eqs[i]:
                    if not queued[affected]:
                        queued[affected] = True
                        queue.append(affected)
    return domains, changelog


def _ceil_div(a: int, b: int) -> int:
    # b 可能为负；Python // 是地板除，ceil(a/b) = -floor(-a/b)
    return -((-a) // b)


def _floor_div(a: int, b: int) -> int:
    return a // b


def prefix_feasible(equations: list[Equation], domains: list[list[int]],
                    assigned_count: int, index=None) -> bool:
    """前缀可行性剪枝。

    变量按规范顺序 0..n-1 排列，前 assigned_count 个已赋值（域为单点）。
    对每条等式，把前缀贡献固定，检查后缀变量在当前域上能否补足残差。
    """
    if index is None:
        terms_list, _var_to_eqs = _constraint_index(equations, len(domains))
    else:
        terms_list, _var_to_eqs = index
    for eq, terms in zip(equations, terms_list):
        fixed = 0
        rem_min = 0
        rem_max = 0
        for i, c in terms:
            lo, hi = domains[i]
            if i < assigned_count:
                fixed += c * lo  # 单点域 lo == hi
            elif c > 0:
                rem_min += c * lo
                rem_max += c * hi
            else:
                rem_min += c * hi
                rem_max += c * lo
        residual = eq.rhs - fixed
        if residual < rem_min or residual > rem_max:
            return False
    return True


# --------------------------------------------------------------------------- #
# 整数可行解 DFS
# --------------------------------------------------------------------------- #

def solve_integer(equations: list[Equation], n_vars: int,
                  bounds: list[tuple[int, int]],
                  extra_fixed: dict[int, int] | None = None,
                  stats: SolveStats | None = None,
                  memo: dict | None = None) -> tuple[int, ...] | None:
    """求一个满足全部等式与边界的整数赋值；不可行返回 None。

    extra_fixed: 保护探测用的额外单点约束 {变量索引: 必须取的值}。
    赋值顺序固定为变量索引顺序（前缀剪枝依赖），选变量用 MRV。
    memo: 可选的跨调用可行性缓存，键为 (等式指纹, 边界, 固定变量)。
    规划器对大量相似等式组反复探测，命中缓存可省去整棵 DFS。
    """
    if stats is None:
        stats = SolveStats()
    stats.integer_solves += 1

    fixed = tuple(sorted((extra_fixed or {}).items()))
    cache_key = None
    if memo is not None:
        cache_key = (
            tuple((eq.rhs, eq.coeffs) for eq in equations),
            tuple(bounds), fixed,
        )
        if cache_key in memo:
            cached = memo[cache_key]
            stats.memo_hits += 1
            if cached is not None:
                stats.solutions_found += 1
            return cached

    domains = [[lo, hi] for lo, hi in bounds]
    for idx, value in (extra_fixed or {}).items():
        if not (domains[idx][0] <= value <= domains[idx][1]):
            if memo is not None:
                memo[cache_key] = None
            return None
        domains[idx] = [value, value]

    index = _constraint_index(equations, n_vars)
    result = _dfs_integer(equations, domains, stats, index)
    if memo is not None:
        memo[cache_key] = result  # None 不可行也缓存
    return result


def _dfs_integer(equations, domains: list[list[int]], stats: SolveStats, index):
    stats.nodes += 1
    n = len(domains)
    assigned_count = 0
    for lo, hi in domains:
        if lo == hi:
            assigned_count += 1

    # 全部变量已单点：前缀检查即完整校验，无需再跑传播
    if assigned_count == n:
        if prefix_feasible(equations, domains, n, index):
            stats.solutions_found += 1
            return tuple(lo for lo, _ in domains)
        stats.prefix_prunes += 1
        return None

    # 剪枝 1：前缀可行性（先于传播，使用父节点继承下来的域）
    if not prefix_feasible(equations, domains, assigned_count, index):
        stats.prefix_prunes += 1
        return None

    # 剪枝 2：区间传播收紧（原地），失败时它已自行回滚
    tightened, changelog = propagate(equations, domains, index)
    if tightened is None:
        stats.propagation_prunes += 1
        return None

    # MRV：单趟遍历选值域最小的未定值变量（平手取最小索引，保证确定性）
    var = -1
    best_width = 10 ** 9
    for i, (lo, hi) in enumerate(domains):
        if lo != hi:
            width = hi - lo
            if width < best_width:
                best_width = width
                var = i

    lo, hi = domains[var]
    try:
        for value in range(lo, hi + 1):
            original = domains[var]
            domains[var] = [value, value]
            result = _dfs_integer(equations, domains, stats, index)
            if result is not None:
                return result
            domains[var] = original
    finally:
        # 回溯传播造成的全部域收紧
        for i, original in reversed(changelog):
            domains[i] = original
    return None


# --------------------------------------------------------------------------- #
# 实数松弛（精确 Fraction，用于对照整数语义）
# --------------------------------------------------------------------------- #

def real_feasible(equations: list[Equation], n_vars: int,
                  bounds: list[tuple[int, int]],
                  extra_fixed: dict[int, Fraction] | None = None,
                  stats: SolveStats | None = None
                  ) -> tuple[bool, tuple[Fraction, ...] | None]:
    """判断实数松弛是否可行，可行时返回一个有理数见证赋值。

    做法：实数区间传播 + 高斯-若尔当消元（RREF）+ 自由变量回溯。
    全程 Fraction 精确运算，无浮点误差。
    """
    if stats is None:
        stats = SolveStats()
    stats.real_solves += 1

    domains: list[list[Fraction]] = [[Fraction(lo), Fraction(hi)] for lo, hi in bounds]
    for idx, value in (extra_fixed or {}).items():
        value = Fraction(value)
        if not (domains[idx][0] <= value <= domains[idx][1]):
            return False, None
        domains[idx] = [value, value]

    tightened = _propagate_real(equations, domains)
    if tightened is None:
        return False, None

    # 高斯-若尔当消元，检测 0 = 非零 矛盾
    matrix = [[Fraction(c) for c in eq.coeffs] + [Fraction(eq.rhs)] for eq in equations]
    _rref_mat, _rref_pivots, inconsistent = _rref(matrix, n_vars)
    if inconsistent:
        return False, None

    witness = _real_dfs(equations, tightened, stats)
    return (witness is not None), witness


def _propagate_real(equations, domains: list[list[Fraction]]):
    """实数版区间传播，不做取整。"""
    domains = [d[:] for d in domains]
    indexed = [[(i, c) for i, c in enumerate(eq.coeffs) if c != 0] for eq in equations]
    changed = True
    while changed:
        changed = False
        for eq, terms in zip(equations, indexed):
            lo_sum = Fraction(0)
            hi_sum = Fraction(0)
            for i, c in terms:
                lo, hi = domains[i]
                if c > 0:
                    lo_sum += c * lo
                    hi_sum += c * hi
                else:
                    lo_sum += c * hi
                    hi_sum += c * lo
            if lo_sum > eq.rhs or hi_sum < eq.rhs:
                return None
            for i, c in terms:
                lo, hi = domains[i]
                if c > 0:
                    others_min = lo_sum - c * lo
                    others_max = hi_sum - c * hi
                    new_lo = max(lo, (Fraction(eq.rhs) - others_max) / c)
                    new_hi = min(hi, (Fraction(eq.rhs) - others_min) / c)
                else:
                    others_min = lo_sum - c * hi
                    others_max = hi_sum - c * lo
                    new_hi = min(hi, (Fraction(eq.rhs) - others_min) / c)
                    new_lo = max(lo, (Fraction(eq.rhs) - others_max) / c)
                if new_lo > new_hi:
                    return None
                if new_lo != lo or new_hi != hi:
                    domains[i] = [new_lo, new_hi]
                    changed = True
    return domains


def _rref(matrix: list[list[Fraction]], n_vars: int):
    """高斯-若尔当消元；返回 (RREF, 主元列序列, 是否矛盾)。"""
    mat = [row[:] for row in matrix]
    pivots: list[int] = []
    row = 0
    for col in range(n_vars):
        pivot = next((r for r in range(row, len(mat)) if mat[r][col] != 0), None)
        if pivot is None:
            continue
        mat[row], mat[pivot] = mat[pivot], mat[row]
        scale = mat[row][col]
        mat[row] = [v / scale for v in mat[row]]
        for r in range(len(mat)):
            if r != row and mat[r][col] != 0:
                factor = mat[r][col]
                mat[r] = [a - factor * b for a, b in zip(mat[r], mat[row])]
        pivots.append(col)
        row += 1
    inconsistent = any(
        all(v == 0 for v in mat[r][:n_vars]) and mat[r][n_vars] != 0
        for r in range(len(mat))
    )
    return mat, pivots, inconsistent


def _real_dfs(equations, domains, stats: SolveStats):
    """自由变量回溯：可行凸集在某变量上的投影是区间，优先试边界。"""
    if all(lo == hi for lo, hi in domains):
        assignment = tuple(lo for lo, _ in domains)
        # 显式校验全部等式
        for eq in equations:
            if sum(c * assignment[i] for i, c in enumerate(eq.coeffs)) != eq.rhs:
                return None
        stats.solutions_found += 1
        return assignment

    var = next(i for i, (lo, hi) in enumerate(domains) if lo != hi)
    lo, hi = domains[var]
    mid = (lo + hi) / 2
    for candidate in (lo, hi, mid):  # 边界优先，中点兜底
        child = [d[:] for d in domains]
        child[var] = [candidate, candidate]
        tightened = _propagate_real(equations, child)
        if tightened is None:
            continue
        result = _real_dfs(equations, tightened, stats)
        if result is not None:
            return result
    return None
