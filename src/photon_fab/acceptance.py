"""容器和快照检查使用的冒烟验收命令。"""

from __future__ import annotations

import argparse
import json

from .service import PhotonService


def run() -> dict:
    service = PhotonService()
    service.bootstrap_admin()
    admin = service.auth.login("admin", "photon-admin")

    service.auth.create_user("engineer", "engineer-pass", "engineer")
    service.auth.create_user("quality", "quality-pass-1", "quality")
    engineer = service.auth.login("engineer", "engineer-pass")
    quality = service.auth.login("quality", "quality-pass-1")

    service.create_lot(admin, "LOT-DEMO", "CMOS image sensor", "P3.2", 10)
    for wavelength, response in ((450, .71), (520, .93), (650, .84)):
        service.add_measurement(admin, "LOT-DEMO", wavelength, response, .01, "spectrometer-1")
    result = service.analyze(admin, "LOT-DEMO")
    service.approve(quality, "LOT-DEMO", "hold", "awaiting quality review")

    # 供应商与来料批次：不同供应商的外延片和封装材料进入同一批次
    service.create_supplier(engineer, "SUP-EPI", "外延片供应商甲", ["epi_wafer"], "epi@example.com")
    service.create_supplier(engineer, "SUP-PKG", "封装材料供应商乙", ["package"], "pkg@example.com")
    service.register_material_batch(engineer, "MB-EPI-1", "SUP-EPI", "epi_wafer", "EPI-1550-A", 25.0)
    service.register_material_batch(engineer, "MB-PKG-1", "SUP-PKG", "package", "QFN-64-R", 5000.0)
    service.register_material_batch(engineer, "MB-EPI-ALT", "SUP-EPI", "epi_wafer", "EPI-1550-B", 8.0)

    # 来料检验（仅质量角色）
    service.record_inspection(quality, "MB-EPI-1", "pass", "厚度与缺陷密度合格", {"defect_density": 0.02})
    service.record_inspection(quality, "MB-PKG-1", "pass", "开封检查合格")
    service.record_inspection(quality, "MB-EPI-ALT", "conditional", "缺陷密度接近上限，限用", {"defect_density": 0.09})

    # 已检验合格的常规物料直接关联
    service.link_lot_material(engineer, "LOT-DEMO", "MB-PKG-1", "package", 1000.0)

    # 替代料：工程师申请，质量审批通过后才能投产
    substitution = service.request_substitution(
        engineer, "LOT-DEMO", "epi_wafer", "EPI-1550-A", "MB-EPI-ALT", "主供应商批次余量不足",
    )
    service.decide_substitution(quality, substitution["substitution_id"], "approved", "conditional 批次限用于本批并加密抽检")
    service.link_lot_material(
        engineer, "LOT-DEMO", "MB-EPI-ALT", "epi_wafer", 8.0, substitution_id=substitution["substitution_id"],
    )

    # 已投产批次禁止删除或改写
    locked = True
    try:
        service.delete_material_batch(engineer, "MB-EPI-ALT")
        locked = False
    except PermissionError:
        pass
    try:
        service.update_material_batch(engineer, "MB-EPI-ALT", spec="TAMPERED")
        locked = False
    except PermissionError:
        pass

    trace = service.trace_lot(quality, "LOT-DEMO")
    chain_lengths = [
        len([m["supplier"], m["material_batch"], *m["inspections"]]) for m in trace["materials"]
    ]
    return {
        "status": "ok",
        "lot": result["lot_id"],
        "peak": result["spectrum"]["peak_wavelength_nm"],
        "events": len(service.audit(admin, "LOT-DEMO")),
        "materials": len(trace["materials"]),
        "suppliers": len({m["supplier"]["supplier_id"] for m in trace["materials"]}),
        "substitutions": sum(1 for m in trace["materials"] if m["substitution"] and m["substitution"]["status"] == "approved"),
        "used_batch_locked": locked,
        "min_trace_chain_nodes": min(chain_lengths),
    }


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
