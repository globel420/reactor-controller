import unittest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from supervisory.app.database import Base, ErrorLog
from supervisory.app.crud import create_error_log, resolve_error_log, get_error_logs

class TestErrorLogging(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()

    def test_create_and_resolve_error_log(self):
        # 1. Create error log
        log1 = create_error_log(self.db, "Liquid Reactor TC", "MAX31855 Open Circuit (0x01)")
        self.assertIsNotNone(log1.id)
        self.assertEqual(log1.sensor, "Liquid Reactor TC")
        self.assertEqual(log1.exact_error, "MAX31855 Open Circuit (0x01)")
        self.assertIsNone(log1.cleared_timestamp)

        # 2. Fetch logs
        logs = get_error_logs(self.db)
        self.assertEqual(len(logs), 1)
        self.assertTrue(logs[0]["active"])
        self.assertIsNone(logs[0]["cleared_timestamp"])

        # 3. Resolve error log
        resolve_error_log(self.db, log1.id)
        
        # 4. Verify cleared timestamp updated
        logs_after = get_error_logs(self.db)
        self.assertEqual(len(logs_after), 1)
        self.assertFalse(logs_after[0]["active"])
        self.assertIsNotNone(logs_after[0]["cleared_timestamp"])
