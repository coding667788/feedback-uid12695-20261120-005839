"""命令行入口：plan / publish / verify。

用法：
    python -m privacy_publisher plan     --request REQ.json --outdir DIR [--db D.db]
    python -m privacy_publisher publish  --request REQ.json --outdir DIR --db D.db
    python -m privacy_publisher verify   --plan PRIVATE.json --public PUB.json

公开与私有文件分目录存放；公开文件只含被选候选值与公开总数，
保护名单、见证、剪枝计数、拒绝原因只出现在私有复核文件中。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .engine import solve_integer
from .model import (
    Equation,
    Request,
    canonical_hash,
    parse_request,
    read_json,
    write_json_atomic,
)
from .planner import PROTECTED_VALUES, build_equation, make_bounds, plan as run_plan
from .store import ReplayError, StalePlanError, Store


# --------------------------------------------------------------------------- #
# 共享辅助
# --------------------------------------------------------------------------- #

def request_fingerprint(raw: dict) -> str:
    """精确重放指纹：覆盖请求中影响发布结果的全部字段。"""
    key_fields = {
        "data_version": raw["data_version"],
        "atom_values": raw["atom_values"],
        "structural_zeros": sorted(raw.get("structural_zeros", [])),
        "candidates": [
            {
                "name": c["name"],
                "atoms": sorted(c["atoms"]),
                "weight": c.get("weight", 1),
                "required": c.get("required", False),
            }
            for c in raw["candidates"]
        ],
        "public_total": raw.get("public_total", False),
    }
    return canonical_hash(key_fields)


def prior_equations_for(request: Request, store: Store):
    """从台账读取历史等式并对齐到当前请求的原子变量顺序。"""
    atom_order = tuple(sorted(request.atom_values))
    views = store.prior_equations(request.data_version)
    equations: list[Equation] = []
    for view in views:
        unknown = [a for a in view.atoms if a not in request.atom_values]
        if unknown:
            raise ValueError(
                f"同数据版本历史发布引用了当前请求中不存在的原子 {unknown}，"
                "数据版本固定时原子单元不变，请检查输入一致性"
            )
        equations.append(build_equation(view.label, view.atoms, view.rhs, atom_order))
    return equations


def _write_private_plan(outdir: str, request_id: str, private_plan: dict,
                        base_seq: int | None = None) -> str:
    """写私有复核文件。

    文件名纳入 base_seq：同一 request_id 在并发重算时会基于不同台账版本
    生成多份计划，按 request_id 单文件命名会互相覆盖，导致 verify 拿到
    与公开内容不匹配的旧计划。base_seq 为 None（拒绝场景）时退回简单命名。
    """
    private_dir = os.path.join(outdir, "private")
    suffix = f"_base{base_seq}" if base_seq is not None else ""
    path = os.path.join(private_dir, f"plan_{request_id}{suffix}.json")
    write_json_atomic(path, private_plan)
    return path


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #

def cmd_plan(args) -> int:
    raw = read_json(args.request)
    request = parse_request(raw)
    fingerprint = request_fingerprint(raw)

    store = Store(args.db, os.path.join(args.outdir, "public"))
    base_seq = store.current_seq(request.data_version)
    prior = prior_equations_for(request, store)

    result = run_plan(request, prior, base_seq, fingerprint)
    private_path = _write_private_plan(
        args.outdir, request.request_id, result.private_plan, base_seq
    )

    if result.status == "rejected":
        print(json.dumps({
            "status": "rejected",
            "request_id": request.request_id,
            "base_seq": base_seq,
            "reasons": result.reasons,
            "private_plan": private_path,
            "stats": result.stats.as_dict(),
        }, ensure_ascii=False, indent=2))
        return 2

    print(json.dumps({
        "status": "accepted",
        "request_id": request.request_id,
        "base_seq": base_seq,
        "selected": list(result.selected),
        "weight": result.weight,
        "private_plan": private_path,
        "stats": result.stats.as_dict(),
    }, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------------- #
# publish
# --------------------------------------------------------------------------- #

def cmd_publish(args) -> int:
    raw = read_json(args.request)
    request = parse_request(raw)
    fingerprint = request_fingerprint(raw)

    public_dir = os.path.join(args.outdir, "public")
    store = Store(args.db, public_dir, crash_hook=_maybe_crash_hook(args))

    # 精确重放：同请求同内容直接返回原回执，不产生新版本
    try:
        replay = store.lookup_replay(request.request_id, fingerprint)
    except ReplayError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 3
    if replay is not None:
        print(json.dumps({"status": "replay", **replay}, ensure_ascii=False, indent=2))
        return 0

    # 规划（可能因为拿锁时发现版本过时而整体重算，最多重试若干轮）
    for _attempt in range(8):
        base_seq = store.current_seq(request.data_version)
        prior = prior_equations_for(request, store)
        result = run_plan(request, prior, base_seq, fingerprint)

        if result.status == "rejected":
            private_path = _write_private_plan(
                args.outdir, request.request_id, result.private_plan, base_seq
            )
            print(json.dumps({
                "status": "rejected",
                "request_id": request.request_id,
                "reasons": result.reasons,
                "private_plan": private_path,
                "stats": result.stats.as_dict(),
            }, ensure_ascii=False, indent=2))
            return 2

        try:
            receipt = store.commit_release(
                data_version=request.data_version,
                request_id=request.request_id,
                request_hash=fingerprint,
                planned_base_seq=base_seq,
                public_payload=result.public_payload,
                private_plan=result.private_plan,
            )
        except StalePlanError as exc:
            # 并发请求抢先提交：丢弃本计划，基于新台账重算
            print(json.dumps({"status": "replan", "reason": str(exc)}, ensure_ascii=False),
                  file=sys.stderr)
            continue
        except ReplayError as exc:
            print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
            return 3

        private_path = _write_private_plan(
            args.outdir, request.request_id, result.private_plan, base_seq
        )
        restored = store.reconcile_public_files()
        print(json.dumps({
            "status": "published",
            **receipt,
            "private_plan": private_path,
            "restored_files": restored,
            "stats": result.stats.as_dict(),
        }, ensure_ascii=False, indent=2))
        return 0

    print(json.dumps({"status": "error",
                      "error": "并发冲突反复导致重算，已放弃"}, ensure_ascii=False))
    return 4


def _maybe_crash_hook(args):
    """测试钩子：--crash-before-commit REQUEST_ID 在事务提交前抛异常。"""
    target = getattr(args, "crash_before_commit", None)
    if not target:
        return None

    def hook(seq, request_id):
        if request_id == target:
            raise RuntimeError(
                f"模拟进程中断：request_id={request_id} seq={seq} 事务尚未提交"
            )

    return hook


# --------------------------------------------------------------------------- #
# verify（独立复核）
# --------------------------------------------------------------------------- #

def cmd_verify(args) -> int:
    """独立读取私有计划与公开文件，重新求解检查全部约束。

    不调用规划器的任何判定结论，只复用底层引擎；公开文件中出现私有字段即失败。
    """
    plan_doc = read_json(args.plan)
    public_doc = read_json(args.public_file)
    errors: list[str] = []
    warnings: list[str] = []

    if plan_doc.get("status") != "accepted":
        errors.append("私有计划状态不是 accepted，无可验证发布")
        print(_verify_report(errors, warnings))
        return 1

    atom_order = tuple(plan_doc["atom_order"])
    protected = tuple(plan_doc.get("protected_atoms", ()))
    n = len(atom_order)

    # --- 检查 1：公开文件泄露私有证据 ------------------------------------
    forbidden_keys = {
        "protected_atoms", "witnesses", "equations", "stats", "atom_order",
        "true_values", "reasons", "forbidden",
    }
    leaked = forbidden_keys.intersection(public_doc.keys())
    if leaked:
        errors.append(f"公开文件出现私有字段: {sorted(leaked)}")
    for item in public_doc.get("items", []):
        if set(item.keys()) != {"name", "value"}:
            errors.append(f"公开条目只能含 name/value，实际字段 {sorted(item.keys())}")

    # --- 检查 2：公开文件与计划选择一致 ----------------------------------
    plan_names = sorted(plan_doc["selected"])
    public_names = sorted(item["name"] for item in public_doc.get("items", []))
    if plan_names != public_names:
        errors.append(
            f"计划选择 {plan_names} 与公开条目 {public_names} 不一致"
        )

    # --- 检查 3：重建声明取值范围与等式，独立整数求解 --------------------
    structural = set(plan_doc.get("structural_zeros", []))
    if not structural:
        # 私有计划未冗余存储结构零时，由见证/边界推断不到则按无结构零处理，
        # 但要求计划显式记录，避免验证口径不一致
        warnings.append("私有计划未记录 structural_zeros，按全部原子 [0,20] 复核")
    bounds = [
        (0, 0) if name in structural else (0, 20)
        for name in atom_order
    ]
    equations = [
        build_equation(eq["label"], tuple(eq["atoms"]), int(eq["rhs"]), atom_order)
        for eq in plan_doc["equations"]
    ]
    assignment = solve_integer(equations, n, bounds)
    if assignment is None:
        errors.append("旧表+新表等式组不存在整数可行赋值，见证前提失败")
    else:
        for eq in equations:
            value = sum(c * assignment[i] for i, c in enumerate(eq.coeffs))
            if value != eq.rhs:
                errors.append(f"等式 {eq.label} 复核不成立: {value} != {eq.rhs}")

    # --- 检查 4：受保护原子至少两个不同整数值（独立重算，不信任见证） ---
    truth = plan_doc.get("truth_values")
    if truth is None and plan_doc.get("witnesses"):
        # 见证中记录了基线赋值，可恢复真值
        any_w = next(iter(plan_doc["witnesses"].values()))
        truth = any_w["baseline_assignment"]
    if truth is None:
        errors.append("私有计划缺少真值或见证，无法独立复核保护条件")
    else:
        for p in protected:
            i = atom_order.index(p)
            true_value = truth[p]
            if true_value not in PROTECTED_VALUES:
                errors.append(f"原子 {p} 真实值 {true_value} 不属于保护值 {{1,2}} 却被列入保护名单")
            # 独立枚举该原子在声明范围内的全部可行整数，不信任计划的结论。
            # 两阶段：先查两端点（常见安全情形只需 2 次求解），端点不足时全枚举
            # （整数投影在非全幺模约束下可能有洞，不能只查端点）。
            lo, hi = bounds[i]
            at_lo = solve_integer(equations, n, bounds, extra_fixed={i: lo})
            at_hi = solve_integer(equations, n, bounds, extra_fixed={i: hi})
            if at_lo is not None and at_hi is not None and lo != hi:
                feasible_values = {lo: at_lo, hi: at_hi}
            else:
                feasible_values = {}
                for probe in range(lo, hi + 1):
                    found = solve_integer(equations, n, bounds, extra_fixed={i: probe})
                    if found is not None:
                        feasible_values[probe] = found
            if len(feasible_values) < 2:
                errors.append(
                    f"独立复核失败：受保护原子 {p} 在公开等式下只有可行整数值 "
                    f"{sorted(feasible_values)}"
                )
            # 同时核对计划自带见证的两个赋值确实满足等式
            witness = plan_doc.get("witnesses", {}).get(p)
            if witness is None:
                errors.append(f"私有计划缺少原子 {p} 的见证")
            else:
                for key in ("baseline_assignment", "alternative_assignment"):
                    vec = witness[key]
                    for eq in equations:
                        val = sum(
                            c * vec[atom_order[i]]
                            for i, c in enumerate(eq.coeffs)
                        )
                        if val != eq.rhs:
                            errors.append(
                                f"原子 {p} 的见证 {key} 不满足等式 {eq.label}"
                            )
                if (witness["baseline_assignment"][p]
                        == witness["alternative_assignment"][p]):
                    errors.append(f"原子 {p} 的两份见证取值未发生变化")
                # 见证给出的替代值必须确实是独立枚举得到的可行值
                if witness["alternative_value"] not in feasible_values:
                    errors.append(
                        f"原子 {p} 的见证替代值 {witness['alternative_value']} "
                        "不在独立复核的可行整数集合中"
                    )

    # --- 检查 5：结构零纪律 ----------------------------------------------
    for name in atom_order:
        if name in structural and truth is not None and truth[name] != 0:
            errors.append(f"原子 {name} 被声明为结构零但真实值非零")

    report = _verify_report(errors, warnings)
    print(report)
    return 1 if errors else 0


def _verify_report(errors: list[str], warnings: list[str]) -> str:
    status = "FAIL" if errors else "PASS"
    return json.dumps(
        {"verify": status, "errors": errors, "warnings": warnings},
        ensure_ascii=False, indent=2,
    )


# --------------------------------------------------------------------------- #
# argparse 装配
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="privacy_publisher",
        description="离线隐私发布器：plan / publish / verify",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="生成发布计划（不写台账）")
    p_plan.add_argument("--request", required=True)
    p_plan.add_argument("--outdir", required=True)
    p_plan.add_argument("--db", default=None)
    p_plan.set_defaults(func=cmd_plan)

    p_pub = sub.add_parser("publish", help="事务化发布并写台账")
    p_pub.add_argument("--request", required=True)
    p_pub.add_argument("--outdir", required=True)
    p_pub.add_argument("--db", required=True)
    p_pub.add_argument("--crash-before-commit", default=None,
                        help="测试用：指定 request_id 在提交前中断")
    p_pub.set_defaults(func=cmd_publish)

    p_ver = sub.add_parser("verify", help="独立复核计划与公开文件")
    p_ver.add_argument("--plan", required=True)
    p_ver.add_argument("--public", required=True, dest="public_file")
    p_ver.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(json.dumps({"status": "error", "error": f"文件不存在: {exc}"},
                         ensure_ascii=False))
        return 3
    except ValueError as exc:
        print(json.dumps({"status": "error", "error": str(exc)},
                         ensure_ascii=False))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
