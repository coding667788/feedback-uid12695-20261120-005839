"""规划器：在保护约束下做最大权重候选选择，并生成私有复核见证。

选择问题（最多 16 个候选，2^16 可管理，但仍做剪枝）：
- 必发项必须入选；
- 含受保护原子单点值的候选直接禁止入选；
- 任一候选子集 S 与历史公开等式合并后，必须让每个受保护原子
  （真实值 1 或 2）至少能取两个不同的整数值；
- 最大化保留权重；等权时取“已选公开名称排序序列”字典序最小者。

保护判定是单调的：新增等式只会收紧可行域，所以不安全集合的超集一律剪枝。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .engine import SolveStats, real_feasible, solve_integer
from .model import (
    ATOM_VALUE_CAP,
    Candidate,
    Equation,
    Request,
    candidate_true_value,
)

PROTECTED_VALUES = frozenset({1, 2})


@dataclass
class PlanResult:
    """plan 的完整结果（含私有信息，调用方负责决定写哪个文件）。"""

    status: str  # "accepted" | "rejected"
    request_id: str
    data_version: str
    request_hash: str
    base_seq: int  # 规划时台账已发布的最大序号（0 表示空台账）
    selected: tuple[str, ...]
    weight: float
    reasons: list[str]
    protected_atoms: tuple[str, ...]
    witnesses: dict[str, dict]
    equations: list[dict]  # 旧表+新表全部公开等式（私有复核视图）
    stats: SolveStats
    public_payload: dict | None  # 仅含被选候选值与公开总数的公开内容
    private_plan: dict  # 完整私有计划文件内容


def make_bounds(atom_order: tuple[str, ...], request: Request) -> list[tuple[int, int]]:
    """声明取值范围：结构零明示为 [0,0]，其余一律 [0,20]。

    真实值为 0 但未声明为结构零的原子保持 [0,20]，不得当成结构零。
    """
    return [
        (0, 0) if name in request.structural_zeros else (0, ATOM_VALUE_CAP)
        for name in atom_order
    ]


def build_equation(label: str, atoms: tuple[str, ...], rhs: int,
                   atom_order: tuple[str, ...]) -> Equation:
    index = {name: i for i, name in enumerate(atom_order)}
    coeffs = [0] * len(atom_order)
    for name in atoms:
        coeffs[index[name]] += 1
    return Equation(label=label, rhs=rhs, coeffs=tuple(coeffs))


def _check_safety(equations: list[Equation], atom_order: tuple[str, ...],
                  bounds: list[tuple[int, int]], protected: tuple[str, ...],
                  truth: tuple[int, ...], stats: SolveStats,
                  solve_memo: dict | None = None,
                  also_real: bool = False
                  ) -> tuple[bool, dict[str, dict], list[str]]:
    """检查等式组对每个受保护原子是否容许至少两个不同整数值。

    不能只探测真值±1：原子可能被等式钉在偏离真值的单点上（例如真值 2
    却被锁成 0）。线性等式 + 整数盒域在单变量上的整数投影可能有洞
    （系数矩阵非全幺模时），所以严格判定分两阶段：
      1) 先探测声明范围的两个端点；两端点都可行则投影至少含两个不同整数，
         直接安全（常见情形，成本为每原子 2 次求解）；
      2) 否则完整枚举该原子声明范围内的每个整数，独立求解收集全部可行值。
    同一 (等式组, 固定变量) 的可行性结果做记忆化，跨原子/跨候选复用。
    每个原子可由不同可行解给出证据（逐原子独立探测）。
    """
    index = {name: i for i, name in enumerate(atom_order)}
    truth_assignment = tuple(truth)
    equations_key = tuple((eq.rhs, eq.coeffs) for eq in equations)
    feasible_cache: dict[tuple, tuple[int, ...] | None] = {}

    def probe(var_idx: int, value: int):
        key = (equations_key, var_idx, value)
        if key not in feasible_cache:
            feasible_cache[key] = solve_integer(
                equations, len(atom_order), bounds,
                extra_fixed={var_idx: value}, stats=stats, memo=solve_memo,
            )
        return feasible_cache[key]

    # 基线：真实赋值必须可行（正常输入下恒成立，防御性校验）
    baseline = solve_integer(
        equations, len(atom_order), bounds, stats=stats, memo=solve_memo
    )
    if baseline is None:
        return False, [], ["等式组整体无整数可行解（真实赋值都不满足，输入或台账异常）"]

    witnesses: dict[str, dict] = {}
    failures: list[str] = []
    for p in protected:
        i = index[p]
        true_value = truth[i]
        lo, hi = bounds[i]
        at_lo = probe(i, lo)
        at_hi = probe(i, hi)
        if at_lo is not None and at_hi is not None and lo != hi:
            # 阶段 1：两端点可行 => 至少两个不同整数值
            feasible_values = {lo: at_lo, hi: at_hi}
        else:
            # 阶段 2：完整枚举（含端点，缓存命中不重复求解）
            feasible_values = {}
            for candidate_value in range(lo, hi + 1):
                found = probe(i, candidate_value)
                if found is not None:
                    feasible_values[candidate_value] = found
        if len(feasible_values) < 2:
            failures.append(
                f"受保护原子 {p}（真实值 {true_value}）在全部公开等式下"
                f"只有可行整数值 {sorted(feasible_values)}，保护失败"
            )
            if also_real:
                # 方法论对照：同一约束在实数松弛下可能可行
                ok, real_witness = real_feasible(
                    equations, len(atom_order), bounds, stats=stats,
                )
                if ok:
                    failures.append(
                        "  对照：实数松弛存在半整数可行点"
                        f" {_fmt_fraction_assignment(real_witness, atom_order)}，"
                        "但整数语义下无足够取值"
                    )
            continue
        # 见证：优先选取偏离真值的可行值（离真值最近者），保证两份见证取值不同
        other_value = min(
            (v for v in feasible_values if v != true_value),
            key=lambda v: (abs(v - true_value), v),
            default=next(iter(feasible_values)),
        )
        alt_assignment = feasible_values[other_value]
        witnesses[p] = {
            "true_value": true_value,
            "alternative_value": other_value,
            "baseline_assignment": dict(zip(atom_order, truth_assignment)),
            "alternative_assignment": dict(zip(atom_order, alt_assignment)),
        }
    if failures:
        return False, {}, failures
    return True, witnesses, []


def _fmt_fraction_assignment(assignment, atom_order) -> dict:
    return {
        name: (float(v) if v.denominator != 1 else int(v))
        for name, v in zip(atom_order, assignment)
    }


def plan(request: Request, prior_equations: list[Equation], base_seq: int,
         request_hash: str) -> PlanResult:
    """执行规划。prior_equations 已按本函数的变量顺序对齐。"""
    atom_order = tuple(sorted(request.atom_values))
    bounds = make_bounds(atom_order, request)
    truth = tuple(request.atom_values[name] for name in atom_order)
    protected = tuple(
        name for name in atom_order
        if request.atom_values[name] in PROTECTED_VALUES
    )
    stats = SolveStats()
    # 跨全部候选子集共享的整数求解缓存，避免对相似等式组重复 DFS
    solve_memo: dict = {}

    # --- 步骤 1：硬性禁止——含受保护原子单点值的候选 -----------------------
    forbidden: dict[str, str] = {}
    searchable: list[Candidate] = []
    for cand in request.candidates:
        hits = [p for p in protected if cand.atoms == frozenset({p})]
        if hits:
            forbidden[cand.name] = (
                f"候选 {cand.name} 是受保护原子 {hits[0]} 的单点值，禁止发布"
            )
        else:
            searchable.append(cand)

    reasons: list[str] = []
    required = [c for c in request.candidates if c.required]
    hard_required_blocked = [
        forbidden[c.name] for c in required if c.name in forbidden
    ]
    if hard_required_blocked:
        reasons.extend(hard_required_blocked)
        reasons.append("必发项含被禁止的单点候选，无安全方案")
        return _reject(request, base_seq, request_hash, protected, stats, reasons)

    # --- 步骤 2：构造候选等式（按公开名称排序，保证确定性） ---------------
    ordered = sorted(searchable, key=lambda c: c.name)
    cand_equations: dict[str, Equation] = {
        cand.name: build_equation(
            f"candidate:{cand.name}",
            tuple(sorted(cand.atoms)),
            candidate_true_value(cand, request.atom_values),
            atom_order,
        )
        for cand in ordered
    }
    total_equation = None
    if request.public_total:
        total_equation = build_equation(
            "public_total", atom_order, sum(truth), atom_order
        )

    def equations_for(selected_keys: frozenset[str]) -> list[Equation]:
        eqs = list(prior_equations)
        if total_equation is not None:
            eqs.append(total_equation)
        eqs.extend(cand_equations[name] for name in sorted(selected_keys))
        return eqs

    # 安全判定缓存：键为已选候选集合。
    # 保护约束关于“新增等式”单调：若集合 S 不安全，则 S 的任何超集都不安全。
    safety_cache: dict[frozenset[str], tuple[bool, dict, list[str]]] = {}
    unsafe_sets: set[frozenset[str]] = set()

    def safe(selected_keys: frozenset[str]):
        cached = safety_cache.get(selected_keys)
        if cached is not None:
            return cached
        # 单调性短路：任一直接子集已判定不安全，则本集合必然不安全
        for name in selected_keys:
            if selected_keys - {name} in unsafe_sets:
                result = (False, {}, ["单调性剪枝：子集已破坏保护"])
                safety_cache[selected_keys] = result
                unsafe_sets.add(selected_keys)
                return result
        result = _check_safety(
            equations_for(selected_keys), atom_order, bounds,
            protected, truth, stats, solve_memo=solve_memo,
        )
        safety_cache[selected_keys] = result
        if not result[0]:
            unsafe_sets.add(selected_keys)
        return result

    required_names = frozenset(c.name for c in required if c.name not in forbidden)
    weights = {cand.name: cand.weight for cand in ordered}
    suffix_weight = [0.0] * (len(ordered) + 1)
    for i in range(len(ordered) - 1, -1, -1):
        suffix_weight[i] = suffix_weight[i + 1] + weights[ordered[i].name]

    # 空集合先测一次：公开总数/旧表本身可能就破坏保护（单原子极端情形）
    base_safe, base_witnesses, base_failures = safe(frozenset())
    if not base_safe:
        reasons.append("不可抑制的公开总数与历史发布已使保护失败：")
        reasons.extend("  " + f for f in base_failures)
        if required:
            reasons.append("必发项在此前提下无法安全发布")
        return _reject(request, base_seq, request_hash, protected, stats, reasons)

    best: tuple[float, tuple[str, ...], dict] | None = None  # (权重, 名称序列, 见证)

    # 贪心构造一个高质量初始可行解，让分支限界尽早获得紧的最优下界
    greedy_set: set[str] = set(required_names)
    for cand in ordered:
        if cand.name in greedy_set:
            continue
        trial = frozenset(greedy_set | {cand.name})
        ok, witnesses, _fails = safe(trial)
        if ok:
            greedy_set.add(cand.name)
    if required_names.issubset(greedy_set):
        ok, witnesses, _fails = safe(frozenset(greedy_set))
        if ok:
            best = (
                sum(weights[n] for n in greedy_set),
                tuple(sorted(greedy_set)), witnesses,
            )

    def dfs(idx: int, chosen: frozenset[str], current_weight: float) -> None:
        nonlocal best
        # 分支限界：乐观上界仍不敌当前最优则整棵子树剪掉（含其安全检查）
        if best is not None and current_weight + suffix_weight[idx] < best[0]:
            return
        # 字典序剪枝：即使拿满剩余权重也只能与当前最优持平，若当前已选名称
        # 的排序序列在字典序上已严格大于最优序列，则后续不可能反超
        if best is not None and current_weight + suffix_weight[idx] == best[0]:
            current_seq = tuple(sorted(chosen))
            best_seq = best[1]
            # 取两者等长前缀比较；当前序列出现更大的名称即退出
            if current_seq > best_seq[:len(current_seq)]:
                return
        if idx == len(ordered):
            if not required_names.issubset(chosen):
                return
            ok, witnesses, failures = safe(chosen)
            if not ok:
                return
            sequence = tuple(sorted(chosen))
            if best is None or current_weight > best[0] or (
                current_weight == best[0] and sequence < best[1]
            ):
                best = (current_weight, sequence, witnesses)
            return

        cand = ordered[idx]
        # 必发项只有“入选”分支
        branches = (True, False) if not cand.required else (True,)
        for take in branches:
            if take:
                next_chosen = chosen | {cand.name}
                # 单调性剪枝：当前集合已不安全，任何超集都不安全
                ok, _w, failures = safe(next_chosen)
                if not ok:
                    stats.prefix_prunes += 1  # 计入前缀类剪枝计数展示
                    continue
                dfs(idx + 1, next_chosen, current_weight + weights[cand.name])
            else:
                dfs(idx + 1, chosen, current_weight)

    dfs(0, frozenset(), 0.0)

    if best is None:
        reasons.append("不存在同时满足全部保护条件与必发项的候选子集")
        # 给出必发项为何失败的具体诊断
        if required_names:
            ok, _w, failures = safe(frozenset(required_names))
            if not ok:
                reasons.append("必发项集合本身的保护诊断：")
                reasons.extend("  " + f for f in failures)
        return _reject(request, base_seq, request_hash, protected, stats, reasons)

    weight, sequence, witnesses = best
    selected_equations = equations_for(frozenset(sequence))
    public_payload = _build_public_payload(request, sequence)
    private_plan = {
        "plan_version": 1,
        "status": "accepted",
        "request_id": request.request_id,
        "data_version": request.data_version,
        "request_hash": request_hash,
        "base_seq": base_seq,
        "atom_order": list(atom_order),
        "truth_values": dict(zip(atom_order, truth)),
        "structural_zeros": sorted(request.structural_zeros),
        "protected_atoms": list(protected),
        "selected": list(sequence),
        "weight": weight,
        "forbidden": forbidden,
        "witnesses": witnesses,
        "equations": [
            {"label": eq.label,
             "atoms": [atom_order[i] for i, c in enumerate(eq.coeffs) if c],
             "rhs": eq.rhs}
            for eq in selected_equations
        ],
        "stats": stats.as_dict(),
    }
    return PlanResult(
        status="accepted",
        request_id=request.request_id,
        data_version=request.data_version,
        request_hash=request_hash,
        base_seq=base_seq,
        selected=sequence,
        weight=weight,
        reasons=[],
        protected_atoms=protected,
        witnesses=witnesses,
        equations=private_plan["equations"],
        stats=stats,
        public_payload=public_payload,
        private_plan=private_plan,
    )


def _reject(request: Request, base_seq: int, request_hash: str,
            protected: tuple[str, ...], stats: SolveStats,
            reasons: list[str]) -> PlanResult:
    """拒绝：只生成私有拒绝原因，公开状态由调用方保持不变。"""
    private_plan = {
        "plan_version": 1,
        "status": "rejected",
        "request_id": request.request_id,
        "data_version": request.data_version,
        "request_hash": request_hash,
        "base_seq": base_seq,
        "protected_atoms": list(protected),
        "reasons": reasons,
        "stats": stats.as_dict(),
    }
    return PlanResult(
        status="rejected",
        request_id=request.request_id,
        data_version=request.data_version,
        request_hash=request_hash,
        base_seq=base_seq,
        selected=(),
        weight=0.0,
        reasons=reasons,
        protected_atoms=protected,
        witnesses={},
        equations=[],
        stats=stats,
        public_payload=None,
        private_plan=private_plan,
    )


def _build_public_payload(request: Request, sequence: tuple[str, ...]) -> dict:
    """公开输出只含被选候选及其值，以及指定的公开总数。"""
    by_name = {c.name: c for c in request.candidates}
    payload: dict = {
        "data_version": request.data_version,
        "request_id": request.request_id,
        "items": [
            {
                "name": name,
                "value": candidate_true_value(by_name[name], request.atom_values),
            }
            for name in sequence
        ],
    }
    if request.public_total:
        payload["public_total"] = {
            "name": "public_total",
            "value": sum(request.atom_values.values()),
        }
    return payload
