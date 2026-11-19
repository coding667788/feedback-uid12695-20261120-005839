"""引擎单元测试：传播、前缀剪枝、整数 DFS、实数松弛分歧。"""

import unittest
from fractions import Fraction

from privacy_publisher.engine import (
    SolveStats,
    prefix_feasible,
    propagate,
    real_feasible,
    solve_integer,
)
from privacy_publisher.model import Equation

from tests.brute_reference import enumerate_integer_feasible


def eq(coeffs, rhs, label="e"):
    return Equation(label=label, coeffs=tuple(coeffs), rhs=rhs)


class PropagationTests(unittest.TestCase):

    def test_propagate_pins_variable(self):
        # x1 + x2 = 3，x1 域 [0,1]，则 x2 被收紧到 [2,3]
        equations = [eq([1, 1], 3)]
        domains, _changelog = propagate(equations, [[0, 1], [0, 20]])
        self.assertEqual(domains, [[0, 1], [2, 3]])

    def test_propagate_detects_contradiction(self):
        # 两个等式互相矛盾：x+y=5 且 x+y=2
        equations = [eq([1, 1], 5), eq([1, 1], 2)]
        domains, changelog = propagate(equations, [[0, 20], [0, 20]])
        self.assertIsNone(domains)
        self.assertIsNone(changelog)

    def test_propagate_rolls_back_on_failure(self):
        # 传播失败后调用方传入的域恢复原状，不残留部分收紧
        original = [[0, 20], [0, 20]]
        equations = [eq([1, 1], 5), eq([1, 1], 2)]
        propagate(equations, original)
        self.assertEqual(original, [[0, 20], [0, 20]])

    def test_structural_zero_domain(self):
        # z 是结构零 [0,0]，a+z=2 推出 a=2
        equations = [eq([1, 1], 2)]
        domains, _changelog = propagate(equations, [[0, 20], [0, 0]])
        self.assertEqual(domains, [[2, 2], [0, 0]])


class PrefixPruneTests(unittest.TestCase):

    def test_prefix_prunes_infeasible_residual(self):
        # x1 固定为 10，x1+x2=3，x2 域 [0,20]：残差 -7 不可行
        equations = [eq([1, 1], 3)]
        domains = [[10, 10], [0, 20]]
        self.assertFalse(prefix_feasible(equations, domains, assigned_count=1))
        # 同样的约束，x1=1 时残差 2 可行
        self.assertTrue(
            prefix_feasible(equations, [[1, 1], [0, 20]], assigned_count=1)
        )

    def test_counters_are_incremented_on_hard_instance(self):
        # 无解系统：x1+x2=1 与 x1+x2=3，必须依靠剪枝快速失败而非遍历 21^2
        stats = SolveStats()
        result = solve_integer(
            [eq([1, 1], 1), eq([1, 1], 3)], 2, [(0, 20), (0, 20)], stats=stats
        )
        self.assertIsNone(result)
        self.assertGreaterEqual(stats.propagation_prunes, 1)
        self.assertGreaterEqual(stats.nodes, 1)

    def test_pruning_beats_naive_enumeration_budget(self):
        # 10 个变量、无剪枝朴素空间 21^10；紧约束下 DFS 节点数应远小于该量级
        equations = [
            eq([1] * 10, 12),
            eq([1, 1, 1, 0, 0, 0, 0, 0, 0, 0], 2),
            eq([0, 0, 1, 1, 1, 0, 0, 0, 0, 0], 3),
            eq([0, 0, 0, 0, 1, 1, 1, 0, 0, 0], 4),
            eq([0, 0, 0, 0, 0, 0, 1, 1, 1, 1], 5),
        ]
        stats = SolveStats()
        result = solve_integer(equations, 10, [(0, 20)] * 10, stats=stats)
        self.assertIsNotNone(result)
        self.assertLess(stats.nodes, 10_000)
        self.assertGreater(stats.propagation_prunes, 0)

    def test_prefix_prune_triggers_on_partial_assignment(self):
        # 根节点上传播只收紧不矛盾；x0 取高值后，x0+x1+x2=3 的残差跌破
        # x1、x2 的下界之和，前缀可行性检查在传播之前剪掉该分支。
        # 下界约束写成等式 x3=0、x4=0，并让 x1>=4、x2>=4 由独立等式表达：
        # 这里直接用窄域 [4,20] 表达下界，保持测试聚焦前缀剪枝本身。
        equations = [
            eq([1, 1, 1, 0], 3),   # x0+x1+x2=3
            eq([0, 0, 0, 1], 0),   # x3 结构零
        ]
        bounds = [(0, 20), (4, 20), (4, 20), (0, 0)]
        stats = SolveStats()
        result = solve_integer(equations, 4, bounds, stats=stats)
        self.assertIsNone(result)
        self.assertGreater(stats.prefix_prunes, 0)

    def test_naive_space_is_21_pow_10_and_we_never_enumerate_it(self):
        # 证明性断言：哪怕最松的可行实例，DFS 节点数也远小于 21^10
        equations = [eq([1] * 10, 50)]
        stats = SolveStats()
        result = solve_integer(equations, 10, [(0, 20)] * 10, stats=stats)
        self.assertIsNotNone(result)
        self.assertLess(stats.nodes, 21 ** 5)


