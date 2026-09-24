from __future__ import annotations

import unittest

from photon_fab.service import PhotonService


class TraceabilityTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PhotonService()
        self.service.bootstrap_admin()
        self.admin = self.service.auth.login("admin", "photon-admin")
        self.service.auth.create_user("engineer", "engineer-pass", "engineer")
        self.service.auth.create_user("quality", "quality-pass-1", "quality")
        self.service.auth.create_user("operator", "operator-pass", "operator")
        self.engineer = self.service.auth.login("engineer", "engineer-pass")
        self.quality = self.service.auth.login("quality", "quality-pass-1")
        self.operator = self.service.auth.login("operator", "operator-pass")
        self.service.create_lot(self.admin, "LOT-1", "CMOS image sensor", "P3.2", 10)

    def _seed_materials(self) -> tuple[str, str, str]:
        self.service.create_supplier(self.engineer, "SUP-EPI", "外延片供应商甲", ["epi_wafer"])
        self.service.create_supplier(self.engineer, "SUP-PKG", "封装材料供应商乙", ["package"])
        self.service.register_material_batch(self.engineer, "MB-EPI-1", "SUP-EPI", "epi_wafer", "EPI-1550-A", 25.0)
        self.service.register_material_batch(self.engineer, "MB-PKG-1", "SUP-PKG", "package", "QFN-64-R", 5000.0)
        self.service.register_material_batch(self.engineer, "MB-EPI-ALT", "SUP-EPI", "epi_wafer", "EPI-1550-B", 8.0)
        self.service.record_inspection(self.quality, "MB-EPI-1", "pass", "合格")
        self.service.record_inspection(self.quality, "MB-PKG-1", "pass", "合格")
        self.service.record_inspection(self.quality, "MB-EPI-ALT", "conditional", "限用")
        return "MB-EPI-1", "MB-PKG-1", "MB-EPI-ALT"


