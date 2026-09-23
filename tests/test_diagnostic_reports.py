import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services import diagnostic_reports as reports


class DiagnosticReportsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "reports.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def accept(self, report_id="run-error", user="one"):
        return reports.accept({"report_id": report_id, "message": "stack", "reason": "native_crash"},
                              {"x-user-id": user, "Authorization": "must not persist"}, "now", 40, self.path)

    def test_duplicate_accept_is_durable_and_credentials_are_not_saved(self):
        self.accept(); self.accept()
        with reports._connect(self.path) as db:
            rows = db.execute("SELECT body FROM reports").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("must not persist", rows[0][0])
        self.assertEqual(self.accept()["delivery"], "pending")

    def test_ids_are_scoped_by_origin(self):
        self.accept(user="one"); self.accept(user="two")
        with reports._connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 2)

    def test_success_is_not_delivered_twice(self):
        self.accept()
        sender = AsyncMock(return_value=True)
        with patch.object(reports.feishu_alerts, "send_error_log_alert", sender):
            self.assertTrue(asyncio.run(reports.deliver_one(self.path)))
            self.assertFalse(asyncio.run(reports.deliver_one(self.path)))
        self.assertEqual(sender.await_count, 1)
        self.assertEqual(self.accept()["delivery"], "sent")

    def test_failure_remains_pending_and_recovers(self):
        self.accept()
        with patch.object(reports.feishu_alerts, "send_error_log_alert", AsyncMock(return_value=False)):
            asyncio.run(reports.deliver_one(self.path))
        self.assertEqual(self.accept()["delivery"], "pending")
        with reports._connect(self.path) as db:
            row = db.execute("SELECT due,attempts FROM reports").fetchone()
            self.assertGreater(row[0], time.time()); self.assertEqual(row[1], 1)
            db.execute("UPDATE reports SET due=0")
        with patch.object(reports.feishu_alerts, "send_error_log_alert", AsyncMock(return_value=True)):
            asyncio.run(reports.deliver_one(self.path))
        self.assertEqual(self.accept()["delivery"], "sent")

    def test_lease_prevents_concurrent_claim_and_survives_worker_death(self):
        self.accept()
        first = reports.claim(self.path, 1)
        self.assertIsNotNone(first)
        self.assertIsNone(reports.claim(self.path, 2))
        self.assertEqual(reports.claim(self.path, 302)[0], first[0])

    def test_invalid_or_oversized_report_is_rejected(self):
        with self.assertRaises(ValueError): self.accept("")
        with self.assertRaises(ValueError):
            reports.accept({"report_id": "x", "message": "x" * 140000}, {}, "now", 140000, self.path)


if __name__ == "__main__":
    unittest.main()
