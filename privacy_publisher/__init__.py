"""离线隐私发布器（仅标准库）。

模块划分：
- model:   输入数据模型、校验、规范化 JSON 哈希
- engine:  区间传播 + 整数可行解 DFS（带剪枝计数），实数松弛对照
- planner: 分支限界选择候选单元，保护条件判定与见证生成
- store:   SQLite 发布台账、版本号、事务、并发与中断钩子
- cli:     plan / publish / verify 三个命令
"""

__version__ = "1.0.0"