class SupplierTests(TraceabilityTestBase):
    def test_create_and_list_suppliers(self) -> None:
        self.service.create_supplier(self.engineer, "SUP-1", "供应商甲", ["epi_wafer", "package"], "a@x.com")
        supplier = self.service.get_supplier(self.quality, "SUP-1")
        self.assertEqual(supplier["name"], "供应商甲")
        self.assertEqual(supplier["material_types"], ["epi_wafer", "package"])
        self.assertTrue(supplier["active"])

    def test_operator_cannot_create_supplier(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.create_supplier(self.operator, "SUP-1", "供应商甲")

    def test_duplicate_supplier_rejected(self) -> None:
        self.service.create_supplier(self.engineer, "SUP-1", "供应商甲")
        with self.assertRaises(ValueError):
            self.service.create_supplier(self.engineer, "SUP-1", "供应商乙")

    def test_deactivate_keeps_record(self) -> None:
        self.service.create_supplier(self.engineer, "SUP-1", "供应商甲")
        result = self.service.deactivate_supplier(self.admin, "SUP-1")
        self.assertFalse(result["active"])
        with self.assertRaises(KeyError):
            self.service.register_material_batch(self.engineer, "MB-1", "SUP-1", "epi_wafer", "S", 1.0)


class MaterialBatchTests(TraceabilityTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.service.create_supplier(self.engineer, "SUP-1", "供应商甲", ["epi_wafer"])

    def test_register_requires_existing_active_supplier(self) -> None:
        with self.assertRaises(KeyError):
            self.service.register_material_batch(self.engineer, "MB-1", "NOPE", "epi_wafer", "S", 1.0)

    def test_inspection_quality_only(self) -> None:
        self.service.register_material_batch(self.engineer, "MB-1", "SUP-1", "epi_wafer", "S", 1.0)
        with self.assertRaises(PermissionError):
            self.service.record_inspection(self.engineer, "MB-1", "pass")
        inspection = self.service.record_inspection(self.quality, "MB-1", "pass", "ok", {"defect_density": 0.01})
        self.assertEqual(inspection["result"], "pass")
        self.assertEqual(inspection["metrics"]["defect_density"], 0.01)
        self.assertEqual(self.service.get_material_batch(self.quality, "MB-1")["status"], "accepted")

    def test_invalid_inspection_result(self) -> None:
        self.service.register_material_batch(self.engineer, "MB-1", "SUP-1", "epi_wafer", "S", 1.0)
        with self.assertRaises(ValueError):
            self.service.record_inspection(self.quality, "MB-1", "maybe")

    def test_unused_batch_can_update_and_delete(self) -> None:
        self.service.register_material_batch(self.engineer, "MB-1", "SUP-1", "epi_wafer", "S", 1.0)
        updated = self.service.update_material_batch(self.engineer, "MB-1", spec="S2", quantity=2.0)
        self.assertEqual(updated["spec"], "S2")
        self.assertEqual(updated["quantity"], 2.0)
        result = self.service.delete_material_batch(self.admin, "MB-1")
        self.assertTrue(result["deleted"])
        with self.assertRaises(KeyError):
            self.service.get_material_batch(self.quality, "MB-1")

    def test_used_batch_cannot_be_deleted_or_rewritten(self) -> None:
        epi, pkg, _alt = self._seed_materials()
        self.service.link_lot_material(self.engineer, "LOT-1", pkg, "package", 1000.0)
        with self.assertRaises(PermissionError):
            self.service.delete_material_batch(self.admin, pkg)
        with self.assertRaises(PermissionError):
            self.service.update_material_batch(self.admin, pkg, spec="TAMPERED")
        with self.assertRaises(PermissionError):
            self.service.record_inspection(self.quality, pkg, "fail")
        batch = self.service.get_material_batch(self.quality, pkg)
        self.assertEqual(batch["status"], "in_use")
        self.assertEqual(batch["spec"], "QFN-64-R")

    def test_link_requires_inspection(self) -> None:
        self.service.register_material_batch(self.engineer, "MB-X", "SUP-1", "epi_wafer", "S", 1.0)
        with self.assertRaises(PermissionError):
            self.service.link_lot_material(self.engineer, "LOT-1", "MB-X", "epi_wafer", 1.0)

    def test_failed_inspection_blocks_linking(self) -> None:
        self.service.register_material_batch(self.engineer, "MB-X", "SUP-1", "epi_wafer", "S", 1.0)
        self.service.record_inspection(self.quality, "MB-X", "fail")
        with self.assertRaises(PermissionError):
            self.service.link_lot_material(self.engineer, "LOT-1", "MB-X", "epi_wafer", 1.0)


class SubstitutionTests(TraceabilityTestBase):
    def test_substitution_must_be_quality_approved(self) -> None:
        _epi, _pkg, alt = self._seed_materials()
        sub = self.service.request_substitution(
            self.engineer, "LOT-1", "epi_wafer", "EPI-1550-A", alt, "余量不足",
        )
        self.assertEqual(sub["status"], "pending")
        with self.assertRaises(PermissionError):
            self.service.link_lot_material(self.engineer, "LOT-1", alt, "epi_wafer", 8.0, substitution_id=sub["substitution_id"])

    def test_engineer_cannot_approve_own_substitution(self) -> None:
        _epi, _pkg, alt = self._seed_materials()
        sub = self.service.request_substitution(
            self.engineer, "LOT-1", "epi_wafer", "EPI-1550-A", alt, "余量不足",
        )
        with self.assertRaises(PermissionError):
            self.service.decide_substitution(self.engineer, sub["substitution_id"], "approved", "同意")

    def test_approved_conditional_substitute_can_link(self) -> None:
        _epi, _pkg, alt = self._seed_materials()
        sub = self.service.request_substitution(
            self.engineer, "LOT-1", "epi_wafer", "EPI-1550-A", alt, "余量不足",
        )
        decided = self.service.decide_substitution(self.quality, sub["substitution_id"], "approved", "限用并加密抽检")
        self.assertEqual(decided["reviewer"], "quality")
        link = self.service.link_lot_material(
            self.engineer, "LOT-1", alt, "epi_wafer", 8.0, substitution_id=sub["substitution_id"],
        )
        self.assertEqual(link["substitution_id"], sub["substitution_id"])

    def test_rejected_substitution_cannot_link(self) -> None:
        _epi, _pkg, alt = self._seed_materials()
        sub = self.service.request_substitution(
            self.engineer, "LOT-1", "epi_wafer", "EPI-1550-A", alt, "余量不足",
        )
        self.service.decide_substitution(self.quality, sub["substitution_id"], "rejected", "风险过高")
        with self.assertRaises(PermissionError):
            self.service.link_lot_material(
                self.engineer, "LOT-1", alt, "epi_wafer", 8.0, substitution_id=sub["substitution_id"],
            )

    def test_decision_is_one_shot(self) -> None:
        _epi, _pkg, alt = self._seed_materials()
        sub = self.service.request_substitution(
            self.engineer, "LOT-1", "epi_wafer", "EPI-1550-A", alt, "余量不足",
        )
        self.service.decide_substitution(self.quality, sub["substitution_id"], "approved", "ok")
        with self.assertRaises(PermissionError):
            self.service.decide_substitution(self.quality, sub["substitution_id"], "rejected", "改判")

    def test_substitution_must_match_lot_and_batch(self) -> None:
        _epi, _pkg, alt = self._seed_materials()
        self.service.create_lot(self.admin, "LOT-2", "CMOS image sensor", "P3.2", 4)
        sub = self.service.request_substitution(
            self.engineer, "LOT-1", "epi_wafer", "EPI-1550-A", alt, "余量不足",
        )
        self.service.decide_substitution(self.quality, sub["substitution_id"], "approved", "ok")
        with self.assertRaises(ValueError):
            self.service.link_lot_material(
                self.engineer, "LOT-2", alt, "epi_wafer", 8.0, substitution_id=sub["substitution_id"],
            )

    def test_substitution_material_type_must_match(self) -> None:
        _epi, pkg, _alt = self._seed_materials()
        with self.assertRaises(ValueError):
            self.service.request_substitution(
                self.engineer, "LOT-1", "epi_wafer", "EPI-1550-A", pkg, "误用封装料",
            )


class TraceChainTests(TraceabilityTestBase):
    def _full_chain(self) -> dict:
        epi, pkg, alt = self._seed_materials()
        self.service.link_lot_material(self.engineer, "LOT-1", pkg, "package", 1000.0)
        sub = self.service.request_substitution(
            self.engineer, "LOT-1", "epi_wafer", "EPI-1550-A", alt, "余量不足",
        )
        self.service.decide_substitution(self.quality, sub["substitution_id"], "approved", "限用")
        self.service.link_lot_material(
            self.engineer, "LOT-1", alt, "epi_wafer", 8.0, substitution_id=sub["substitution_id"],
        )
        return self.service.trace_lot(self.quality, "LOT-1")

    def test_lot_trace_returns_full_multi_level_chain(self) -> None:
        trace = self._full_chain()
        self.assertEqual(trace["lot"]["lot_id"], "LOT-1")
        self.assertEqual(len(trace["materials"]), 2)
        by_batch = {m["material_batch"]["batch_id"]: m for m in trace["materials"]}
        pkg = by_batch["MB-PKG-1"]
        self.assertEqual(pkg["supplier"]["supplier_id"], "SUP-PKG")
        self.assertEqual(pkg["inspections"][0]["result"], "pass")
        self.assertIsNone(pkg["substitution"])
        alt = by_batch["MB-EPI-ALT"]
        self.assertEqual(alt["supplier"]["supplier_id"], "SUP-EPI")
        self.assertEqual(alt["inspections"][0]["result"], "conditional")
        self.assertEqual(alt["substitution"]["status"], "approved")
        self.assertEqual(alt["substitution"]["reviewer"], "quality")
        self.assertEqual(alt["link"]["role"], "epi_wafer")

    def test_trace_includes_measurements_approvals_and_events(self) -> None:
        self.service.add_measurement(self.admin, "LOT-1", 520, 0.93, 0.01, "spectrometer-1")
        trace = self._full_chain()
        self.assertEqual(len(trace["measurements"]), 1)
        event_types = {e["event_type"] for e in trace["events"]}
        self.assertIn("substitution_requested", event_types)
        self.assertIn("substitution_decided", event_types)
        self.assertIn("material_linked", event_types)

    def test_material_batch_forward_trace(self) -> None:
        trace = self._full_chain()
        batch_id = trace["materials"][0]["material_batch"]["batch_id"]
        forward = self.service.trace_material_batch(self.quality, batch_id)
        self.assertTrue(forward["locked"])
        self.assertEqual(len(forward["usages"]), 1)
        self.assertEqual(forward["usages"][0]["lot"]["lot_id"], "LOT-1")
        self.assertEqual(forward["supplier"]["name"], "封装材料供应商乙")

    def test_forward_trace_lists_substitution_requests(self) -> None:
        self._full_chain()
        forward = self.service.trace_material_batch(self.quality, "MB-EPI-ALT")
        self.assertEqual(len(forward["substitution_requests"]), 1)
        self.assertEqual(forward["substitution_requests"][0]["status"], "approved")


if __name__ == "__main__":
    unittest.main()
