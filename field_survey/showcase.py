"""本地演练：编辑并发、进程恢复与不支持几何的处理。

全程使用本地合成资料：geopackage_fixture 生成真实 SQLite GeoPackage 并
保留 WAL 写连接模拟调查员编辑；采集端只读接入；台账是独立的本地小库。
"""

import hashlib
import tempfile
from pathlib import Path

from .collection import CollectionJournal, collect_package, export_collection
from .geopackage import SnapshotReader, source_facts
from .geopackage_fixture import build, point_geometry


def _source_digest(path):
    digest = hashlib.sha256()
    for suffix in ("", "-wal"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            digest.update(candidate.read_bytes())
    return digest.hexdigest()


def _class_totals(manifest):
    totals = {}
    for layer in manifest["layers"]:
        for name, count in layer["geometry_classes"].items():
            totals[name] = totals.get(name, 0) + count
    return totals


def run(workdir=None):
    temporary = None
    if workdir is None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
    else:
        root = Path(workdir)
        root.mkdir(parents=True, exist_ok=True)
    package = root / "survey.gpkg"
    journal_path = root / "collection.sqlite3"
    export_path = root / "export.json"
    writer = build(package)
    steps = []
    try:
        # 1) 编辑并发：读取事务进行期间，调查员的提交不影响该事务的快照
        reader = SnapshotReader(package)
        reader.begin()
        with writer:
            writer.execute(
                "INSERT INTO track_points VALUES (?,?,?,?)",
                (901, 901, "并发补测", point_geometry(121.5, 31.1)),
            )
        _, rows = reader.fetch_batch("track_points", "fid", None, 100000)
        pinned_rows = len(rows)
        reader.close()
        fresh = SnapshotReader(package)
        fresh.begin()
        _, fresh_rows = fresh.fetch_batch("track_points", "fid", None, 100000)
        fresh.close()
        steps.append({
            "step": "edit_concurrency",
            "pinned_snapshot_rows": pinned_rows,
            "fresh_snapshot_rows": len(fresh_rows),
            "note": "进行中的读取事务保持自己的快照；调查员的新提交只在下一份快照出现",
        })

        # 2) 进程领了两批后退出（每次采集调用是一个进程生命周期）
        first = collect_package(package, journal_path, batch_size=100, max_batches=2)
        steps.append({
            "step": "partial_run_then_exit",
            "snapshot_id": first["snapshot_id"],
            "complete": first["complete"],
            "layers": first["layers"],
            "note": "批次预算耗尽即退出，模拟进程中途结束",
        })

        # 3) 同一快照续接：只有同一快照的偏移才会被沿用
        second = collect_package(package, journal_path, batch_size=100)
        steps.append({
            "step": "resume_same_snapshot",
            "same_snapshot": second["snapshot_id"] == first["snapshot_id"],
            "resumed_from_pk": second["layers"]["track_points"]["resumed_from_pk"],
            "complete": second["complete"],
        })

        # 4) 调查员继续编辑并提交（仍在 WAL，未检查点）
        with writer:
            writer.execute("INSERT INTO points VALUES (?,?,?)", (90, "编辑中新增", point_geometry(119.9, 29.9)))
            writer.execute("UPDATE points SET name=? WHERE fid=1", ("复核改名",))
        steps.append({
            "step": "investigator_keeps_editing",
            "wal_frames": source_facts(package).get("wal_frames", 0),
            "note": "提交仍只在 WAL 中，未检查点；WAL 中的已提交内容属于来源事实",
        })

        # 5) 新快照重新领取：不沿用上一快照的偏移
        third = collect_package(package, journal_path, batch_size=100)
        steps.append({
            "step": "recollect_new_snapshot",
            "new_snapshot": third["snapshot_id"] != second["snapshot_id"],
            "resumed_from_pk": third["layers"]["track_points"]["resumed_from_pk"],
            "complete": third["complete"],
            "note": "快照已变，上一快照的偏移不作为续接证明，图层重新领取",
        })

        # 6) 只读核验：采集前后源文件摘要一致
        before = _source_digest(package)
        collect_package(package, journal_path)
        source_unmodified = before == _source_digest(package)
        steps.append({"step": "read_only_verified", "source_unmodified": source_unmodified})

        # 7) 导出：清单说明本批记录基于哪份快照、哪些层被隔离
        manifest = export_collection(journal_path, export_path)
        journal = CollectionJournal(journal_path)
        try:
            snapshots = journal.snapshots_overview()
        finally:
            journal.close()
        steps.append({
            "step": "export",
            "path": str(export_path),
            "snapshot_id": manifest["snapshot"]["id"],
            "quarantined_layers": manifest["quarantined_layers"],
            "geometry_classes": _class_totals(manifest),
        })
        return {
            "workdir": str(root),
            "package": str(package),
            "journal": str(journal_path),
            "steps": steps,
            "snapshots": snapshots,
            "quarantined_layers": manifest["quarantined_layers"],
            "source_unmodified": source_unmodified,
            "temporary_workdir": temporary is not None,
        }
    finally:
        writer.close()
        if temporary is not None:
            temporary.cleanup()
