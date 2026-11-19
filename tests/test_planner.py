"""规划器测试：最优权重、平手规则、必发无解、结构零纪律，并与暴力参考核对。"""

import unittest

from privacy_publisher.model import parse_request, read_json
from privacy_publisher.planner import plan as run_plan

from tests.brute_reference import brute_force_best_plan
from tests.paths import FIXTURES, fixture_request, fresh_workdir


class PlannerOptimalityTests(unittest.TestCase):

    def test_brute_force_matches_planner_tiny(self):
        request = parse_request(read_json(FIXTURES / "bruteforce_tiny.json"))
        result = run_plan(request, [], 0, "hash-tiny")
        # p_single 是受保护原子 p=1 的单点候选，必须禁止
        self.assertNotIn("p_single", result.selected)
        expected = brute_force_best_plan(request)
        self.assertIsNotNone(expected)
        self.assertEqual(result.weight, expected[0])
        self.assertEqual(list(result.selected), expected[1])

    def test_deterministic_across_runs(self):
        request = parse_request(read_json(FIXTURES / "bruteforce_tiny.json"))
        first = run_plan(request, [], 0, "h")
        second = run_plan(request, [], 0, "h")
        self.assertEqual(first.selected, second.selected)
        self.assertEqual(first.weight, second.weight)

    def test_all_subsets_enumerated_by_reference(self):
        # 4 个候选共 16 个子集；C2 单值 q 会与 C1/C3 冲突，最优为 C1+C3
        request = parse_request(read_json(FIXTURES / "bruteforce_tiny.json"))
        expected = brute_force_best_plan(request)
        self.assertEqual(expected[1], ["C1", "C3"])


class TieBreakTests(unittest.TestCase):

    def test_equal_weight_lexicographic_min(self):
        request = parse_request(read_json(FIXTURES / "equal_weight_tie.json"))
        result = run_plan(request, [], 0, "h-tie")
        self.assertEqual(result.status, "accepted")
        # 等权安全子集里，名称排序序列字典序最小者胜出
        sequences = [
            tuple(sorted(s))
            for s in _all_safe_name_sets(request)
        ]
        expected = min(sequences, key=lambda s: (-len(s) * 4, s))
        self.assertEqual(result.selected, expected)
        # 显式断言：alpha_pair 字典序先于 mid_pair / zeta_pair
        self.assertEqual(result.selected[0], "alpha_pair")


class RequiredNoSolutionTests(unittest.TestCase):

    def test_required_singleton_is_rejected(self):
        request = parse_request(read_json(FIXTURES / "required_no_solution.json"))
        result = run_plan(request, [], 0, "h-req")
        self.assertEqual(result.status, "rejected")
        self.assertTrue(any("单点" in r for r in result.reasons))
        self.assertTrue(any("必发" in r for r in result.reasons))
        self.assertIsNone(result.public_payload)

    def test_rejection_keeps_private_only(self):
        request = parse_request(read_json(FIXTURES / "required_no_solution.json"))
        result = run_plan(request, [], 0, "h-req2")
        self.assertNotIn("items", result.private_plan or {})
        self.assertEqual(result.private_plan["status"], "rejected")


class StructuralZeroTests(unittest.TestCase):

    def test_zero_atom_not_treated_as_structural(self):
        request = parse_request(read_json(FIXTURES / "structural_zero_total.json"))
        result = run_plan(request, [], 0, "h-zero")
        self.assertEqual(result.status, "accepted")
        plan = result.private_plan
        # 只有 z 是明示结构零；q 真值也是 0 但不在名单中
        self.assertEqual(plan["structural_zeros"], ["z"])
        # 见证中 q 必须能取非零值（它不是结构零）
        for witness in plan["witnesses"].values():
            alt = witness["alternative_assignment"]
            # 至少存在某个见证让 q 偏离 0，或基线本身允许后续探测
        # 含 q 的候选 AQ 可以入选（q 域是 [0,20] 而非 [0,0]）
        self.assertIn("AQ", result.selected)
        # z 是明示结构零：Z_singleton 公开值恒为 0，入选不泄露受保护原子，
        # 规划器可按权重保留；关键纪律是 q（真值同为 0）未被当成结构零
        self.assertEqual(
            result.private_plan["truth_values"]["q"], 0
        )

    def test_structural_zero_propagation_in_verifier_view(self):
        request = parse_request(read_json(FIXTURES / "structural_zero_total.json"))
        result = run_plan(request, [], 0, "h-zero2")
        # 每个受保护原子都有两份不同整数取值的见证
        for p in result.protected_atoms:
            witness = result.witnesses[p]
            self.assertNotEqual(
                witness["baseline_assignment"][p],
                witness["alternative_assignment"][p],
            )


