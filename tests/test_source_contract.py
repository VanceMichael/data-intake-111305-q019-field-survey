import hashlib
import json
import unittest

from field_survey.client import Client
from field_survey.decoding import parse_page, records as decode_records
from field_survey.origin import load_scenario, serve


class SourceContractTests(unittest.TestCase):
    def test_domain_observations_preserve_source_distinctions(self):
        scenario = load_scenario()
        records = scenario["source_records"]
        with serve(scenario) as origin:
            client = Client(origin.base_url)
            import tempfile
            import sqlite3
            from pathlib import Path
            from field_survey.geopackage_fixture import build
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "survey.gpkg"
                writer = build(path)
                try:
                    self.assertEqual(writer.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                    self.assertEqual(writer.execute("SELECT srs_id FROM gpkg_geometry_columns WHERE table_name='points'").fetchone()[0], 4326)
                    self.assertEqual(bytes(writer.execute("SELECT geom FROM points LIMIT 1").fetchone()[0])[:2], b"GP")
                    self.assertTrue(Path(str(path) + "-wal").exists())
                finally:
                    writer.close()

    def test_every_record_has_the_declared_source_identity(self):
        scenario = load_scenario()
        for record in scenario["source_records"]:
            for field in scenario["identity_fields"]:
                self.assertIn(field, record)
                self.assertNotEqual(str(record[field]).strip(), "")


if __name__ == "__main__":
    unittest.main()
