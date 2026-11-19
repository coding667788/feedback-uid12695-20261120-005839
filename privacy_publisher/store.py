"""SQLite 发布台账：版本序号、精确重放、事务提交与并发/中断控制。

关键一致性设计：
- 每次成功发布在同一事务内插入 receipts/releases/ledger_state 三行，
  并把公开内容写入 release_blobs；文件在事务提交成功后再原子替换落盘，
  事务内挂接可选崩溃钩子用于中断测试。
- 全程 BEGIN IMMEDIATE 抢占写锁：两个并发请求不可能各自通过旧状态后
  分别提交；后到者在拿到锁时重新读取 base_seq，发现计划过时必须重算。
- 同 request_id + 同 request_hash 的请求视为精确重放，直接返回原回执。
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Callable

from .model import write_json_atomic

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_state (
    data_version TEXT PRIMARY KEY,
    base_seq     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS releases (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    data_version  TEXT NOT NULL,
    request_id    TEXT NOT NULL,
    request_hash  TEXT NOT NULL,
    public_json   TEXT NOT NULL,
    plan_json     TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS receipts (
    request_id    TEXT PRIMARY KEY,
    data_version  TEXT NOT NULL,
    request_hash  TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    receipt_json  TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class StalePlanError(Exception):
    """计划基于的 base_seq 与当前台账不一致，调用方必须重算。"""


class ReplayError(Exception):
    """同 request_id 但请求内容不同：不是精确重放，拒绝。"""


@dataclass
class StoredEquationView:
    seq: int
    label: str
    atoms: tuple[str, ...]
    rhs: int


class Store:
    def __init__(self, db_path: str, public_dir: str, crash_hook: Callable | None = None):
        self.db_path = db_path
        self.public_dir = public_dir
        # 崩溃钩子：事务内、提交前调用；抛出异常即模拟进程中断
        self.crash_hook = crash_hook
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
        os.makedirs(public_dir, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #

    def current_seq(self, data_version: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT base_seq FROM ledger_state WHERE data_version=?",
                (data_version,),
            ).fetchone()
            return int(row[0]) if row else 0

    def prior_equations(self, data_version: str) -> list[StoredEquationView]:
        """读取同数据版本此前已发布的全部准确值（公开等式来源）。

        公开文件按设计只含名称与值，不含原子构成；等式构成从各发布的
        私有计划（releases.plan_json，含 atom_order/equations）重建。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT seq, plan_json FROM releases WHERE data_version=? ORDER BY seq",
                (data_version,),
            ).fetchall()
        views: list[StoredEquationView] = []
        for seq, plan_json in rows:
            plan = json.loads(plan_json)
            for eq in plan["equations"]:
                views.append(StoredEquationView(
                    seq=seq, label=eq["label"],
                    atoms=tuple(eq["atoms"]), rhs=int(eq["rhs"]),
                ))
        return views

    def lookup_replay(self, request_id: str, request_hash: str):
        """精确重放查询；同 id 不同内容抛 ReplayError。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT request_hash, receipt_json FROM receipts WHERE request_id=?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        if row[0] != request_hash:
            raise ReplayError(
                f"request_id={request_id} 已用于不同内容的请求，拒绝覆盖（非精确重放）"
            )
        return json.loads(row[1])

    # ------------------------------------------------------------------ #
    # 写入（单事务）
    # ------------------------------------------------------------------ #

    def commit_release(self, data_version: str, request_id: str,
                       request_hash: str, planned_base_seq: int,
                       public_payload: dict, private_plan: dict) -> dict:
        """在一个事务内提交计划回执与完整发布内容。

        拿锁后重新核对 base_seq：若与计划时不一致则抛 StalePlanError，
        由 CLI 层重算后重试。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT base_seq FROM ledger_state WHERE data_version=?",
                (data_version,),
            ).fetchone()
            actual_base = int(row[0]) if row else 0
            if actual_base != planned_base_seq:
                conn.execute("ROLLBACK")
                raise StalePlanError(
                    f"计划基于版本 {planned_base_seq}，台账当前为 {actual_base}，必须重算"
                )

            # 重放在锁内复查，防止并发下两个相同请求各发一次
            replay = conn.execute(
                "SELECT receipt_json FROM receipts WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if replay is not None:
                stored_hash = conn.execute(
                    "SELECT request_hash FROM receipts WHERE request_id=?",
                    (request_id,),
                ).fetchone()[0]
                if stored_hash != request_hash:
                    conn.execute("ROLLBACK")
                    raise ReplayError(f"request_id={request_id} 内容不一致")
                conn.execute("COMMIT")
                return json.loads(replay[0])

            public_blob = json.dumps(public_payload, ensure_ascii=False,
                                     sort_keys=True, separators=(",", ":"))
            plan_blob = json.dumps(private_plan, ensure_ascii=False,
                                   sort_keys=True, separators=(",", ":"))
            cur = conn.execute(
                "INSERT INTO releases (data_version, request_id, request_hash,"
                " public_json, plan_json) VALUES (?, ?, ?, ?, ?)",
                (data_version, request_id, request_hash, public_blob, plan_blob),
            )
            seq = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO ledger_state (data_version, base_seq) VALUES (?, ?) "
                "ON CONFLICT(data_version) DO UPDATE SET base_seq=excluded.base_seq",
                (data_version, seq),
            )
            receipt = {
                "receipt": "ok",
                "request_id": request_id,
                "data_version": data_version,
                "request_hash": request_hash,
                "seq": seq,
                "base_seq": actual_base,
                "public_file": self._public_file_name(seq),
                "selected": public_payload.get("items", []),
            }
            conn.execute(
                "INSERT INTO receipts (request_id, data_version, request_hash,"
                " seq, receipt_json) VALUES (?, ?, ?, ?, ?)",
                (request_id, data_version, request_hash, seq,
                 json.dumps(receipt, ensure_ascii=False, sort_keys=True)),
            )

            if self.crash_hook is not None:
                # 钩子抛异常 => 事务未提交，台账与公开文件都不得留下痕迹
                self.crash_hook(seq=seq, request_id=request_id)

            conn.execute("COMMIT")
        except Exception:
            # 任何异常路径都确保事务回滚、连接关闭
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            conn.close()
            raise
        else:
            conn.close()

        # 提交成功后才写公开文件（原子替换）；失败可由 reconcile 从台账恢复
        path = os.path.join(self.public_dir, self._public_file_name(seq))
        write_json_atomic(path, public_payload)
        return receipt

    def reconcile_public_files(self) -> list[str]:
        """根据台账重建缺失的公开文件（崩溃发生在提交后、写文件前的场景）。"""
        restored: list[str] = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT seq, public_json FROM releases ORDER BY seq"
            ).fetchall()
        for seq, public_json in rows:
            path = os.path.join(self.public_dir, self._public_file_name(seq))
            if not os.path.exists(path):
                write_json_atomic(path, json.loads(public_json))
                restored.append(path)
        return restored

    @staticmethod
    def _public_file_name(seq: int) -> str:
        return f"release_{seq:06d}.json"