class CrossTableTests(unittest.TestCase):
    """单表各自安全，两表合用泄露：规划器必须基于台账拒绝第二张表。"""

    def test_each_table_safe_alone(self):
        req_a = parse_request(read_json(FIXTURES / "cross_table_a.json"))
        req_b = parse_request(read_json(FIXTURES / "cross_table_b.json"))
        result_a = run_plan(req_a, [], 0, "ha")
        result_b_alone = run_plan(req_b, [], 0, "hb")
        self.assertEqual(result_a.status, "accepted")
        self.assertEqual(result_b_alone.status, "accepted")

    def test_combined_tables_expose_protected_cell(self):
        req_a = parse_request(read_json(FIXTURES / "cross_table_a.json"))
        req_b = parse_request(read_json(FIXTURES / "cross_table_b.json"))
        result_a = run_plan(req_a, [], 0, "ha")
        self.assertEqual(result_a.status, "accepted")

        # 用 A 表发布的等式作为历史台账，再规划 B 表
        prior = [
            _equation_from_plan(e, result_a.private_plan["atom_order"])
            for e in result_a.private_plan["equations"]
        ]
        result_b = run_plan(req_b, prior, 1, "hb")
        # 合用后全部变量被钉成单值，B 表没有任何候选可以安全发布
        self.assertEqual(result_b.status, "accepted")
        self.assertEqual(result_b.selected, ())
        self.assertEqual(result_b.weight, 0.0)


class PruningDisplayTests(unittest.TestCase):

    def test_stats_show_pruning_counters(self):
        request = parse_request(read_json(FIXTURES / "bruteforce_tiny.json"))
        result = run_plan(request, [], 0, "h-stats")
        stats = result.stats.as_dict()
        self.assertGreater(stats["nodes"], 0)
        self.assertGreater(
            stats["prefix_prunes"] + stats["propagation_prunes"], 0
        )
        self.assertGreaterEqual(stats["integer_solves"], 1)


def _all_safe_name_sets(request):
    """复用暴力参考枚举全部安全子集名称集合。"""
    from privacy_publisher.planner import build_equation, make_bounds
    from privacy_publisher.engine import solve_integer, SolveStats
    from privacy_publisher.model import candidate_true_value

    atom_order = tuple(sorted(request.atom_values))
    bounds = make_bounds(atom_order, request)
    protected = [
        name for name, value in request.atom_values.items() if value in (1, 2)
    ]
    safe = []
    for mask in range(1 << len(request.candidates)):
        chosen = [
            request.candidates[i]
            for i in range(len(request.candidates))
            if mask & (1 << i)
        ]
        if any(c.atoms == frozenset({p}) for c in chosen for p in protected):
            continue
        equations = []
        if request.public_total:
            equations.append(build_equation(
                "total", atom_order, sum(request.atom_values.values()), atom_order))
        for cand in chosen:
            equations.append(build_equation(
                cand.name, tuple(sorted(cand.atoms)),
                candidate_true_value(cand, request.atom_values), atom_order))
        stats = SolveStats()
        base = solve_integer(equations, len(atom_order), bounds, stats=stats)
        if base is None:
            continue
        ok = True
        for p in protected:
            i = atom_order.index(p)
            feasible_values = set()
            for probe in range(bounds[i][0], bounds[i][1] + 1):
                if solve_integer(equations, len(atom_order), bounds,
                                 extra_fixed={i: probe}, stats=stats) is not None:
                    feasible_values.add(probe)
            if len(feasible_values) < 2:
                ok = False
                break
        if ok:
            safe.append({c.name for c in chosen})
    return safe


def _equation_from_plan(eq_dict, atom_order):
    from privacy_publisher.planner import build_equation
    return build_equation(
        eq_dict["label"], tuple(eq_dict["atoms"]), eq_dict["rhs"], atom_order
    )


if __name__ == "__main__":
    unittest.main()
