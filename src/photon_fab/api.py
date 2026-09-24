"""用于离线验收的无依赖 JSON HTTP API。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .service import PhotonService


class Handler(BaseHTTPRequestHandler):
    service = PhotonService()

    def _json(self, status: int, body: dict | list) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if not raw:
            return {}
        return json.loads(raw)

    def _token(self) -> str:
        return self.headers.get("Authorization", "").removeprefix("Bearer ")

    def _serve(self, fn):
        try:
            return fn()
        except PermissionError as exc:
            return self._json(403, {"error": str(exc)})
        except KeyError as exc:
            return self._json(404, {"error": f"not found: {exc.args[0]}"})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})

    # ---------- GET ----------

    def do_GET(self):
        path = self.path
        if path == "/health":
            return self._json(200, {"status": "ok", "service": "photon-fab"})
        token = self._token()
        parts = path.strip("/").split("/")

        def route():
            if path == "/suppliers":
                return self._json(200, {"suppliers": self.service.list_suppliers(token)})
            if len(parts) == 2 and parts[0] == "suppliers":
                return self._json(200, self.service.get_supplier(token, parts[1]))
            if len(parts) == 3 and parts[0] == "material-batches":
                if parts[2] == "inspections":
                    return self._json(200, {"inspections": self.service.list_inspections(token, parts[1])})
                if parts[2] == "trace":
                    return self._json(200, self.service.trace_material_batch(token, parts[1]))
            if len(parts) == 2 and parts[0] == "material-batches":
                return self._json(200, self.service.get_material_batch(token, parts[1]))
            if len(parts) == 2 and parts[0] == "lots":
                return self._json(200, self.service.get_lot(token, parts[1]))
            if len(parts) == 3 and parts[0] == "lots":
                if parts[2] == "materials":
                    return self._json(200, {"materials": self.service.list_lot_materials(token, parts[1])})
                if parts[2] == "trace":
                    return self._json(200, self.service.trace_lot(token, parts[1]))
                return self._json(404, {"error": "not found"})
            if len(parts) == 2 and parts[0] == "substitutions":
                return self._json(200, self.service.get_substitution(token, parts[1]))
            return self._json(404, {"error": "not found"})

        return self._serve(route)

    # ---------- POST ----------

    def do_POST(self):
        path = self.path

        def route():
            body = self._body()
            if path == "/login":
                return self._json(200, {"token": self.service.auth.login(body["user_id"], body["password"])})
            token = self._token()
            parts = path.strip("/").split("/")
            if path == "/suppliers":
                result = self.service.create_supplier(token, body["supplier_id"], body["name"], body.get("material_types"), body.get("contact", ""))
                return self._json(201, result)
            if len(parts) == 3 and parts[0] == "suppliers" and parts[2] == "deactivate":
                return self._json(200, self.service.deactivate_supplier(token, parts[1]))
            if path == "/material-batches":
                result = self.service.register_material_batch(
                    token, body["batch_id"], body["supplier_id"], body["material_type"],
                    body["spec"], body["quantity"], body.get("received_at"),
                )
                return self._json(201, result)
            if len(parts) == 3 and parts[0] == "material-batches" and parts[2] == "inspections":
                result = self.service.record_inspection(token, parts[1], body["result"], body.get("findings", ""), body.get("metrics"))
                return self._json(201, result)
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "materials":
                result = self.service.link_lot_material(
                    token, parts[1], body["batch_id"], body["role"], body["quantity"], body.get("substitution_id"),
                )
                return self._json(201, result)
            if path == "/substitutions":
                result = self.service.request_substitution(
                    token, body["lot_id"], body["material_type"], body["intended_spec"],
                    body["substitute_batch_id"], body["reason"],
                )
                return self._json(201, result)
            if len(parts) == 3 and parts[0] == "substitutions" and parts[2] == "decision":
                result = self.service.decide_substitution(token, parts[1], body["decision"], body["reason"])
                return self._json(200, result)
            if path == "/lots":
                return self._json(201, self.service.create_lot(token, body["lot_id"], body["product"], body["process_rev"], body["wafer_count"]))
            if path.endswith("/measurements") and path.startswith("/lots/"):
                lot_id = path.split("/")[2]
                return self._json(201, self.service.add_measurement(token, lot_id, body["wavelength_nm"], body["response"], body.get("noise", 0.0), body["instrument"]))
            if path.endswith("/analysis") and path.startswith("/lots/"):
                return self._json(200, self.service.analyze(token, path.split("/")[2]))
            return self._json(404, {"error": "not found"})

        return self._serve(route)

    # ---------- PATCH / DELETE ----------

    def do_PATCH(self):
        parts = self.path.strip("/").split("/")

        def route():
            body = self._body()
            token = self._token()
            if len(parts) == 2 and parts[0] == "material-batches":
                return self._json(200, self.service.update_material_batch(token, parts[1], **body))
            return self._json(404, {"error": "not found"})

        return self._serve(route)

    def do_DELETE(self):
        parts = self.path.strip("/").split("/")

        def route():
            token = self._token()
            if len(parts) == 2 and parts[0] == "material-batches":
                return self._json(200, self.service.delete_material_batch(token, parts[1]))
            return self._json(404, {"error": "not found"})

        return self._serve(route)

    def log_message(self, fmt, *args):  # 静默标准访问日志
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=":memory:")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    Handler.service = PhotonService(args.database)
    Handler.service.bootstrap_admin()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
