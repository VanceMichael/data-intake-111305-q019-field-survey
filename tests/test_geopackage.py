import hashlib
import json
import math
import struct
import tempfile
import unittest
from pathlib import Path

from field_survey.collection import CollectionJournal, collect_package, export_collection
from field_survey.geopackage import SnapshotReader, classify_geometry
from field_survey.geopackage_fixture import TRACK_POINT_ROWS, build, point_geometry
from field_survey.showcase import run as showcase_run


def source_digest(path):
    digest = hashlib.sha256()
    for suffix in ("", "-wal"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            digest.update(candidate.read_bytes())
    return digest.hexdigest()


class GeometryParsingTests(unittest.TestCase):
    def test_missing_geometry_is_not_empty_geometry(self):
        self.assertEqual(classify_geometry(None), ("missing", {"reason": "geom_is_null"}))

    def test_coordinates_come_from_wkb_not_from_envelope(self):
        blob = (
            b"GP" + bytes([0, 3]) + struct.pack("<i", 4326)
            + struct.pack("<dddd", 0.0, 0.0, 10.0, 10.0)
            + struct.pack("<BIdd", 1, 1, 120.2, 30.1)
        )
        klass, details = classify_geometry(blob)
        self.assertEqual(klass, "ok")
        self.assertEqual(details["envelope"], [0.0, 0.0, 10.0, 10.0])
        self.assertEqual(details["wkb"]["coordinates"], [120.2, 30.1])

    def test_empty_flag_with_nan_point(self):
        blob = b"GP" + bytes([0, 0x11]) + struct.pack("<i", 4326) + struct.pack("<BIdd", 1, 1, math.nan, math.nan)
        klass, details = classify_geometry(blob)
        self.assertEqual(klass, "empty")
        self.assertNotIn("coordinates", details["wkb"])

    def test_empty_linestring_without_flag(self):
        blob = b"GP" + bytes([0, 1]) + struct.pack("<i", 4326) + struct.pack("<BII", 1, 2, 0)
        self.assertEqual(classify_geometry(blob)[0], "empty")

    def test_curve_type_is_unsupported(self):
        blob = b"GP" + bytes([0, 1]) + struct.pack("<i", 4326) + struct.pack("<BII", 1, 8, 0)
        klass, details = classify_geometry(blob)
        self.assertEqual((klass, details["reason"]), ("unsupported", "wkb_type_8"))

    def test_ewkb_srid_flag_is_unsupported(self):
        blob = b"GP" + bytes([0, 1]) + struct.pack("<i", 4326) + struct.pack("<BIidd", 1, 0x20000001, 4326, 1.0, 2.0)
        klass, details = classify_geometry(blob)
        self.assertEqual((klass, details["reason"]), ("unsupported", "ewkb_srid_flag"))

    def test_reserved_envelope_indicator_is_unsupported(self):
        blob = b"GP" + bytes([0, 0x0B]) + struct.pack("<i", 4326) + b"\x00" * 24
        klass, details = classify_geometry(blob)
        self.assertEqual((klass, details["reason"]), ("unsupported", "reserved_envelope_indicator"))

    def test_malformed_inputs(self):
        self.assertEqual(classify_geometry(b"XX" + b"\x00" * 20)[1]["reason"], "bad_magic")
        self.assertEqual(classify_geometry(b"GP\x00")[1]["reason"], "gpkg_header_short")
        short_point = b"GP" + bytes([0, 1]) + struct.pack("<i", 4326) + struct.pack("<BId", 1, 1, 1.0)
        self.assertEqual(classify_geometry(short_point)[0], "malformed")
        trailing = point_geometry(1.0, 2.0) + b"\x00"
        self.assertEqual(classify_geometry(trailing)[1]["reason"], "trailing_bytes")

    def test_big_endian_header_and_xyzm(self):
        blob = b"GP" + bytes([0, 0]) + struct.pack(">i", 4326) + struct.pack(">BIdddd", 0, 3001, 1.0, 2.0, 3.0, 4.0)
        klass, details = classify_geometry(blob)
        self.assertEqual(klass, "ok")
        self.assertEqual(details["srs_id"], 4326)
        self.assertEqual(details["wkb"]["dimensions"], "XYZM")
        self.assertEqual(details["wkb"]["coordinates"], [1.0, 2.0, 3.0, 4.0])


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.package = Path(self.temporary.name) / "survey.gpkg"
        self.writer = build(self.package)

    def tearDown(self):
        self.writer.close()
        self.temporary.cleanup()

    def test_committed_wal_content_is_part_of_the_snapshot(self):
        with self.writer:
            self.writer.execute("INSERT INTO points VALUES (?,?,?)", (70, "WAL提交", point_geometry(119.9, 29.9)))
        self.assertGreater(Path(str(self.package) + "-wal").stat().st_size, 32)
        reader = SnapshotReader(self.package)
        reader.begin()
        try:
            columns, rows = reader.fetch_batch("points", "fid", None, 100)
            names = [row[columns.index("name")] for row in rows]
            self.assertIn("WAL提交", names)
            self.assertGreater(reader.facts["wal_frames"], 0)
        finally:
            reader.close()

    def test_inflight_transaction_keeps_its_own_snapshot(self):
        reader = SnapshotReader(self.package)
        reader.begin()
        try:
            metadata = reader.identify()
            with self.writer:
                self.writer.execute(
                    "INSERT INTO track_points VALUES (?,?,?,?)", (991, 991, "迟到", point_geometry(1.0, 1.0))
                )
            _, rows = reader.fetch_batch("track_points", "fid", None, 100000)
            self.assertEqual(len(rows), TRACK_POINT_ROWS)
            self.assertNotIn("late_layer", [plan.table for plan in metadata.layers])
        finally:
            reader.close()
        fresh = SnapshotReader(self.package)
        fresh.begin()
        try:
            _, rows = fresh.fetch_batch("track_points", "fid", None, 100000)
            self.assertEqual(len(rows), TRACK_POINT_ROWS + 1)
            self.assertNotEqual(fresh.token, reader.token)
        finally:
            fresh.close()

    def test_metadata_and_records_share_one_snapshot(self):
        reader = SnapshotReader(self.package)
        reader.begin()
        try:
            metadata = reader.identify()
            names = [plan.table for plan in metadata.layers]
            with self.writer:
                self.writer.execute("INSERT INTO gpkg_geometry_columns VALUES ('late','geom','POINT',4326,0,0)")
            again = reader.identify()
            self.assertEqual(names, [plan.table for plan in again.layers])
        finally:
            reader.close()


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.package = Path(self.temporary.name) / "survey.gpkg"
        self.journal = Path(self.temporary.name) / "collection.sqlite3"
        self.writer = build(self.package)

    def tearDown(self):
        self.writer.close()
        self.temporary.cleanup()

    def collect(self, **kwargs):
        return collect_package(self.package, self.journal, **kwargs)

    def test_conflicted_layers_quarantined_consistent_layers_collected(self):
        result = self.collect()
        quarantined = {item["layer"]: item["reasons"] for item in result["quarantined"]}
        self.assertIn("srs_mismatch", quarantined["conflicted_layer"])
        self.assertIn("srs_unregistered", quarantined["unknown_layer"])
        self.assertIn("unsupported_geometry_type:CUSTOM", quarantined["unknown_layer"])
        self.assertIn("unsupported_extension:local_custom", quarantined["unknown_layer"])
        self.assertEqual(result["layers"]["points"]["status"], "collected")
        self.assertEqual(result["layers"]["track_points"]["status"], "collected")
        self.assertTrue(result["complete"])

    def test_records_preserve_package_layer_pk_and_raw_geometry(self):
        result = self.collect()
        journal = CollectionJournal(self.journal)
        try:
            row = journal.connection.execute(
                "SELECT * FROM records WHERE snapshot_id=? AND layer='points' AND pk=1", (result["snapshot_id"],)
            ).fetchone()
            source_blob = self.writer.execute("SELECT geom FROM points WHERE fid=1").fetchone()[0]
            self.assertEqual(bytes(row["geom"]), bytes(source_blob))
            self.assertEqual(row["geom_class"], "ok")
            summary = json.loads(row["geom_summary"])
            self.assertEqual(summary["wkb"]["coordinates"], [120.2, 30.1])
            self.assertEqual(json.loads(row["attributes"])["name"], "合成样点")
        finally:
            journal.close()

    def test_geometry_classes_distinguish_missing_empty_unsupported(self):
        result = self.collect()
        journal = CollectionJournal(self.journal)
        try:
            classes = dict(journal.connection.execute(
                "SELECT pk, geom_class FROM records WHERE snapshot_id=? AND layer='points'", (result["snapshot_id"],)
            ).fetchall())
            self.assertEqual(classes, {1: "ok", 2: "missing", 3: "empty", 4: "unsupported", 5: "unsupported", 6: "unsupported"})
            rows = journal.connection.execute(
                "SELECT geom_summary FROM records WHERE snapshot_id=? AND layer='points' AND geom_class != 'ok'",
                (result["snapshot_id"],),
            ).fetchall()
            for (text,) in rows:
                self.assertNotIn("coordinates", json.loads(text).get("wkb", {}))
        finally:
            journal.close()

    def test_resume_continues_only_within_the_same_snapshot(self):
        first = self.collect(batch_size=100, max_batches=2)
        self.assertFalse(first["complete"])
        self.assertEqual(first["layers"]["track_points"]["collected_this_run"], 100)
        second = self.collect(batch_size=100)
        self.assertEqual(second["snapshot_id"], first["snapshot_id"])
        self.assertFalse(second["new_snapshot"])
        self.assertEqual(second["layers"]["track_points"]["resumed_from_pk"], 100)
        self.assertTrue(second["complete"])
        journal = CollectionJournal(self.journal)
        try:
            count = journal.connection.execute(
                "SELECT count(*) FROM records WHERE snapshot_id=? AND layer='track_points'", (first["snapshot_id"],)
            ).fetchone()[0]
            self.assertEqual(count, TRACK_POINT_ROWS)
        finally:
            journal.close()

    def test_new_snapshot_does_not_inherit_offsets(self):
        first = self.collect(batch_size=100, max_batches=2)
        with self.writer:
            self.writer.execute(
                "INSERT INTO track_points VALUES (?,?,?,?)", (992, 992, "编辑新增", point_geometry(1.5, 1.5))
            )
        second = self.collect(batch_size=100)
        self.assertNotEqual(second["snapshot_id"], first["snapshot_id"])
        self.assertTrue(second["new_snapshot"])
        self.assertIsNone(second["layers"]["track_points"]["resumed_from_pk"])
        journal = CollectionJournal(self.journal)
        try:
            old_count = journal.connection.execute(
                "SELECT count(*) FROM records WHERE snapshot_id=? AND layer='track_points'", (first["snapshot_id"],)
            ).fetchone()[0]
            self.assertEqual(old_count, 100)
            new_rows = journal.connection.execute(
                "SELECT count(*), min(pk) FROM records WHERE snapshot_id=? AND layer='track_points'", (second["snapshot_id"],)
            ).fetchone()
            self.assertEqual(tuple(new_rows), (TRACK_POINT_ROWS + 1, 1))
            progress = journal.connection.execute("SELECT count(*) FROM progress").fetchone()[0]
            self.assertEqual(progress, 4)
        finally:
            journal.close()

    def test_source_files_are_not_modified(self):
        before = source_digest(self.package)
        self.collect()
        self.assertEqual(before, source_digest(self.package))

    def test_layer_selection_claims_only_selected_layers(self):
        result = self.collect(layers=["points"])
        self.assertEqual(list(result["layers"]), ["points"])
        journal = CollectionJournal(self.journal)
        try:
            status = journal.connection.execute(
                "SELECT status FROM layers WHERE snapshot_id=? AND table_name='track_points'", (result["snapshot_id"],)
            ).fetchone()[0]
            self.assertEqual(status, "not_selected")
            count = journal.connection.execute(
                "SELECT count(*) FROM records WHERE snapshot_id=? AND layer='track_points'", (result["snapshot_id"],)
            ).fetchone()[0]
            self.assertEqual(count, 0)
        finally:
            journal.close()

    def test_export_states_snapshot_and_quarantined_layers(self):
        result = self.collect()
        out = Path(self.temporary.name) / "export.json"
        manifest = export_collection(self.journal, out)
        self.assertEqual(manifest["snapshot"]["token"], result["snapshot_token"])
        self.assertGreaterEqual(manifest["snapshot"]["facts"]["wal_frames"], 1)
        self.assertEqual(
            {layer["table"] for layer in manifest["quarantined_layers"]},
            {"conflicted_layer", "unknown_layer"},
        )
        self.assertEqual(len(manifest["records"]), 6 + TRACK_POINT_ROWS)
        self.assertTrue(all(record["geometry_sha256"] or record["geometry_class"] == "missing" for record in manifest["records"]))
        written = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(written["snapshot"]["id"], manifest["snapshot"]["id"])
        journal = CollectionJournal(self.journal)
        try:
            self.assertEqual(journal.connection.execute("SELECT count(*) FROM exports").fetchone()[0], 1)
        finally:
            journal.close()


class ShowcaseTests(unittest.TestCase):
    def test_demo_narrative(self):
        with tempfile.TemporaryDirectory() as directory:
            narrative = showcase_run(directory)
        self.assertTrue(narrative["source_unmodified"])
        steps = {step["step"]: step for step in narrative["steps"]}
        self.assertLess(steps["edit_concurrency"]["pinned_snapshot_rows"], steps["edit_concurrency"]["fresh_snapshot_rows"])
        self.assertFalse(steps["partial_run_then_exit"]["complete"])
        self.assertTrue(steps["resume_same_snapshot"]["same_snapshot"])
        self.assertTrue(steps["recollect_new_snapshot"]["new_snapshot"])
        self.assertIsNone(steps["recollect_new_snapshot"]["resumed_from_pk"])
        self.assertGreaterEqual(len(narrative["snapshots"]), 2)
        self.assertEqual(
            {layer["table"] for layer in narrative["quarantined_layers"]},
            {"conflicted_layer", "unknown_layer"},
        )
        classes = steps["export"]["geometry_classes"]
        self.assertEqual(classes["missing"], 1)
        self.assertEqual(classes["empty"], 1)
        self.assertEqual(classes["unsupported"], 3)
        self.assertEqual(classes["ok"], TRACK_POINT_ROWS + 1 + 2)


if __name__ == "__main__":
    unittest.main()
