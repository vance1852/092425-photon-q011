from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from photon_fab.api import Handler
from photon_fab.service import PhotonService


class TraceabilityServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PhotonService()
        self.service.bootstrap_admin()
        self.service.auth.create_user("eng", "engineer-pass", "engineer")
        self.service.auth.create_user("qa", "quality-pass", "quality")
        self.service.auth.create_user("op", "operator-pass", "operator")
        self.admin = self.service.auth.login("admin", "photon-admin")
        self.eng = self.service.auth.login("eng", "engineer-pass")
        self.qa = self.service.auth.login("qa", "quality-pass")
        self.op = self.service.auth.login("op", "operator-pass")
        self.service.create_lot(self.eng, "LOT-1", "DFB laser", "P1.0", 20)
        self.service.create_supplier(self.eng, "SUP-A", "外延供应商A", "epi_wafer")
        self.service.create_supplier(self.eng, "SUP-B", "封装供应商B", "packaging")
        self.service.register_material_batch(self.eng, "MB-EPI-1", "SUP-A", "EPI-0901", "epi_wafer", 50, "pcs")
        self.service.register_material_batch(self.eng, "MB-PKG-1", "SUP-B", "PKG-0901", "packaging", 200, "pcs")

    def test_multi_supplier_trace_chain(self) -> None:
        self.service.record_inspection(self.qa, "MB-EPI-1", "pass", "外观与电阻率合格")
        self.service.record_inspection(self.qa, "MB-PKG-1", "conditional", "待复测")
        self.service.use_material(self.eng, "LOT-1", "MB-EPI-1", 10, "外延生长")
        self.service.use_material(self.eng, "LOT-1", "MB-PKG-1", 40, "封装")
        trace = self.service.trace_lot(self.qa, "LOT-1")
        self.assertEqual(trace["lot"]["lot_id"], "LOT-1")
        self.assertEqual(len(trace["materials"]), 2)
        epi = next(m for m in trace["materials"] if m["batch"]["batch_id"] == "MB-EPI-1")
        self.assertEqual(epi["supplier"]["supplier_id"], "SUP-A")
        self.assertEqual(epi["batch"]["supplier_lot"], "EPI-0901")
        self.assertEqual(epi["inspections"][0]["result"], "pass")
        self.assertEqual(epi["inspections"][0]["inspector"], "qa")
        self.assertFalse(epi["is_substitute"])
        self.assertIsNone(epi["substitution"])
        pkg = next(m for m in trace["materials"] if m["batch"]["batch_id"] == "MB-PKG-1")
        self.assertEqual(pkg["supplier"]["name"], "封装供应商B")
        self.assertEqual(pkg["inspections"][0]["result"], "conditional")

    def test_used_batch_cannot_be_updated_or_deleted(self) -> None:
        self.service.use_material(self.eng, "LOT-1", "MB-EPI-1", 10, "外延生长")
        with self.assertRaises(ValueError):
            self.service.update_material_batch(self.eng, "MB-EPI-1", supplier_lot="EPI-9999")
        with self.assertRaises(ValueError):
            self.service.delete_material_batch(self.eng, "MB-EPI-1")
        with self.assertRaises(ValueError):
            self.service.delete_material_batch(self.admin, "MB-EPI-1")
        batch = self.service.get_material_batch(self.qa, "MB-EPI-1")
        self.assertEqual(batch["supplier_lot"], "EPI-0901")
        self.assertEqual(batch["used_by_lots"], ["LOT-1"])

    def test_unused_batch_can_be_updated_and_deleted(self) -> None:
        updated = self.service.update_material_batch(self.eng, "MB-PKG-1", supplier_lot="PKG-0902", quantity=300)
        self.assertEqual(updated["supplier_lot"], "PKG-0902")
        self.assertEqual(updated["quantity"], 300.0)
        self.assertEqual(self.service.delete_material_batch(self.eng, "MB-PKG-1"), {"deleted": "MB-PKG-1"})
        with self.assertRaises(KeyError):
            self.service.get_material_batch(self.qa, "MB-PKG-1")

    def test_substitute_requires_quality_approval(self) -> None:
        self.service.register_material_batch(self.eng, "MB-EPI-2", "SUP-A", "EPI-0902", "epi_wafer", 50, "pcs")
        with self.assertRaises(PermissionError):
            self.service.use_material(self.eng, "LOT-1", "MB-EPI-2", 10, "外延生长", substitute_for="MB-EPI-1")
        request = self.service.request_substitution(self.eng, "LOT-1", "MB-EPI-2", "MB-EPI-1", "原批次来料检验不合格")
        self.assertEqual(request["decision"], "pending")
        approved = self.service.review_substitution(self.qa, request["approval_id"], "approved", "同意替代")
        self.assertEqual(approved["decision"], "approved")
        self.assertEqual(approved["reviewer"], "qa")
        usage = self.service.use_material(self.eng, "LOT-1", "MB-EPI-2", 10, "外延生长", substitute_for="MB-EPI-1")
        self.assertTrue(usage["usage_id"])
        trace = self.service.trace_lot(self.qa, "LOT-1")
        material = trace["materials"][0]
        self.assertTrue(material["is_substitute"])
        self.assertEqual(material["substitution"]["decision"], "approved")
        self.assertEqual(material["substitution"]["reviewer"], "qa")
        self.assertEqual(material["substituted_batch"]["batch_id"], "MB-EPI-1")
        self.assertEqual(material["substituted_batch"]["supplier"]["supplier_id"], "SUP-A")
        self.assertEqual(len(trace["substitutions"]), 1)

    def test_rejected_substitute_blocks_usage(self) -> None:
        self.service.register_material_batch(self.eng, "MB-EPI-2", "SUP-A", "EPI-0902", "epi_wafer", 50, "pcs")
        request = self.service.request_substitution(self.eng, "LOT-1", "MB-EPI-2", "MB-EPI-1", "试替代")
        self.service.review_substitution(self.qa, request["approval_id"], "rejected", "风险过高")
        with self.assertRaises(PermissionError):
            self.service.use_material(self.eng, "LOT-1", "MB-EPI-2", 10, "外延生长", substitute_for="MB-EPI-1")

    def test_substitution_decision_is_final(self) -> None:
        self.service.register_material_batch(self.eng, "MB-EPI-2", "SUP-A", "EPI-0902", "epi_wafer", 50, "pcs")
        request = self.service.request_substitution(self.eng, "LOT-1", "MB-EPI-2", "MB-EPI-1", "试替代")
        self.service.review_substitution(self.qa, request["approval_id"], "approved", "同意")
        with self.assertRaises(ValueError):
            self.service.review_substitution(self.qa, request["approval_id"], "rejected", "反悔")
        with self.assertRaises(ValueError):
            self.service.request_substitution(self.eng, "LOT-1", "MB-EPI-2", "MB-EPI-1", "重复申请")

    def test_role_permissions(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.record_inspection(self.eng, "MB-EPI-1", "pass", "x")
        with self.assertRaises(PermissionError):
            self.service.record_inspection(self.op, "MB-EPI-1", "pass", "x")
        with self.assertRaises(PermissionError):
            self.service.create_supplier(self.op, "SUP-C", "x", "epi_wafer")
        self.service.register_material_batch(self.eng, "MB-EPI-2", "SUP-A", "EPI-0902", "epi_wafer", 50, "pcs")
        request = self.service.request_substitution(self.eng, "LOT-1", "MB-EPI-2", "MB-EPI-1", "r")
        with self.assertRaises(PermissionError):
            self.service.review_substitution(self.eng, request["approval_id"], "approved", "x")

    def test_validation(self) -> None:
        with self.assertRaises(ValueError):
            self.service.register_material_batch(self.eng, "MB-X", "SUP-A", "L1", "copper", 10, "pcs")
        with self.assertRaises(ValueError):
            self.service.register_material_batch(self.eng, "MB-X", "SUP-A", "L1", "epi_wafer", 0, "pcs")
        with self.assertRaises(KeyError):
            self.service.register_material_batch(self.eng, "MB-X", "SUP-Z", "L1", "epi_wafer", 10, "pcs")
        with self.assertRaises(ValueError):
            self.service.record_inspection(self.qa, "MB-EPI-1", "maybe", "x")
        with self.assertRaises(KeyError):
            self.service.trace_lot(self.qa, "LOT-404")
        with self.assertRaises(ValueError):
            self.service.update_material_batch(self.eng, "MB-EPI-1")


class TraceabilityApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        class TestHandler(Handler):
            pass

        TestHandler.service = PhotonService()
        TestHandler.service.bootstrap_admin()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def call(self, method: str, path: str, body: dict | None = None, token: str | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        data = None
        if body is not None:
            request.add_header("Content-Type", "application/json")
            data = json.dumps(body).encode()
        try:
            with urllib.request.urlopen(request, data) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_traceability_flow_over_http(self) -> None:
        status, body = self.call("POST", "/login", {"user_id": "admin", "password": "photon-admin"})
        self.assertEqual(status, 200)
        token = body["token"]
        status, _ = self.call("POST", "/lots", {"lot_id": "LOT-API", "product": "DFB laser", "process_rev": "P1.0", "wafer_count": 12}, token)
        self.assertEqual(status, 201)
        status, _ = self.call("POST", "/suppliers", {"supplier_id": "SUP-API", "name": "供应商", "material_scope": "epi_wafer"}, token)
        self.assertEqual(status, 201)
        status, _ = self.call("POST", "/material-batches", {"batch_id": "MB-API-1", "supplier_id": "SUP-API", "supplier_lot": "EPI-1", "material_type": "epi_wafer", "quantity": 30, "unit": "pcs"}, token)
        self.assertEqual(status, 201)
        status, _ = self.call("POST", "/material-batches/MB-API-1/inspections", {"result": "pass", "notes": "合格"}, token)
        self.assertEqual(status, 201)
        status, _ = self.call("POST", "/lots/LOT-API/materials", {"batch_id": "MB-API-1", "quantity": 12, "purpose": "外延生长"}, token)
        self.assertEqual(status, 201)
        status, trace = self.call("GET", "/lots/LOT-API/trace", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(trace["materials"][0]["supplier"]["supplier_id"], "SUP-API")
        self.assertEqual(trace["materials"][0]["inspections"][0]["result"], "pass")
        status, _ = self.call("PUT", "/material-batches/MB-API-1", {"supplier_lot": "EPI-2"}, token)
        self.assertEqual(status, 400)
        status, _ = self.call("DELETE", "/material-batches/MB-API-1", token=token)
        self.assertEqual(status, 400)
        status, _ = self.call("POST", "/material-batches", {"batch_id": "MB-API-2", "supplier_id": "SUP-API", "supplier_lot": "EPI-9", "material_type": "epi_wafer", "quantity": 5, "unit": "pcs"}, token)
        self.assertEqual(status, 201)
        status, _ = self.call("DELETE", "/material-batches/MB-API-2", token=token)
        self.assertEqual(status, 200)
        status, _ = self.call("GET", "/material-batches/MB-API-2", token=token)
        self.assertEqual(status, 404)
        status, _ = self.call("GET", "/lots/LOT-API/trace")
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