class IntegerSolverTests(unittest.TestCase):

    def test_solution_satisfies_all_equations(self):
        equations = [
            eq([1, 1, 0], 5),
            eq([0, 1, 1], 7),
            eq([1, 0, 1], 6),
        ]
        stats = SolveStats()
        result = solve_integer(equations, 3, [(0, 20)] * 3, stats=stats)
        self.assertIsNotNone(result)
        for equation in equations:
            self.assertEqual(
                sum(c * result[i] for i, c in enumerate(equation.coeffs)),
                equation.rhs,
            )

    def test_extra_fixed_probe(self):
        equations = [eq([1, 1], 5)]
        stats = SolveStats()
        self.assertIsNotNone(
            solve_integer(equations, 2, [(0, 20)] * 2,
                          extra_fixed={0: 2}, stats=stats)
        )
        self.assertIsNone(
            solve_integer(equations, 2, [(0, 20)] * 2,
                          extra_fixed={0: 99}, stats=stats)
        )

    def test_matches_brute_force_on_small_box(self):
        equations = [
            eq([1, 1, 0], 4),
            eq([0, 1, 1], 5),
        ]
        bounds = [(0, 6), (0, 6), (0, 6)]
        brute = enumerate_integer_feasible(equations, bounds)
        stats = SolveStats()
        found = solve_integer(equations, 3, bounds, stats=stats)
        self.assertIn(found, brute)
        # 朴素枚举 7^3=343；DFS 节点显著更少
        self.assertLess(stats.nodes, 343)

    def test_infeasible_box(self):
        # x1+x2=3 但两个变量都钉在 [2,2]
        stats = SolveStats()
        result = solve_integer(
            [eq([1, 1], 3)], 2, [(2, 2), (2, 2)], stats=stats
        )
        self.assertIsNone(result)


class RealRelaxationTests(unittest.TestCase):
    """整数可行与实数可行不同的方法论对照（非全幺模奇环）。"""

    def build_odd_cycle(self):
        # 3 变量奇环（系数矩阵行列式为 2，非全幺模）：
        #   x+y=3, y+z=5, z+x=4
        return [
            eq([1, 1, 0], 3, "xy"),
            eq([0, 1, 1], 5, "yz"),
            eq([1, 0, 1], 4, "zx"),
        ]

    def test_integer_system_unique_point(self):
        equations = self.build_odd_cycle()
        bounds = [(0, 20)] * 3
        stats = SolveStats()
        result = solve_integer(equations, 3, bounds, stats=stats)
        self.assertEqual(result, (1, 2, 3))
        # 要求 x=0 的整数探测不可行：代入得 y=3, z=2, z+x=2 != 4
        self.assertIsNone(
            solve_integer(equations, 3, bounds, extra_fixed={0: 0}, stats=stats)
        )

    def test_real_relaxation_also_pins_integer_system(self):
        # 整数唯一解本身也是实数唯一解：对照求解器应给出同一有理数点
        equations = self.build_odd_cycle()
        ok, witness = real_feasible(equations, 3, [(0, 20)] * 3)
        self.assertTrue(ok)
        self.assertEqual(witness, (Fraction(1), Fraction(2), Fraction(3)))

    def test_divergence_fractional_vertex_integer_infeasible(self):
        # 带松弛变量的奇环 + 结构零，制造“实数可行、整数不可行”的探测：
        #   s+u=2, u+v=3, v+w=3, w+s=2, t=0（t 为结构零），探测 u=1
        # 真实整数点 (s,u,v,w)=(1,1,2,1) 存在；
        # u=1 探测下整数仍可行，因此改用收紧后的奇环切片制造分歧：
        # 直接构造 3 等式奇环并探测一个制造半整数顶点的边界组合。
        equations = [
            eq([1, 1, 0], 3),
            eq([0, 1, 1], 4),
            eq([1, 0, 1], 4),
        ]
        bounds = [(0, 20), (0, 20), (0, 20)]
        # 唯一解为半整数点 (1.5, 1.5, 2.5)：整数不可行、实数可行
        stats = SolveStats()
        integer_result = solve_integer(equations, 3, bounds, stats=stats)
        self.assertIsNone(integer_result)
        ok, witness = real_feasible(equations, 3, bounds, stats=stats)
        self.assertTrue(ok)
        for equation in equations:
            self.assertEqual(
                sum(c * witness[i] for i, c in enumerate(equation.coeffs)),
                equation.rhs,
            )
        self.assertTrue(any(v.denominator != 1 for v in witness))

    def test_divergence_with_box_probe(self):
        # 同一奇环：完整系统实数可行但整数不可行；
        # 加结构零把系统切成整数唯一点后，受保护变量的整数探测失败而实数成功。
        equations = [
            eq([1, 1, 0, 0], 3),
            eq([0, 1, 1, 0], 4),
            eq([1, 0, 1, 0], 4),
            eq([0, 0, 0, 1], 0),  # 结构零 t=0
        ]
        bounds = [(0, 20), (0, 20), (0, 20), (0, 0)]
        stats = SolveStats()
        # 整数语义：整个系统无解
        self.assertIsNone(solve_integer(equations, 4, bounds, stats=stats))
        # 实数语义：存在半整数解
        ok, witness = real_feasible(equations, 4, bounds, stats=stats)
        self.assertTrue(ok)
        self.assertEqual(witness[3], Fraction(0))


if __name__ == "__main__":
    unittest.main()
