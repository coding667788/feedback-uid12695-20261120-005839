"""最大规模与性能回归：10 原子、16 候选的上限实例（夹具已冻结）。

约束：
- 必须在固定时间预算内完成（离线交互可接受范围）；
- DFS 节点数必须远小于无剪枝穷举空间 21^10；
- 剪枝计数器必须实证非零；
- 重复运行结果确定性一致。
"""

import time
import unittest

from privacy_publisher.model import parse_request, read_json
from privacy_publisher.planner import plan as run_plan

from tests.paths import FIXTURES


class MaxScaleTests(unittest.TestCase):

    _cached = None  # 类级缓存：两个用例共享同一次昂贵规划

    @classmethod
    def setUpClass(cls):
        request = parse_request(read_json(FIXTURES / "max_scale.json"))
        start = time.perf_counter()
        result = run_plan(request, [], 0, "h-max")
        cls._cached = (request, result, time.perf_counter() - start)

    def test_completes_within_budget_and_uses_pruning(self):
        _request, result, elapsed = self._cached

        self.assertEqual(result.status, "accepted")
        self.assertLess(elapsed, 30.0, "最大规模实例超出时间预算")

        stats = result.stats.as_dict()
        # 无剪枝穷举上限 21^10；实际节点必须小若干个数量级
        self.assertLess(stats["nodes"], 21 ** 6)
        self.assertGreater(stats["prefix_prunes"], 0)
        self.assertGreater(stats["propagation_prunes"], 0)
        # 受保护原子（a0=1,a1=2,a2=1,a3=2）都要有见证
        for p in ("a0", "a1", "a2", "a3"):
            self.assertIn(p, result.witnesses)

    def test_deterministic_across_repeated_runs(self):
        _request, first, _elapsed = self._cached
        second = run_plan(self._cached[0], [], 0, "h-max")
        self.assertEqual(first.selected, second.selected)
        self.assertEqual(first.weight, second.weight)


if __name__ == "__main__":
    unittest.main()
