"""采集台账：本地小型数据库，保存快照身份、图层状态、原始记录与续接进度。

台账是采集端自己的库，与来源 GeoPackage 完全分离；来源只读。进度按
（快照，图层）保存：进程退出后继续时，只有同一份快照才有可沿用的偏移；
另一份快照没有可继承的游标，图层只能重新领取。
"""

import hashlib
import json
import math
import sqlite3
from pathlib import Path

from .geopackage import SnapshotReader, classify_geometry

SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    application_id INTEGER,
    user_version INTEGER,
    first_seen TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    package_id INTEGER NOT NULL REFERENCES packages(id),
    token TEXT NOT NULL,
    facts TEXT NOT NULL,
    opened_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(package_id, token)
);
CREATE TABLE IF NOT EXISTS layers (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    table_name TEXT NOT NULL,
    geom_column TEXT,
    declared_type TEXT,
    srs_id INTEGER,
    pk_column TEXT,
    status TEXT NOT NULL,
    reasons TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(snapshot_id, table_name)
);
CREATE TABLE IF NOT EXISTS records (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    layer TEXT NOT NULL,
    pk INTEGER NOT NULL,
    geom BLOB,
    geom_class TEXT NOT NULL,
    geom_summary TEXT NOT NULL,
    attributes TEXT NOT NULL,
    collected_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(snapshot_id, layer, pk)
);
CREATE TABLE IF NOT EXISTS progress (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    layer TEXT NOT NULL,
    last_pk INTEGER,
    done INTEGER NOT NULL DEFAULT 0,
    batches INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(snapshot_id, layer)
);
CREATE TABLE IF NOT EXISTS exports (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    manifest TEXT NOT NULL
);
"""


def _jsonable(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"blob_sha256": hashlib.sha256(bytes(value)).hexdigest()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


class CollectionJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)

    def close(self):
        self.connection.close()

    def package_id(self, path, application_id, user_version):
        row = self.connection.execute("SELECT id FROM packages WHERE path=?", (path,)).fetchone()
        if row is not None:
            return row["id"]
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO packages(path, application_id, user_version) VALUES (?,?,?)",
                (path, application_id, user_version),
            )
        return cursor.lastrowid

    def snapshot_id(self, package_id, token, facts):
        row = self.connection.execute(
            "SELECT id FROM snapshots WHERE package_id=? AND token=?", (package_id, token)
        ).fetchone()
        if row is not None:
            return row["id"], False
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO snapshots(package_id, token, facts) VALUES (?,?,?)",
                (package_id, token, json.dumps(facts, sort_keys=True)),
            )
        return cursor.lastrowid, True

    def record_layer(self, snapshot_id, plan, status, pk_column=None):
        with self.connection:
            self.connection.execute(
                """INSERT INTO layers(snapshot_id, table_name, geom_column, declared_type, srs_id, pk_column, status, reasons)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(snapshot_id, table_name) DO UPDATE SET
                       status=excluded.status,
                       reasons=excluded.reasons,
                       pk_column=COALESCE(excluded.pk_column, layers.pk_column)""",
                (
                    snapshot_id, plan.table, plan.geom_column, plan.declared_type,
                    plan.srs_id, pk_column, status,
                    json.dumps(plan.reasons, ensure_ascii=False),
                ),
            )

    def progress(self, snapshot_id, layer):
        return self.connection.execute(
            "SELECT * FROM progress WHERE snapshot_id=? AND layer=?", (snapshot_id, layer)
        ).fetchone()

    def save_batch(self, snapshot_id, plan, pk_column, columns, rows, last_pk, done):
        with self.connection:
            for row in rows:
                values = dict(zip(columns, row))
                pk = values.pop(pk_column)
                geom = values.pop(plan.geom_column)
                geom_class, details = classify_geometry(geom)
                attributes = {name: _jsonable(value) for name, value in values.items()}
                self.connection.execute(
                    "INSERT OR REPLACE INTO records(snapshot_id, layer, pk, geom, geom_class, geom_summary, attributes)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (
                        snapshot_id, plan.table, pk, geom, geom_class,
                        json.dumps(_jsonable(details), ensure_ascii=False, sort_keys=True),
                        json.dumps(attributes, ensure_ascii=False, sort_keys=True),
                    ),
                )
            self.connection.execute(
                """INSERT INTO progress(snapshot_id, layer, last_pk, done, batches) VALUES (?,?,?,?,1)
                   ON CONFLICT(snapshot_id, layer) DO UPDATE SET
                       last_pk=COALESCE(excluded.last_pk, progress.last_pk),
                       done=excluded.done,
                       batches=progress.batches + 1,
                       updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
                (snapshot_id, plan.table, last_pk, int(done)),
            )

    def latest_snapshot_id(self):
        row = self.connection.execute("SELECT id FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            raise KeyError("台账中还没有任何快照")
        return row["id"]

    def snapshots_overview(self):
        rows = self.connection.execute(
            """SELECT s.id, s.token, s.opened_at,
                      (SELECT count(*) FROM records r WHERE r.snapshot_id=s.id) AS records
               FROM snapshots s ORDER BY s.id"""
        ).fetchall()
        return [dict(row) for row in rows]

    def record_export(self, snapshot_id, manifest):
        with self.connection:
            self.connection.execute(
                "INSERT INTO exports(snapshot_id, manifest) VALUES (?,?)",
                (snapshot_id, json.dumps(manifest, ensure_ascii=False, sort_keys=True)),
            )

    def manifest(self, snapshot_id):
        snapshot = self.connection.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if snapshot is None:
            raise KeyError("未知快照: %s" % snapshot_id)
        package = self.connection.execute("SELECT * FROM packages WHERE id=?", (snapshot["package_id"],)).fetchone()
        layers = []
        quarantined = []
        for row in self.connection.execute(
            "SELECT * FROM layers WHERE snapshot_id=? ORDER BY table_name", (snapshot_id,)
        ).fetchall():
            classes = dict(self.connection.execute(
                "SELECT geom_class, count(*) FROM records WHERE snapshot_id=? AND layer=? GROUP BY geom_class",
                (snapshot_id, row["table_name"]),
            ).fetchall())
            entry = {
                "table": row["table_name"],
                "status": row["status"],
                "geom_column": row["geom_column"],
                "declared_type": row["declared_type"],
                "srs_id": row["srs_id"],
                "reasons": json.loads(row["reasons"]),
                "records": sum(classes.values()),
                "geometry_classes": classes,
            }
            layers.append(entry)
            if row["status"] == "quarantined":
                quarantined.append({"table": row["table_name"], "reasons": entry["reasons"]})
        records = []
        for row in self.connection.execute(
            "SELECT * FROM records WHERE snapshot_id=? ORDER BY layer, pk", (snapshot_id,)
        ).fetchall():
            records.append({
                "layer": row["layer"],
                "pk": row["pk"],
                "geometry_class": row["geom_class"],
                "geometry_sha256": hashlib.sha256(row["geom"]).hexdigest() if row["geom"] is not None else None,
                "geometry": json.loads(row["geom_summary"]),
                "attributes": json.loads(row["attributes"]),
            })
        return {
            "format": "field-survey/gpkg-export-v1",
            "package": {
                "path": package["path"],
                "application_id": package["application_id"],
                "user_version": package["user_version"],
            },
            "snapshot": {
                "id": snapshot["id"],
                "token": snapshot["token"],
                "opened_at": snapshot["opened_at"],
                "facts": json.loads(snapshot["facts"]),
            },
            "layers": layers,
            "quarantined_layers": quarantined,
            "records": records,
            "notes": [
                "本批记录全部来自上述同一份读取快照",
                "几何坐标一律取自WKB原文；空、缺失与不支持的几何不附坐标，不以显示范围推断位置",
                "原始GeoPackageBinary以BLOB保存在采集台账，未做坐标变换",
            ],
        }


def collect_package(package_path, journal_path, layers=None, batch_size=500, max_batches=None):
    """对来源 GeoPackage 做一轮只读采集；max_batches 限制本次进程生命周期的批数。"""
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    if max_batches is not None and (isinstance(max_batches, bool) or not isinstance(max_batches, int) or max_batches < 1):
        raise ValueError("max_batches must be a positive integer")
    journal = CollectionJournal(journal_path)
    reader = SnapshotReader(package_path)
    try:
        reader.begin()
        metadata = reader.identify()
        package_id = journal.package_id(str(reader.path.resolve()), metadata.application_id, metadata.user_version)
        snapshot_id, created = journal.snapshot_id(package_id, reader.token, reader.facts)
        selected = set(layers) if layers else None
        summary = {
            "package": str(reader.path.resolve()),
            "snapshot_id": snapshot_id,
            "snapshot_token": reader.token,
            "new_snapshot": created,
            "snapshot_facts": reader.facts,
            "layers": {},
            "quarantined": [],
            "complete": True,
        }
        if selected is not None:
            summary["unknown_layers"] = sorted(selected - {plan.table for plan in metadata.layers})
        budget = max_batches
        for plan in metadata.layers:
            if plan.reasons:
                journal.record_layer(snapshot_id, plan, "quarantined")
                summary["quarantined"].append({"layer": plan.table, "reasons": plan.reasons})
                continue
            if selected is not None and plan.table not in selected:
                journal.record_layer(snapshot_id, plan, "not_selected")
                continue
            pk_column = reader.primary_key(plan.table)
            if pk_column is None:
                plan.reasons.append("no_integer_primary_key")
                journal.record_layer(snapshot_id, plan, "quarantined")
                summary["quarantined"].append({"layer": plan.table, "reasons": plan.reasons})
                continue
            journal.record_layer(snapshot_id, plan, "collecting", pk_column)
            progress = journal.progress(snapshot_id, plan.table)
            last_pk = progress["last_pk"] if progress else None
            done = bool(progress and progress["done"])
            collected = 0
            batches = 0
            while not done:
                if budget is not None and budget == 0:
                    summary["complete"] = False
                    break
                columns, rows = reader.fetch_batch(plan.table, pk_column, last_pk, batch_size)
                if rows:
                    last_pk = rows[-1][columns.index(pk_column)]
                done = len(rows) < batch_size
                journal.save_batch(snapshot_id, plan, pk_column, columns, rows, last_pk, done)
                collected += len(rows)
                batches += 1
                if budget is not None:
                    budget -= 1
                if not rows:
                    break
            if done:
                journal.record_layer(snapshot_id, plan, "collected", pk_column)
                status = "collected"
            else:
                status = "collecting"
                summary["complete"] = False
            summary["layers"][plan.table] = {
                "status": status,
                "resumed_from_pk": progress["last_pk"] if progress else None,
                "collected_this_run": collected,
                "batches_this_run": batches,
            }
        return summary
    finally:
        reader.close()
        journal.close()


def export_collection(journal_path, out_path=None, snapshot_id=None):
    """导出一批记录，清单写明快照身份与被隔离的图层。"""
    journal = CollectionJournal(journal_path)
    try:
        if snapshot_id is None:
            snapshot_id = journal.latest_snapshot_id()
        manifest = journal.manifest(snapshot_id)
        journal.record_export(snapshot_id, manifest)
        if out_path is not None:
            Path(out_path).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest
    finally:
        journal.close()
