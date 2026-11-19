"""输入校验与台账内部测试。"""

import unittest

from privacy_publisher.model import ValidationError, canonical_hash, parse_request
from privacy_publisher.store import ReplayError, StalePlanError, Store

from tests.paths import FIXTURES, cleanup, fresh_workdir


class ValidationTests(unittest.TestCase):

    def base_raw(self):
        return {
            "request_id": "r1",
            "data_version": "v1",
            "atom_values": {"a": 1, "b": 2},
            "candidates": [{"name": "AB", "atoms": ["a", "b"], "weight": 1}],
        }

    def test_valid_request_parses(self):
        req = parse_request(self.base_raw())
        self.assertEqual(req.atom_values, {"a": 1, "b": 2})
        self.assertFalse(req.public_total)

    def test_atom_count_and_value_caps(self):
        raw = self.base_raw()
        raw["atom_values"] = {f"x{i}": 20 for i in range(11)}
        with self.assertRaises(ValidationError):
            parse_request(raw)
        raw["atom_values"] = {"a": 21}
        with self.assertRaises(ValidationError):
            parse_request(raw)
        raw["atom_values"] = {"a": -1}
        with self.assertRaises(ValidationError):
            parse_request(raw)

    def test_structural_zero_must_be_real_zero(self):
        raw = self.base_raw()
        raw["structural_zeros"] = ["b"]  # b=2，不是零
        with self.assertRaises(ValidationError):
            parse_request(raw)
        raw["atom_values"] = {"a": 1, "b": 0}
        raw["structural_zeros"] = ["b"]
        req = parse_request(raw)
        self.assertIn("b", req.structural_zeros)

    def test_boolean_is_not_accepted_as_integer(self):
        raw = self.base_raw()
        raw["atom_values"] = {"a": True}
        with self.assertRaises(ValidationError):
            parse_request(raw)

    def test_duplicate_candidate_name_rejected(self):
        raw = self.base_raw()
        raw["candidates"] = [
            {"name": "X", "atoms": ["a"]},
            {"name": "X", "atoms": ["b"]},
        ]
        with self.assertRaises(ValidationError):
            parse_request(raw)

    def test_candidate_limit(self):
        raw = self.base_raw()
        raw["candidates"] = [
            {"name": f"C{i}", "atoms": ["a"]} for i in range(17)
        ]
        with self.assertRaises(ValidationError):
            parse_request(raw)

    def test_unknown_atom_reference_rejected(self):
        raw = self.base_raw()
        raw["candidates"] = [{"name": "X", "atoms": ["a", "z"]}]
        with self.assertRaises(ValidationError):
            parse_request(raw)

    def test_canonical_hash_stable_and_order_insensitive(self):
        payload1 = {"b": 1, "a": [1, 2, 3]}
        payload2 = {"a": [1, 2, 3], "b": 1}
        self.assertEqual(canonical_hash(payload1), canonical_hash(payload2))
        self.assertNotEqual(canonical_hash(payload1),
                            canonical_hash({"a": [1, 2, 4], "b": 1}))


class StoreTests(unittest.TestCase):

    def setUp(self):
        self.work = fresh_workdir()
        self.store = Store(
            str(self.work / "ledger.db"),
            str(self.work / "public"),
        )

    def tearDown(self):
        cleanup(self.work)

    def _payloads(self, request_id="r1", hash_="h1"):
        public = {"data_version": "v", "request_id": request_id,
                  "items": [{"name": "X", "value": 3}]}
        private = {"status": "accepted", "request_id": request_id,
                   "atom_order": ["a", "b"], "equations": []}
        return public, private

    def test_commit_advances_seq(self):
        public, private = self._payloads()
        receipt = self.store.commit_release("v", "r1", "h1", 0, public, private)
        self.assertEqual(receipt["seq"], 1)
        self.assertEqual(self.store.current_seq("v"), 1)

    def test_stale_plan_rejected_and_no_partial_write(self):
        public, private = self._payloads()
        self.store.commit_release("v", "r1", "h1", 0, public, private)
        with self.assertRaises(StalePlanError):
            self.store.commit_release(
                "v", "r2", "h2", planned_base_seq=0,
                public_payload=public, private_plan=private,
            )
        # 台账仍只有一个版本
        self.assertEqual(self.store.current_seq("v"), 1)

    def test_replay_same_hash_returns_same_receipt(self):
        public, private = self._payloads()
        first = self.store.commit_release("v", "r1", "h1", 0, public, private)
        # 重放发生在“已提交一个版本之后”，因此新计划的 base_seq 必须是 1；
        # commit_release 在锁内查到 receipts 命中后原样返回首个回执
        second = self.store.commit_release("v", "r1", "h1", 1, public, private)
        self.assertEqual(first["seq"], second["seq"])
        self.assertEqual(second["receipt"], "ok")
        # 台账版本没有前进
        self.assertEqual(self.store.current_seq("v"), first["seq"])

    def test_same_id_different_hash_is_error(self):
        public, private = self._payloads()
        self.store.commit_release("v", "r1", "h1", 0, public, private)
        with self.assertRaises(ReplayError):
            self.store.lookup_replay("r1", "different-hash")

    def test_prior_equations_round_trip(self):
        public, private = self._payloads()
        private["equations"] = [
            {"label": "candidate:X", "atoms": ["a", "b"], "rhs": 3},
        ]
        self.store.commit_release("v", "r1", "h1", 0, public, private)
        views = self.store.prior_equations("v")
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0].rhs, 3)
        self.assertEqual(views[0].atoms, ("a", "b"))


if __name__ == "__main__":
    unittest.main()
