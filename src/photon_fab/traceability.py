"""供应商、物料批次、来料检验与替代料审批的追溯领域逻辑。

规则要点：
- 已关联到芯片批次（即已用于生产）的物料批次禁止删除或改写；
- 替代料必须先由质量角色审批通过，才能作为用料关联到批次；
- 所有关键写操作进入 trace_events，追溯查询返回成品到供应商的多级链路。
"""

from __future__ import annotations

import uuid

from .storage import event, trace_event, transaction, utcnow

INSPECTION_RESULTS = {"pass", "conditional", "fail"}
_BATCH_STATUS_BY_RESULT = {"pass": "accepted", "conditional": "quarantined", "fail": "rejected"}


class TraceabilityMixin:
    # ----- 供应商 -----

    def create_supplier(self, token: str, supplier_id: str, name: str, material_types: list[str] | None = None, contact: str = "") -> dict:
        actor = self.auth.require(token, "source")
        if not supplier_id.strip() or not name.strip():
            raise ValueError("supplier_id and name are required")
        types = ",".join(sorted({t.strip() for t in (material_types or []) if t.strip()}))
        now = utcnow()
        with transaction(self.db):
            try:
                self.db.execute(
                    "INSERT INTO suppliers VALUES(?,?,?,?,1,?)",
                    (supplier_id, name, types, contact, now),
                )
            except Exception as exc:
                raise ValueError(f"supplier {supplier_id} already exists") from exc
            trace_event(self.db, "supplier", supplier_id, "supplier.created", actor.user_id, {"name": name})
        return self.get_supplier(token, supplier_id)

    def get_supplier(self, token: str, supplier_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM suppliers WHERE supplier_id=?", (supplier_id,)).fetchone()
        if not row:
            raise KeyError(supplier_id)
        return _supplier_dict(row)

    def list_suppliers(self, token: str) -> list[dict]:
        self.auth.require(token, "read")
        rows = self.db.execute("SELECT * FROM suppliers ORDER BY supplier_id").fetchall()
        return [_supplier_dict(r) for r in rows]

    def deactivate_supplier(self, token: str, supplier_id: str) -> dict:
        actor = self.auth.require(token, "source")
        with transaction(self.db):
            cur = self.db.execute("UPDATE suppliers SET active=0 WHERE supplier_id=?", (supplier_id,))
            if cur.rowcount == 0:
                raise KeyError(supplier_id)
            trace_event(self.db, "supplier", supplier_id, "supplier.deactivated", actor.user_id, {})
        return self.get_supplier(token, supplier_id)

    # ----- 物料批次 -----

    def register_material_batch(
        self,
        token: str,
        batch_id: str,
        supplier_id: str,
        material_type: str,
        spec: str,
        quantity: float,
        received_at: str | None = None,
    ) -> dict:
        actor = self.auth.require(token, "source")
        if not batch_id.strip() or not material_type.strip() or not spec.strip() or float(quantity) <= 0:
            raise ValueError("batch fields are invalid")
        now = utcnow()
        with transaction(self.db):
            supplier = self.db.execute("SELECT 1 FROM suppliers WHERE supplier_id=? AND active=1", (supplier_id,)).fetchone()
            if not supplier:
                raise KeyError(f"active supplier {supplier_id}")
            try:
                self.db.execute(
                    "INSERT INTO material_batches VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (batch_id, supplier_id, material_type, spec, float(quantity), received_at or now, "registered", actor.user_id, now, now),
                )
            except Exception as exc:
                raise ValueError(f"material batch {batch_id} already exists") from exc
            trace_event(self.db, "material_batch", batch_id, "material_batch.registered", actor.user_id, {"supplier_id": supplier_id, "material_type": material_type})
        return self.get_material_batch(token, batch_id)

    def get_material_batch(self, token: str, batch_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM material_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not row:
            raise KeyError(batch_id)
        return _batch_dict(row)

    def update_material_batch(self, token: str, batch_id: str, **fields) -> dict:
        """仅允许改写尚未投入生产的批次；一旦被批次用料引用即永久锁定。"""
        actor = self.auth.require(token, "source")
        allowed = {"spec", "quantity", "received_at"}
        fields = {k: v for k, v in fields.items() if v is not None and k in allowed}
        if not fields:
            raise ValueError("no updatable fields")
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM material_batches WHERE batch_id=?", (batch_id,)).fetchone():
                raise KeyError(batch_id)
            if self._batch_in_use(batch_id):
                raise PermissionError(f"material batch {batch_id} has been used in production and is locked")
            assignments = ", ".join(f"{k}=?" for k in fields)
            params = [*fields.values(), utcnow(), batch_id]
            self.db.execute(f"UPDATE material_batches SET {assignments}, updated_at=? WHERE batch_id=?", params)
            trace_event(self.db, "material_batch", batch_id, "material_batch.updated", actor.user_id, {"fields": sorted(fields)})
        return self.get_material_batch(token, batch_id)

    def delete_material_batch(self, token: str, batch_id: str) -> dict:
        """仅允许删除从未投产、且无用料关联的来料批次；一经使用即永久保留。"""
        actor = self.auth.require(token, "source")
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM material_batches WHERE batch_id=?", (batch_id,)).fetchone():
                raise KeyError(batch_id)
            if self._batch_in_use(batch_id):
                raise PermissionError(f"material batch {batch_id} has been used in production and cannot be deleted")
            self.db.execute("DELETE FROM material_inspections WHERE batch_id=?", (batch_id,))
            pending = self.db.execute(
                "SELECT substitution_id FROM substitution_approvals WHERE substitute_batch_id=? AND status='pending'",
                (batch_id,),
            ).fetchall()
            for row in pending:
                self.db.execute("DELETE FROM substitution_approvals WHERE substitution_id=?", (row["substitution_id"],))
            self.db.execute("DELETE FROM material_batches WHERE batch_id=?", (batch_id,))
            trace_event(self.db, "material_batch", batch_id, "material_batch.deleted", actor.user_id, {})
        return {"batch_id": batch_id, "deleted": True}

    def _batch_in_use(self, batch_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM lot_materials WHERE batch_id=? LIMIT 1", (batch_id,)).fetchone() is not None

    # ----- 来料检验（仅质量角色） -----

    def record_inspection(self, token: str, batch_id: str, result: str, findings: str = "", metrics: dict | None = None) -> dict:
        actor = self.auth.require(token, "inspect")
        if result not in INSPECTION_RESULTS:
            raise ValueError(f"result must be one of {sorted(INSPECTION_RESULTS)}")
        import json as _json

        inspection_id = uuid.uuid4().hex
        now = utcnow()
        with transaction(self.db):
            batch = self.db.execute("SELECT status FROM material_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if not batch:
                raise KeyError(batch_id)
            if batch["status"] in {"rejected", "in_use"}:
                raise PermissionError(f"material batch {batch_id} is {batch['status']} and cannot be re-inspected")
            self.db.execute(
                "INSERT INTO material_inspections VALUES(?,?,?,?,?,?,?)",
                (inspection_id, batch_id, result, findings, _json.dumps(metrics or {}, sort_keys=True), actor.user_id, now),
            )
            self.db.execute(
                "UPDATE material_batches SET status=?, updated_at=? WHERE batch_id=?",
                (_BATCH_STATUS_BY_RESULT[result], now, batch_id),
            )
            trace_event(self.db, "material_batch", batch_id, "inspection.recorded", actor.user_id, {"inspection_id": inspection_id, "result": result})
        return self.get_inspection(token, inspection_id)

    def get_inspection(self, token: str, inspection_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM material_inspections WHERE inspection_id=?", (inspection_id,)).fetchone()
        if not row:
            raise KeyError(inspection_id)
        return _inspection_dict(row)

    def list_inspections(self, token: str, batch_id: str) -> list[dict]:
        self.auth.require(token, "read")
        rows = self.db.execute(
            "SELECT * FROM material_inspections WHERE batch_id=? ORDER BY inspected_at, inspection_id",
            (batch_id,),
        ).fetchall()
        return [_inspection_dict(r) for r in rows]

    def _latest_inspection(self, batch_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM material_inspections WHERE batch_id=? ORDER BY inspected_at DESC, inspection_id DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
        return _inspection_dict(row) if row else None

    # ----- 替代料审批 -----

    def request_substitution(
        self,
        token: str,
        lot_id: str,
        material_type: str,
        intended_spec: str,
        substitute_batch_id: str,
        reason: str,
    ) -> dict:
        actor = self.auth.require(token, "source")
        if not material_type.strip() or not intended_spec.strip() or not reason.strip():
            raise ValueError("material_type, intended_spec and reason are required")
        substitution_id = uuid.uuid4().hex
        now = utcnow()
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                raise KeyError(lot_id)
            sub_batch = self.db.execute("SELECT material_type FROM material_batches WHERE batch_id=?", (substitute_batch_id,)).fetchone()
            if not sub_batch:
                raise KeyError(f"material batch {substitute_batch_id}")
            if sub_batch["material_type"] != material_type:
                raise ValueError("substitute batch material_type does not match request")
            self.db.execute(
                "INSERT INTO substitution_approvals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (substitution_id, lot_id, material_type, intended_spec, substitute_batch_id, reason, actor.user_id, now, "pending", None, None, None),
            )
            trace_event(self.db, "substitution", substitution_id, "substitution.requested", actor.user_id, {"lot_id": lot_id, "substitute_batch_id": substitute_batch_id})
            event(self.db, lot_id, "substitution_requested", actor.user_id, {"substitution_id": substitution_id, "substitute_batch_id": substitute_batch_id})
        return self.get_substitution(token, substitution_id)

    def decide_substitution(self, token: str, substitution_id: str, decision: str, reason: str) -> dict:
        actor = self.auth.require(token, "approve")
        if decision not in {"approved", "rejected"} or not reason.strip():
            raise ValueError("decision (approved/rejected) and reason are required")
        with transaction(self.db):
            row = self.db.execute("SELECT * FROM substitution_approvals WHERE substitution_id=?", (substitution_id,)).fetchone()
            if not row:
                raise KeyError(substitution_id)
            if row["status"] != "pending":
                raise PermissionError(f"substitution {substitution_id} already decided: {row['status']}")
            self.db.execute(
                "UPDATE substitution_approvals SET status=?,reviewer=?,decision_reason=?,decided_at=? WHERE substitution_id=?",
                (decision, actor.user_id, reason, utcnow(), substitution_id),
            )
            trace_event(self.db, "substitution", substitution_id, "substitution.decided", actor.user_id, {"decision": decision})
            event(self.db, row["lot_id"], "substitution_decided", actor.user_id, {"substitution_id": substitution_id, "decision": decision})
        return self.get_substitution(token, substitution_id)

    def get_substitution(self, token: str, substitution_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM substitution_approvals WHERE substitution_id=?", (substitution_id,)).fetchone()
        if not row:
            raise KeyError(substitution_id)
        return _substitution_dict(row)

    # ----- 批次用料关联 -----

    def link_lot_material(self, token: str, lot_id: str, batch_id: str, role: str, quantity: float, substitution_id: str | None = None) -> dict:
        actor = self.auth.require(token, "source")
        if not role.strip() or float(quantity) <= 0:
            raise ValueError("role and positive quantity are required")
        link_id = uuid.uuid4().hex
        now = utcnow()
        with transaction(self.db):
            lot = self.db.execute("SELECT status FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
            if not lot:
                raise KeyError(lot_id)
            batch = self.db.execute("SELECT * FROM material_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if not batch:
                raise KeyError(batch_id)
            approved_substitution = False
            if substitution_id is not None:
                sub = self.db.execute("SELECT * FROM substitution_approvals WHERE substitution_id=?", (substitution_id,)).fetchone()
                if not sub:
                    raise KeyError(f"substitution {substitution_id}")
                if sub["lot_id"] != lot_id or sub["substitute_batch_id"] != batch_id:
                    raise ValueError("substitution does not match lot or batch")
                if sub["status"] != "approved":
                    raise PermissionError("substitution has not been approved by quality")
                approved_substitution = True
            latest = self._latest_inspection(batch_id)
            if latest is None:
                raise PermissionError(f"material batch {batch_id} has no incoming inspection")
            if latest["result"] != "pass":
                if not (approved_substitution and latest["result"] == "conditional"):
                    raise PermissionError(f"material batch {batch_id} latest inspection is {latest['result']}")
            try:
                self.db.execute(
                    "INSERT INTO lot_materials VALUES(?,?,?,?,?,?,?,?)",
                    (link_id, lot_id, batch_id, role, float(quantity), substitution_id, actor.user_id, now),
                )
            except Exception as exc:
                raise ValueError("material already linked to this lot") from exc
            self.db.execute("UPDATE material_batches SET status='in_use', updated_at=? WHERE batch_id=?", (now, batch_id))
            trace_event(self.db, "material_batch", batch_id, "material.linked", actor.user_id, {"lot_id": lot_id, "role": role})
            event(self.db, lot_id, "material_linked", actor.user_id, {"batch_id": batch_id, "role": role, "substitution_id": substitution_id})
        return self.get_lot_material(token, link_id)

    def get_lot_material(self, token: str, link_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM lot_materials WHERE link_id=?", (link_id,)).fetchone()
        if not row:
            raise KeyError(link_id)
        return _link_dict(row)

    def list_lot_materials(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        rows = self.db.execute("SELECT * FROM lot_materials WHERE lot_id=? ORDER BY linked_at, link_id", (lot_id,)).fetchall()
        return [_link_dict(r) for r in rows]

    # ----- 多级追溯链 -----

    def trace_lot(self, token: str, lot_id: str) -> dict:
        """从成品芯片批次反查：用料 → 物料批次/来料检验 → 供应商，及替代料审批。"""
        self.auth.require(token, "read")
        lot = self.get_lot(token, lot_id)
        materials = []
        for link_row in self.db.execute("SELECT * FROM lot_materials WHERE lot_id=? ORDER BY linked_at, link_id", (lot_id,)).fetchall():
            batch_row = self.db.execute("SELECT * FROM material_batches WHERE batch_id=?", (link_row["batch_id"],)).fetchone()
            supplier_row = self.db.execute("SELECT * FROM suppliers WHERE supplier_id=?", (batch_row["supplier_id"],)).fetchone()
            inspection_rows = self.db.execute(
                "SELECT * FROM material_inspections WHERE batch_id=? ORDER BY inspected_at, inspection_id",
                (batch_row["batch_id"],),
            ).fetchall()
            substitution = None
            if link_row["substitution_id"]:
                sub_row = self.db.execute(
                    "SELECT * FROM substitution_approvals WHERE substitution_id=?",
                    (link_row["substitution_id"],),
                ).fetchone()
                substitution = _substitution_dict(sub_row)
            materials.append({
                "link": _link_dict(link_row),
                "material_batch": _batch_dict(batch_row),
                "supplier": _supplier_dict(supplier_row),
                "inspections": [_inspection_dict(r) for r in inspection_rows],
                "substitution": substitution,
            })
        measurements = [
            dict(r)
            for r in self.db.execute("SELECT * FROM measurements WHERE lot_id=? ORDER BY measured_at, measurement_id", (lot_id,)).fetchall()
        ]
        approvals = [dict(r) for r in self.db.execute("SELECT * FROM approvals WHERE lot_id=? ORDER BY created_at", (lot_id,)).fetchall()]
        events = [dict(r) for r in self.db.execute("SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)).fetchall()]
        return {
            "lot": lot,
            "materials": materials,
            "measurements": measurements,
            "approvals": approvals,
            "events": events,
        }

    def trace_material_batch(self, token: str, batch_id: str) -> dict:
        """从来料批次正向查询：供应商、检验记录、替代料审批与使用它的所有芯片批次。"""
        self.auth.require(token, "read")
        batch_row = self.db.execute("SELECT * FROM material_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not batch_row:
            raise KeyError(batch_id)
        supplier_row = self.db.execute("SELECT * FROM suppliers WHERE supplier_id=?", (batch_row["supplier_id"],)).fetchone()
        inspection_rows = self.db.execute(
            "SELECT * FROM material_inspections WHERE batch_id=? ORDER BY inspected_at, inspection_id",
            (batch_id,),
        ).fetchall()
        usages = []
        for link_row in self.db.execute("SELECT * FROM lot_materials WHERE batch_id=? ORDER BY linked_at, link_id", (batch_id,)).fetchall():
            lot_row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (link_row["lot_id"],)).fetchone()
            substitution = None
            if link_row["substitution_id"]:
                sub_row = self.db.execute(
                    "SELECT * FROM substitution_approvals WHERE substitution_id=?",
                    (link_row["substitution_id"],),
                ).fetchone()
                substitution = _substitution_dict(sub_row)
            usages.append({"link": _link_dict(link_row), "lot": dict(lot_row), "substitution": substitution})
        substitution_requests = [
            _substitution_dict(r)
            for r in self.db.execute(
                "SELECT * FROM substitution_approvals WHERE substitute_batch_id=? ORDER BY requested_at",
                (batch_id,),
            ).fetchall()
        ]
        return {
            "material_batch": _batch_dict(batch_row),
            "supplier": _supplier_dict(supplier_row),
            "inspections": [_inspection_dict(r) for r in inspection_rows],
            "usages": usages,
            "substitution_requests": substitution_requests,
            "locked": self._batch_in_use(batch_id),
        }


def _supplier_dict(row) -> dict:
    data = dict(row)
    data["active"] = bool(data["active"])
    data["material_types"] = [t for t in data["material_types"].split(",") if t]
    return data


def _batch_dict(row) -> dict:
    return dict(row)


def _inspection_dict(row) -> dict:
    import json as _json

    data = dict(row)
    data["metrics"] = _json.loads(data["metrics"] or "{}")
    return data


def _substitution_dict(row) -> dict:
    return dict(row)


def _link_dict(row) -> dict:
    return dict(row)
