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
            self.assertEqual(records[0]["package_id"], records[1]["package_id"])
            self.assertIsNone(records[1]["crs"])
            for member in records[0]["manifest"]:
                self.assertEqual(client.fetch("/packages/PK-21/" + member).status, 200)
            transforms = json.loads(client.fetch("/transforms").body)
            self.assertNotEqual(transforms["shift-v1"], transforms["shift-v2"])

    def test_every_record_has_the_declared_source_identity(self):
        scenario = load_scenario()
        for record in scenario["source_records"]:
            for field in scenario["identity_fields"]:
                self.assertIn(field, record)
                self.assertNotEqual(str(record[field]).strip(), "")


if __name__ == "__main__":
    unittest.main()
