import unittest
from unittest.mock import patch

import classify_rerun as triage


class CollectTest(unittest.TestCase):
    def collect(self, rows, details):
        with patch.object(triage, "get_launch", return_value={"name": "Rerun", "closed": True}), patch.object(
            triage, "scan_flat", return_value=(rows, {"complete": True})
        ), patch.object(triage, "get_result", side_effect=details.__getitem__):
            return triage.collect(42)

    def test_trace_and_message_are_from_one_result(self):
        rows = [
            {"id": 1, "name": "12.34 Example", "status": "failed"},
            {"id": 2, "name": "12.34 Example", "status": "failed"},
        ]
        result = self.collect(rows, {1: {"message": "other failure"}, 2: {"trace": "trace evidence"}})
        item = result["items"][0]
        self.assertEqual((item["message"], item["trace"]), ("", "trace evidence"))
        self.assertEqual(item["evidence_result_id"], 2)
        self.assertTrue(result["stats"]["gate_ok"])

    def test_unmatched_nonpass_rows_fail_gate(self):
        rows = [
            {"id": 1, "name": "Without number", "status": "failed"},
            {"id": 2, "name": "Passing case", "status": "passed"},
        ]
        result = self.collect(rows, {})
        self.assertEqual(result["stats"]["failed_rows"], 1)
        self.assertEqual(result["unmatched_rows"], [{"result_id": 1, "status": "failed"}])
        self.assertFalse(result["stats"]["gate_ok"])


if __name__ == "__main__":
    unittest.main()
