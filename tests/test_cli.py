"""CLI 端到端测试：plan/publish/verify、重放、并发与中断发布。

所有子进程用 `python -I` 运行并显式设置 cwd，避免夹具目录中的同名模块污染；
测试代码本身放在 tests/，与夹具目录分离。
"""

import json
import subprocess
import sys
import threading
import unittest
from pathlib import Path

from privacy_publisher.model import read_json, write_json_atomic
from privacy_publisher.store import Store

from tests.paths import FIXTURES, cleanup, fresh_workdir

ROOT = Path(__file__).resolve().parent.parent


def run_cli(*args: str) -> tuple[int, dict | str]:
    """以子进程方式运行 CLI，返回 (退出码, JSON 输出或原文)。

    使用 -I 隔离模式（忽略环境变量与用户 site-packages，防止夹具目录里的
    同名模块污染），并通过 sys.path 插入项显式指向工程根目录。
    """
    bootstrap = (
        "import sys; "
        f"sys.path.insert(0, {str(ROOT)!r}); "
        "sys.stdout.reconfigure(encoding='utf-8'); "
        "sys.stderr.reconfigure(encoding='utf-8'); "
        "from privacy_publisher.cli import main; "
        "sys.exit(main())"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", bootstrap, *args],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    out = proc.stdout.strip()
    try:
        return proc.returncode, json.loads(out) if out else proc.stderr
    except json.JSONDecodeError:
        return proc.returncode, out + proc.stderr


class PlanPublishVerifyFlowTests(unittest.TestCase):

    def setUp(self):
        self.work = fresh_workdir()
        self.db = str(self.work / "ledger.db")
        self.outdir = str(self.work / "out")
        self.request = str(FIXTURES / "structural_zero_total.json")

    def tearDown(self):
        cleanup(self.work)

    def test_plan_then_publish_then_verify(self):
        code, plan_out = run_cli(
            "plan", "--request", self.request,
            "--outdir", self.outdir, "--db", self.db,
        )
        self.assertEqual(code, 0, plan_out)
        self.assertEqual(plan_out["status"], "accepted")

        code, pub_out = run_cli(
            "publish", "--request", self.request,
            "--outdir", self.outdir, "--db", self.db,
        )
        self.assertEqual(code, 0, pub_out)
        self.assertEqual(pub_out["status"], "published")
        seq = pub_out["seq"]

        public_path = Path(self.outdir) / "public" / f"release_{seq:06d}.json"
        # 私有计划文件名带 base_seq 后缀，直接取发布回执给出的路径
        private_path = Path(pub_out["private_plan"])
        public_doc = read_json(public_path)

        # 公开文件只能含被选候选值与公开总数，绝无私有证据
        self.assertNotIn("witnesses", public_doc)
        self.assertNotIn("protected_atoms", public_doc)
        self.assertNotIn("equations", public_doc)
        for item in public_doc["items"]:
            self.assertEqual(set(item.keys()), {"name", "value"})
        self.assertIn("public_total", public_doc)

        code, verify_out = run_cli(
            "verify", "--plan", str(private_path), "--public", str(public_path),
        )
        self.assertEqual(code, 0, verify_out)
        self.assertEqual(verify_out["verify"], "PASS")

    def test_private_plan_contains_protected_and_witnesses(self):
        code, plan_out = run_cli("plan", "--request", self.request,
                                 "--outdir", self.outdir, "--db", self.db)
        self.assertEqual(code, 0)
        private_path = Path(plan_out["private_plan"])
        doc = read_json(private_path)
        self.assertIn("a", doc["protected_atoms"])  # a=2 受保护
        self.assertIn("a", doc["witnesses"])
        witness = doc["witnesses"]["a"]
        self.assertNotEqual(
            witness["baseline_assignment"]["a"],
            witness["alternative_assignment"]["a"],
        )


class ReplayTests(unittest.TestCase):

    def setUp(self):
        self.work = fresh_workdir()
        self.db = str(self.work / "ledger.db")
        self.outdir = str(self.work / "out")
        self.request = str(FIXTURES / "bruteforce_tiny.json")

    def tearDown(self):
        cleanup(self.work)

    def test_exact_replay_returns_same_receipt(self):
        code, first = run_cli(
            "publish", "--request", self.request,
            "--outdir", self.outdir, "--db", self.db,
        )
        self.assertEqual(code, 0)
        code, second = run_cli(
            "publish", "--request", self.request,
            "--outdir", self.outdir, "--db", self.db,
        )
        self.assertEqual(code, 0)
        self.assertEqual(second["status"], "replay")
        self.assertEqual(second["seq"], first["seq"])
        # 没有新增发布文件
        public_files = list((Path(self.outdir) / "public").glob("release_*.json"))
        self.assertEqual(len(public_files), 1)

    def test_same_request_id_different_content_rejected(self):
        code, _ = run_cli(
            "publish", "--request", self.request,
            "--outdir", self.outdir, "--db", self.db,
        )
        self.assertEqual(code, 0)
        modified = read_json(Path(self.request))
        modified["atom_values"] = dict(modified["atom_values"], q=4)
        tampered = self.work / "tampered.json"
        write_json_atomic(str(tampered), modified)
        code, out = run_cli(
            "publish", "--request", str(tampered),
            "--outdir", self.outdir, "--db", self.db,
        )
        self.assertEqual(code, 3)
        self.assertEqual(out["status"], "error")


class RejectionTests(unittest.TestCase):

    def setUp(self):
        self.work = fresh_workdir()
        self.db = str(self.work / "ledger.db")
        self.outdir = str(self.work / "out")

    def tearDown(self):
        cleanup(self.work)

    def test_required_no_solution_exits_2_and_keeps_ledger_empty(self):
        request = str(FIXTURES / "required_no_solution.json")
        code, out = run_cli(
            "publish", "--request", request,
            "--outdir", self.outdir, "--db", self.db,
        )
        self.assertEqual(code, 2)
        self.assertEqual(out["status"], "rejected")
        # 台账无发布、公开目录无发布文件
        store = Store(self.db, str(Path(self.outdir) / "public"))
        self.assertEqual(store.current_seq("dv-req"), 0)
        public_files = list((Path(self.outdir) / "public").glob("release_*.json"))
        self.assertEqual(public_files, [])
        # 私有拒绝原因已落盘（拒绝时台账为空，base_seq=0 进入文件名）
        private = read_json(
            Path(self.outdir) / "private" / "plan_required-blocked_base0.json"
        )
        self.assertEqual(private["status"], "rejected")
        self.assertTrue(private["reasons"])


class CrossTableCliTests(unittest.TestCase):
    """单表安全而两表合用泄露：第二张表发布内容必须为空候选。"""

    def setUp(self):
        self.work = fresh_workdir()
        self.db = str(self.work / "ledger.db")
        self.outdir = str(self.work / "out")

    def tearDown(self):
        cleanup(self.work)

    def test_second_table_publishes_nothing_after_first_table(self):
        code, pub_a = run_cli(
            "publish", "--request", str(FIXTURES / "cross_table_a.json"),
            "--outdir", self.outdir, "--db", self.db,
        )
        self.assertEqual(code, 0)
        self.assertTrue(pub_a["selected"])

        code, pub_b = run_cli(
            "publish", "--request", str(FIXTURES / "cross_table_b.json"),
            "--outdir", self.outdir, "--db", self.db,
        )
        self.assertEqual(code, 0)
        # 合用后 B 的两个候选都会钉死受保护原子，故发布条目为空
        self.assertEqual(pub_b["selected"], [])
        public_b = read_json(
            Path(self.outdir) / "public" / f"release_{pub_b['seq']:06d}.json"
        )
        self.assertEqual(public_b["items"], [])


class CrashTests(unittest.TestCase):

    def setUp(self):
        self.work = fresh_workdir()
        self.db = str(self.work / "ledger.db")
        self.outdir = str(self.work / "out")

    def tearDown(self):
        cleanup(self.work)

    def test_crash_before_commit_leaves_no_trace(self):
        request = str(FIXTURES / "crash_publish.json")
        code, out = run_cli(
            "publish", "--request", request,
            "--outdir", self.outdir, "--db", self.db,
            "--crash-before-commit", "crash-case",
        )
        self.assertNotEqual(code, 0)
        # 台账无版本、无公开文件
        store = Store(self.db, str(Path(self.outdir) / "public"))
        self.assertEqual(store.current_seq("dv-crash"), 0)
        public_files = list((Path(self.outdir) / "public").glob("release_*.json"))
        self.assertEqual(public_files, [])

        # 重新发布（不带崩溃钩子）应成功且序号从 1 开始
        code, out = run_cli(
            "publish", "--request", request,
            "--outdir", self.outdir, "--db", self.db,
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(out["status"], "published")
        self.assertEqual(out["seq"], 1)

    def test_reconcile_restores_public_file_after_commit(self):
        # 先正常发布，再删除公开文件模拟“提交后、落盘前”窗口的丢失
        request = str(FIXTURES / "crash_publish.json")
        code, out = run_cli(
            "publish", "--request", request,
            "--outdir", self.outdir, "--db", self.db,
        )
        self.assertEqual(code, 0)
        public_path = Path(self.outdir) / "public" / f"release_{out['seq']:06d}.json"
        public_path.unlink()
        store = Store(self.db, str(Path(self.outdir) / "public"))
        restored = store.reconcile_public_files()
        self.assertEqual(len(restored), 1)
        self.assertTrue(public_path.exists())


class ConcurrencyTests(unittest.TestCase):

    def setUp(self):
        self.work = fresh_workdir()
        self.db = str(self.work / "ledger.db")
        self.outdir = str(self.work / "out")

    def tearDown(self):
        cleanup(self.work)

    def test_concurrent_publish_serializes_and_never_corrupts(self):
        results: list[dict] = []
        errors: list[Exception] = []

        def worker(fixture: str):
            try:
                code, out = run_cli(
                    "publish", "--request", str(FIXTURES / fixture),
                    "--outdir", self.outdir, "--db", self.db,
                )
                results.append({"code": code, "out": out})
            except Exception as exc:  # pragma: no cover - 失败诊断用
                errors.append(exc)

        t1 = threading.Thread(target=worker, args=("concurrent_1.json",))
        t2 = threading.Thread(target=worker, args=("concurrent_2.json",))
        t1.start(); t2.start()
        t1.join(); t2.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertEqual(r["code"], 0, r)

        # 台账恰好两个版本，序号连续；每个版本都独立通过 verify
        store = Store(self.db, str(Path(self.outdir) / "public"))
        self.assertEqual(store.current_seq("dv-conc"), 2)
        public_files = sorted(
            (Path(self.outdir) / "public").glob("release_*.json")
        )
        self.assertEqual(len(public_files), 2)

        # 两个版本都独立通过 verify。调度顺序与重算次数不确定，因此按
        # request_id 收集其全部私有计划，挑选 base_seq+1 等于某公开序号、
        # 且计划选择与该公开文件条目一致的那份进行复核
        private_dir = Path(self.outdir) / "private"
        public_dir = Path(self.outdir) / "public"
        for request_id in ("concurrent-1", "concurrent-2"):
            candidates = list(private_dir.glob(f"plan_{request_id}_base*.json"))
            self.assertTrue(candidates, request_id)
            verified = False
            for private_path in candidates:
                plan_doc = read_json(private_path)
                seq = plan_doc["base_seq"] + 1
                public_path = public_dir / f"release_{seq:06d}.json"
                if not public_path.exists():
                    continue
                public_doc = read_json(public_path)
                if sorted(i["name"] for i in public_doc["items"]) != sorted(
                        plan_doc["selected"]):
                    continue
                code, out = run_cli(
                    "verify", "--plan", str(private_path),
                    "--public", str(public_path),
                )
                self.assertEqual(code, 0, out)
                verified = True
            self.assertTrue(verified, f"{request_id} 未找到可复核的计划/公开配对")

    def test_multiprocess_storm_all_versions_individually_verifiable(self):
        """4 个独立请求进程同时发布：BEGIN IMMEDIATE 串行化，最终每个版本
        都基于其提交时的真实台账规划，verify 必须全部通过。"""
        # 为每个进程准备不同 request_id 的请求副本，避免重放短路
        fixture_paths = []
        base = read_json(FIXTURES / "concurrent_1.json")
        for k in range(4):
            raw = dict(base)
            raw["request_id"] = f"storm-{k}"
            raw["candidates"] = [
                dict(c, name=f"{c['name']}-{k}") for c in base["candidates"]
            ]
            path = self.work / f"storm_{k}.json"
            write_json_atomic(str(path), raw)
            fixture_paths.append(path)

        procs = []
        for path in fixture_paths:
            bootstrap = (
                "import sys; "
                f"sys.path.insert(0, {str(ROOT)!r}); "
                "sys.stdout.reconfigure(encoding='utf-8'); "
                "from privacy_publisher.cli import main; sys.exit(main())"
            )
            procs.append(subprocess.Popen(
                [sys.executable, "-I", "-c", bootstrap,
                 "publish", "--request", str(path),
                 "--outdir", self.outdir, "--db", self.db],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ))
        outputs = []
        for proc in procs:
            stdout, stderr = proc.communicate()
            self.assertEqual(proc.returncode, 0, stderr.decode("utf-8", "replace"))
            outputs.append(json.loads(stdout.decode("utf-8")))

        seqs = sorted(o["seq"] for o in outputs)
        self.assertEqual(seqs, [1, 2, 3, 4])
        store = Store(self.db, str(Path(self.outdir) / "public"))
        self.assertEqual(store.current_seq("dv-conc"), 4)

        # 每个版本的私有计划 base_seq 必须落在已提交台账范围内（0..3），
        # 即规划依据的是拿锁时刻的真实状态而非无锁旧状态
        base_seqs = []
        for k in range(4):
            out_k = next(o for o in outputs if o["request_id"] == f"storm-{k}")
            plan_path = Path(out_k["private_plan"])
            doc = read_json(plan_path)
            base_seqs.append(doc["base_seq"])
            self.assertIn(doc["base_seq"], (0, 1, 2, 3))
            code, verify_out = run_cli(
                "verify", "--plan", str(plan_path),
                "--public", str(
                    Path(self.outdir) / "public"
                    / f"release_{out_k['seq']:06d}.json"
                ),
            )
            self.assertEqual(code, 0, verify_out)
        self.assertEqual(sorted(base_seqs), [0, 1, 2, 3])


class VerifyNegativeTests(unittest.TestCase):

    def setUp(self):
        self.work = fresh_workdir()
        self.db = str(self.work / "ledger.db")
        self.outdir = str(self.work / "out")

    def tearDown(self):
        cleanup(self.work)

    def test_verify_fails_when_public_file_leaks_private_field(self):
        request = str(FIXTURES / "crash_publish.json")
        code, out = run_cli(
            "publish", "--request", request,
            "--outdir", self.outdir, "--db", self.db,
        )
        self.assertEqual(code, 0)
        public_path = Path(self.outdir) / "public" / f"release_{out['seq']:06d}.json"
        doc = read_json(public_path)
        doc["witnesses"] = {"leaked": True}
        write_json_atomic(str(public_path), doc)

        private_path = Path(
            self.outdir) / "private" / "plan_crash-case_base0.json"
        code, verify_out = run_cli(
            "verify", "--plan", str(private_path), "--public", str(public_path),
        )
        self.assertEqual(code, 1)
        self.assertEqual(verify_out["verify"], "FAIL")
        self.assertTrue(any("私有字段" in e for e in verify_out["errors"]))


if __name__ == "__main__":
    unittest.main()
