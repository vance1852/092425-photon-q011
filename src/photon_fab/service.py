"""协调认证、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import uuid
from typing import Sequence

from .analytics import confidence_interval, summarize_spectrum, yield_rate
from .auth import Auth
from .storage import connect, event, transaction, utcnow

MATERIAL_TYPES = {"epi_wafer", "packaging"}
INSPECTION_RESULTS = {"pass", "fail", "conditional"}
SUBSTITUTION_DECISIONS = {"approved", "rejected"}


class PhotonService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database)
        self.auth = Auth(self.db)

    def bootstrap_admin(self, user_id: str = "admin", password: str = "photon-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except Exception:
            pass

    def create_lot(self, token: str, lot_id: str, product: str, process_rev: str, wafer_count: int) -> dict:
        actor = self.auth.require(token, "submit")
        if wafer_count <= 0 or not lot_id.strip() or not process_rev.strip():
            raise ValueError("lot fields are invalid")
        now = utcnow()
        with transaction(self.db):
            self.db.execute("INSERT INTO chip_lots VALUES(?,?,?,?,?,?,?,?)", (lot_id, product, process_rev, wafer_count, "engineering", actor.user_id, now, now))
            event(self.db, lot_id, "created", actor.user_id, {"product": product, "process_rev": process_rev})
        return self.get_lot(token, lot_id)

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise KeyError(lot_id)
        return dict(row)

    def add_measurement(self, token: str, lot_id: str, wavelength_nm: float, response: float, noise: float, instrument: str) -> dict:
        actor = self.auth.require(token, "measure")
        measurement_id = uuid.uuid4().hex
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                raise KeyError(lot_id)
            self.db.execute("INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?)", (measurement_id, lot_id, float(wavelength_nm), float(response), float(noise), instrument, actor.user_id, utcnow()))
            event(self.db, lot_id, "measurement", actor.user_id, {"measurement_id": measurement_id, "wavelength_nm": wavelength_nm})
        return {"measurement_id": measurement_id, "lot_id": lot_id}

    def analyze(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "analyze")
        rows = self.db.execute("SELECT wavelength_nm,response FROM measurements WHERE lot_id=? ORDER BY wavelength_nm", (lot_id,)).fetchall()
        if len(rows) < 3:
            raise ValueError("three measurements are required")
        summary = summarize_spectrum([r[0] for r in rows], [r[1] for r in rows])
        rates = yield_rate(self.get_lot(token, lot_id)["wafer_count"], sum(1 for r in rows if r[1] >= 0.8), 0)
        ci = confidence_interval([r[1] for r in rows])
        return {"lot_id": lot_id, "spectrum": summary.__dict__, "yield": rates, "response_ci": ci}

    def approve(self, token: str, lot_id: str, decision: str, reason: str) -> dict:
        actor = self.auth.require(token, "approve")
        if decision not in {"release", "hold", "reject"} or not reason.strip():
            raise ValueError("decision and reason are required")
        with transaction(self.db):
            self.db.execute("INSERT OR REPLACE INTO approvals VALUES(?,?,?,?,?)", (lot_id, actor.user_id, decision, reason, utcnow()))
            status = {"release": "released", "hold": "hold", "reject": "rejected"}[decision]
            self.db.execute("UPDATE chip_lots SET status=?,updated_at=? WHERE lot_id=?", (status, utcnow(), lot_id))
            event(self.db, lot_id, "approval", actor.user_id, {"decision": decision, "reason": reason})
        return self.get_lot(token, lot_id)

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute("SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)).fetchall()]

    def create_supplier(self, token: str, supplier_id: str, name: str, material_scope: str) -> dict:
        actor = self.auth.require(token, "submit")
        if not supplier_id.strip() or not name.strip() or not material_scope.strip():
            raise ValueError("supplier fields are invalid")
        with transaction(self.db):
            self.db.execute("INSERT INTO suppliers VALUES(?,?,?,?,?)", (supplier_id, name, material_scope, actor.user_id, utcnow()))
        return self.get_supplier(token, supplier_id)

    def get_supplier(self, token: str, supplier_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM suppliers WHERE supplier_id=?", (supplier_id,)).fetchone()
        if not row:
            raise KeyError(supplier_id)
        return dict(row)

    def register_material_batch(self, token: str, batch_id: str, supplier_id: str, supplier_lot: str, material_type: str, quantity: float, unit: str) -> dict:
        actor = self.auth.require(token, "submit")
        quantity = float(quantity)
        if not batch_id.strip() or not supplier_lot.strip() or not unit.strip() or quantity <= 0:
            raise ValueError("material batch fields are invalid")
        if material_type not in MATERIAL_TYPES:
            raise ValueError("unknown material type")
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM suppliers WHERE supplier_id=?", (supplier_id,)).fetchone():
                raise KeyError(supplier_id)
            self.db.execute("INSERT INTO material_batches VALUES(?,?,?,?,?,?,?,?,?)", (batch_id, supplier_id, supplier_lot, material_type, quantity, unit, utcnow(), actor.user_id, utcnow()))
        return self.get_material_batch(token, batch_id)

    def get_material_batch(self, token: str, batch_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM material_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not row:
            raise KeyError(batch_id)
        batch = dict(row)
        batch["supplier"] = dict(self.db.execute("SELECT * FROM suppliers WHERE supplier_id=?", (batch["supplier_id"],)).fetchone())
        batch["inspections"] = [dict(r) for r in self.db.execute("SELECT * FROM inspections WHERE batch_id=? ORDER BY inspected_at", (batch_id,)).fetchall()]
        batch["used_by_lots"] = [r[0] for r in self.db.execute("SELECT DISTINCT lot_id FROM lot_materials WHERE batch_id=? ORDER BY lot_id", (batch_id,)).fetchall()]
        return batch

    def _batch_in_use(self, batch_id: str) -> bool:
        return bool(self.db.execute("SELECT 1 FROM lot_materials WHERE batch_id=? LIMIT 1", (batch_id,)).fetchone())

    def update_material_batch(self, token: str, batch_id: str, supplier_lot: str | None = None, material_type: str | None = None, quantity: float | None = None, unit: str | None = None) -> dict:
        self.auth.require(token, "submit")
        updates: dict[str, object] = {}
        if supplier_lot is not None:
            if not supplier_lot.strip():
                raise ValueError("supplier lot is invalid")
            updates["supplier_lot"] = supplier_lot
        if material_type is not None:
            if material_type not in MATERIAL_TYPES:
                raise ValueError("unknown material type")
            updates["material_type"] = material_type
        if quantity is not None:
            quantity = float(quantity)
            if quantity <= 0:
                raise ValueError("quantity must be positive")
            updates["quantity"] = quantity
        if unit is not None:
            if not unit.strip():
                raise ValueError("unit is invalid")
            updates["unit"] = unit
        if not updates:
            raise ValueError("no fields to update")
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM material_batches WHERE batch_id=?", (batch_id,)).fetchone():
                raise KeyError(batch_id)
            if self._batch_in_use(batch_id):
                raise ValueError("material batch already used in production")
            clause = ",".join(f"{field}=?" for field in updates)
            self.db.execute(f"UPDATE material_batches SET {clause} WHERE batch_id=?", (*updates.values(), batch_id))
        return self.get_material_batch(token, batch_id)

    def delete_material_batch(self, token: str, batch_id: str) -> dict:
        self.auth.require(token, "submit")
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM material_batches WHERE batch_id=?", (batch_id,)).fetchone():
                raise KeyError(batch_id)
            if self._batch_in_use(batch_id):
                raise ValueError("material batch already used in production")
            self.db.execute("DELETE FROM inspections WHERE batch_id=?", (batch_id,))
            self.db.execute("DELETE FROM material_batches WHERE batch_id=?", (batch_id,))
        return {"deleted": batch_id}

    def record_inspection(self, token: str, batch_id: str, result: str, notes: str = "") -> dict:
        actor = self.auth.require(token, "inspect")
        if result not in INSPECTION_RESULTS:
            raise ValueError("unknown inspection result")
        inspection_id = uuid.uuid4().hex
        now = utcnow()
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM material_batches WHERE batch_id=?", (batch_id,)).fetchone():
                raise KeyError(batch_id)
            self.db.execute("INSERT INTO inspections VALUES(?,?,?,?,?,?)", (inspection_id, batch_id, result, notes, actor.user_id, now))
        return {"inspection_id": inspection_id, "batch_id": batch_id, "result": result, "notes": notes, "inspector": actor.user_id, "inspected_at": now}

    def request_substitution(self, token: str, lot_id: str, batch_id: str, substitute_for: str, reason: str) -> dict:
        actor = self.auth.require(token, "submit")
        if not reason.strip() or batch_id == substitute_for:
            raise ValueError("substitution fields are invalid")
        approval_id = uuid.uuid4().hex
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                raise KeyError(lot_id)
            for ref in (batch_id, substitute_for):
                if not self.db.execute("SELECT 1 FROM material_batches WHERE batch_id=?", (ref,)).fetchone():
                    raise KeyError(ref)
            if self.db.execute("SELECT 1 FROM substitution_approvals WHERE lot_id=? AND batch_id=? AND substitute_for=? AND decision IN ('pending','approved')", (lot_id, batch_id, substitute_for)).fetchone():
                raise ValueError("substitution request already exists")
            self.db.execute("INSERT INTO substitution_approvals VALUES(?,?,?,?,?,?,?,?,?,?,?)", (approval_id, lot_id, batch_id, substitute_for, reason, actor.user_id, utcnow(), None, "pending", None, None))
            event(self.db, lot_id, "substitution.requested", actor.user_id, {"approval_id": approval_id, "batch_id": batch_id, "substitute_for": substitute_for})
        return self.get_substitution(token, approval_id)

    def get_substitution(self, token: str, approval_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM substitution_approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if not row:
            raise KeyError(approval_id)
        return dict(row)

    def review_substitution(self, token: str, approval_id: str, decision: str, reason: str) -> dict:
        actor = self.auth.require(token, "approve")
        if decision not in SUBSTITUTION_DECISIONS or not reason.strip():
            raise ValueError("decision and reason are required")
        with transaction(self.db):
            row = self.db.execute("SELECT * FROM substitution_approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if not row:
                raise KeyError(approval_id)
            if row["decision"] != "pending":
                raise ValueError("substitution already decided")
            self.db.execute("UPDATE substitution_approvals SET reviewer=?,decision=?,review_reason=?,decided_at=? WHERE approval_id=?", (actor.user_id, decision, reason, utcnow(), approval_id))
            event(self.db, row["lot_id"], "substitution.reviewed", actor.user_id, {"approval_id": approval_id, "decision": decision})
        return self.get_substitution(token, approval_id)

    def use_material(self, token: str, lot_id: str, batch_id: str, quantity: float, purpose: str, substitute_for: str | None = None) -> dict:
        actor = self.auth.require(token, "submit")
        quantity = float(quantity)
        if quantity <= 0 or not purpose.strip():
            raise ValueError("usage fields are invalid")
        usage_id = uuid.uuid4().hex
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                raise KeyError(lot_id)
            if not self.db.execute("SELECT 1 FROM material_batches WHERE batch_id=?", (batch_id,)).fetchone():
                raise KeyError(batch_id)
            approval_id = None
            if substitute_for is not None:
                approval = self.db.execute("SELECT approval_id FROM substitution_approvals WHERE lot_id=? AND batch_id=? AND substitute_for=? AND decision='approved'", (lot_id, batch_id, substitute_for)).fetchone()
                if not approval:
                    raise PermissionError("substitute material requires quality approval")
                approval_id = approval["approval_id"]
            self.db.execute("INSERT INTO lot_materials VALUES(?,?,?,?,?,?,?,?,?)", (usage_id, lot_id, batch_id, quantity, purpose, substitute_for, approval_id, actor.user_id, utcnow()))
            event(self.db, lot_id, "material.used", actor.user_id, {"usage_id": usage_id, "batch_id": batch_id, "quantity": quantity, "substitute_for": substitute_for})
        return {"usage_id": usage_id, "lot_id": lot_id, "batch_id": batch_id}

    def trace_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        lot = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not lot:
            raise KeyError(lot_id)
        materials = []
        for usage in self.db.execute("SELECT * FROM lot_materials WHERE lot_id=? ORDER BY recorded_at,usage_id", (lot_id,)).fetchall():
            batch = dict(self.db.execute("SELECT * FROM material_batches WHERE batch_id=?", (usage["batch_id"],)).fetchone())
            supplier = dict(self.db.execute("SELECT * FROM suppliers WHERE supplier_id=?", (batch["supplier_id"],)).fetchone())
            inspections = [dict(r) for r in self.db.execute("SELECT * FROM inspections WHERE batch_id=? ORDER BY inspected_at", (usage["batch_id"],)).fetchall()]
            substitution = None
            if usage["approval_id"]:
                substitution = dict(self.db.execute("SELECT * FROM substitution_approvals WHERE approval_id=?", (usage["approval_id"],)).fetchone())
            substituted_batch = None
            if usage["substitute_for"]:
                substituted_batch = dict(self.db.execute("SELECT * FROM material_batches WHERE batch_id=?", (usage["substitute_for"],)).fetchone())
                substituted_batch["supplier"] = dict(self.db.execute("SELECT * FROM suppliers WHERE supplier_id=?", (substituted_batch["supplier_id"],)).fetchone())
            materials.append({
                "usage_id": usage["usage_id"], "quantity": usage["quantity"], "purpose": usage["purpose"],
                "is_substitute": usage["substitute_for"] is not None,
                "recorded_by": usage["recorded_by"], "recorded_at": usage["recorded_at"],
                "batch": batch, "supplier": supplier, "inspections": inspections,
                "substituted_batch": substituted_batch, "substitution": substitution,
            })
        substitutions = [dict(r) for r in self.db.execute("SELECT * FROM substitution_approvals WHERE lot_id=? ORDER BY requested_at", (lot_id,)).fetchall()]
        return {"lot": dict(lot), "materials": materials, "substitutions": substitutions}
