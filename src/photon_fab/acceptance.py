"""容器和快照检查使用的冒烟验收命令。"""

from __future__ import annotations

import argparse
import json

from .service import PhotonService


def run() -> dict:
    service = PhotonService()
    service.bootstrap_admin()
    token = service.auth.login("admin", "photon-admin")
    service.create_lot(token, "LOT-DEMO", "CMOS image sensor", "P3.2", 10)
    for wavelength, response in ((450, .71), (520, .93), (650, .84)):
        service.add_measurement(token, "LOT-DEMO", wavelength, response, .01, "spectrometer-1")
    result = service.analyze(token, "LOT-DEMO")
    service.approve(token, "LOT-DEMO", "hold", "awaiting quality review")
    service.create_supplier(token, "SUP-EPI", "EpiWafer Co", "epi_wafer")
    service.register_material_batch(token, "MB-EPI-DEMO", "SUP-EPI", "EPI-2026-0901", "epi_wafer", 25, "pcs")
    service.record_inspection(token, "MB-EPI-DEMO", "pass", "incoming inspection ok")
    service.use_material(token, "LOT-DEMO", "MB-EPI-DEMO", 10, "epi growth")
    trace = service.trace_lot(token, "LOT-DEMO")
    return {"status": "ok", "lot": result["lot_id"], "peak": result["spectrum"]["peak_wavelength_nm"], "events": len(service.audit(token, "LOT-DEMO")), "trace_materials": len(trace["materials"])}


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
