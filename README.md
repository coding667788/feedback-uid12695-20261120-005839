# 离线隐私发布器（Offline Privacy Publisher）

在完全离线、仅依赖 Python 3.12 标准库的环境中，对程序生成的虚构分类统计做
**互补抑制（complementary suppression）** 发布：在保留有用汇总的同时，
持续隐藏真实值为 1 或 2 的小单元，并保证多张表先后发布时的跨表一致性。

- 无网络、无真实数据、无外部求解器；整数可行性由自带的区间传播 + DFS 求解。
- 发布台账使用标准库 `sqlite3`（WAL 模式），支持事务、并发与崩溃恢复。
- 所有数据均为虚构输入；结构零只由输入显式声明。

## 目录结构

```
privacy_publisher/
  model.py    输入模型、严格校验、规范化哈希、原子落盘
  engine.py   整数区间传播（AC-3 风格队列传播）、前缀可行性剪枝、
              整数 DFS、精确有理数（Fraction）实数松弛对照
  planner.py  分支限界候选选择、保护条件判定、私有见证生成
  store.py    SQLite 台账：版本序号、精确重放、单事务提交、并发锁、崩溃钩子
  cli.py      plan / publish / verify 三个命令
fixtures/     全部场景的虚构请求夹具（JSON）
tests/        自动化测试（unittest），含独立暴力枚举参考实现
run_tests.bat / run_tests.sh   测试入口
```

## 命令用法

```bash
# 1) plan：只生成计划与私有复核文件，不写台账、不改公开状态
python -m privacy_publisher plan \
    --request fixtures/smoke.json --outdir work/smoke --db work/smoke/ledger.db

# 2) publish：事务化发布，写台账并落盘公开文件
python -m privacy_publisher publish \
    --request fixtures/smoke.json --outdir work/smoke --db work/smoke/ledger.db

# 3) verify：独立读取私有计划与公开文件，重新求解复核
python -m privacy_publisher verify \
    --plan work/smoke/private/plan_smoke-1_base0.json \
    --public work/smoke/public/release_000001.json
```

退出码：`0` 成功；`2` 规划被拒绝（无安全方案，原因只进私有文件）；
`3` 输入/文件/重放冲突错误；`4` 并发冲突反复重算后放弃。

`publish` 另提供测试钩子 `--crash-before-commit REQUEST_ID`，
在事务提交前抛异常以模拟进程中断。

## 请求格式

```json
{
  "request_id": "smoke-1",
  "data_version": "dv-smoke",
  "atom_values": {"a": 1, "b": 2, "c": 5, "d": 0},
  "structural_zeros": ["d"],
  "public_total": true,
  "candidates": [
    {"name": "AB", "atoms": ["a", "b"], "weight": 3, "required": false}
  ]
}
```

- `atom_values`：1–10 个非负整数原子，每个值 ∈ [0, 20]。数据版本固定时不变。
- `structural_zeros`：**明示**结构零名单；其真实值必须为 0。真实值为 0
  但不在名单中的原子仍是普通变量（域 [0, 20]），不会被当作结构零。
- `candidates`：1–16 个候选单元，每项是若干原子之和，含公开名称、权重、
  可选 `required` 必发标记。
- `public_total`：为 `true` 时公开总数不可抑制，强制发布。

## 隐私保护语义

1. 真实值为 1 或 2 的原子都是受保护对象。
2. 含某受保护原子**单点值**的候选直接禁止发布。
3. 对每个受保护原子，把“同数据版本全部历史公开等式 + 本次拟公开等式 +
   声明取值范围”作为约束组，要求该原子在约束下**至少存在两个不同的整数
   可行值**。每个原子可由不同可行解给出证据。
4. 整数投影在系数矩阵非全幺模时可能有洞，因此判定分两阶段：先探测取值
   范围两端点；端点不足以证明时完整枚举该原子的全部候选整数独立求解。
5. 保护对象名单、每个原子的两份见证赋值、剪枝计数、拒绝原因**只写入
   `private/` 复核文件**；`public/` 公开文件只含被选候选的 `name/value`
   和指定的公开总数。

## 优化目标与平手规则

在满足全部保护条件与必发项的候选子集中：

1. 最大化保留权重之和；
2. 权重相同（平手）时，取“已选公开名称排序后的序列”字典序最小者。

无解时输出私有拒绝原因并**保留旧发布**，台账不新增版本、公开目录无新文件。

## 求解与剪枝（禁止无剪枝穷举 21^10）

- **区间传播**：队列式 AC-3 风格增量传播，按和区间反推每个变量的整数上下界，
  原地修改 + 变更日志回滚；约束静态索引跨求解复用。
- **前缀可行性剪枝**：变量按规范顺序赋值，固定前缀后检查每条等式残差能否
  被后缀变量的当前值域补足。
- 整数 DFS 按最小值域（MRV）选变量分支。
- 规划层分支限界：贪心初始可行解 + 权重上界剪枝 + 字典序剪枝 +
  保护单调性剪枝（不安全集合的超集一律不探索）+ 跨子集求解记忆化。
- 每次规划的 `stats` 输出 `nodes / prefix_prunes / propagation_prunes /
  integer_solves / memo_hits / solutions_found`，实证剪枝发生。
- 另提供精确 `Fraction` 实数松弛求解（高斯-若尔当 + 区间传播），用于
  “实数可行但整数不可行”的方法论对照（见奇环夹具与引擎测试）。

## 事务、并发与重放

- 每次发布在单个 `BEGIN IMMEDIATE` 事务内插入 `releases`、`receipts`、
  更新 `ledger_state`；提交成功后才原子替换写公开文件。
- 拿锁后重新核对 `base_seq`：计划基于的台账版本过时则抛 `StalePlanError`，
  CLI 丢弃旧计划、基于新台账重算。两个并发请求不可能各自通过旧状态而
  合起来暴露保护格。
- 同 `request_id` + 同请求内容哈希视为**精确重放**，直接返回原回执、
  不产生新版本；同 `request_id` 不同内容则拒绝。
- 提交前崩溃：事务回滚，台账与公开目录均无痕迹，可干净重试。
- 提交后、公开文件落盘前的窗口：`reconcile_public_files()` 可由台账重建。

## verify 的独立性

`verify` 不读取规划器的判定结论，只复用底层引擎：

- 检查公开文件不含任何私有字段；
- 重建声明取值范围与“旧表 + 新表”全部等式，独立求整数可行赋值；
- 对每个受保护原子独立枚举可行整数集合，确认至少两个不同值；
- 核对计划自带两份见证赋值确实满足全部等式且取值不同；
- 核对结构零纪律。

## 测试

```bash
run_tests.bat          # Windows
./run_tests.sh         # Git Bash
# 或
python -m unittest discover -s tests -v
```

覆盖场景（对应任务要求）：

| 要求 | 测试位置 |
| --- | --- |
| 单表安全、两表合用泄露 | `CrossTableTests`、`CrossTableCliTests` |
| 整数可行与实数可行不同 | `RealRelaxationTests`（奇环/半整数顶点） |
| 必发项导致无解 | `RequiredNoSolutionTests`、`RejectionTests` |
| 等权平手字典序 | `TieBreakTests` |
| 公开总和与结构零 | `StructuralZeroTests`、`PlanPublishVerifyFlowTests` |
| 并发与中断发布 | `ConcurrencyTests`、`CrashTests` |
| 可穷举小实例独立核对最优权重与确定性 | `PlannerOptimalityTests` + `tests/brute_reference.py` |
| 剪枝计数与 21^10 上限 | `PrefixPruneTests`、`MaxScaleTests` |

暴力参考实现 `tests/brute_reference.py` 与产品代码独立编写，枚举全部
2^k 候选子集和全部整数赋值，仅用于原子数 ≤ 4 的小夹具交叉核对。
