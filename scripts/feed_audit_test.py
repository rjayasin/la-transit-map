"""Check coverage uses real service IDs and GTFS calendar exceptions."""
from datetime import date
import unittest
from feed_audit import coverage


class CoverageTests(unittest.TestCase):
    def report(self, extra=None):
        files = {
            'trips.txt': [{'service_id': 'weekday'}, {'service_id': 'special'}],
            'calendar.txt': [dict(service_id='weekday', start_date='20260101',
                                  end_date='20260930', thursday='1'),
                             dict(service_id='unused', start_date='20000101',
                                  end_date='20991231', thursday='1')],
            'calendar_dates.txt': [],
        }
        files.update(extra or {})
        return coverage(lambda name: files.get(name, []), date(2026, 9, 24))

    def test_unused_calendar_does_not_hide_expiration(self):
        row = self.report({'calendar.txt': [dict(service_id='weekday', start_date='20260101',
                          end_date='20260801', thursday='1'),
                          dict(service_id='unused', start_date='20000101', end_date='20991231')]})
        self.assertTrue(row['expired'])
        self.assertEqual(row['tripsOnDate'], 0)

    def test_exception_removes_regular_service_and_adds_special_service(self):
        row = self.report({'calendar_dates.txt': [
            dict(service_id='weekday', date='20260924', exception_type='2'),
            dict(service_id='special', date='20260924', exception_type='1')]})
        self.assertEqual(row['tripsOnDate'], 1)
        self.assertFalse(row['expired'])

    def test_exception_only_feed(self):
        row = self.report({'calendar.txt': [], 'calendar_dates.txt': [
            dict(service_id='special', date='20260924', exception_type='1')]})
        self.assertEqual(row['start'], '20260924')
        self.assertEqual(row['end'], '20260924')
        self.assertEqual(row['tripsOnDate'], 1)

    def test_missing_calendar_is_not_coverage(self):
        row = self.report({'calendar.txt': []})
        self.assertTrue(row['missingCoverage'])
        self.assertIsNone(row['end'])

    def test_day_without_service_does_not_mean_expired(self):
        row = self.report({'calendar_dates.txt': [
            dict(service_id='weekday', date='20260924', exception_type='2')]})
        self.assertEqual(row['tripsOnDate'], 0)
        self.assertFalse(row['expired'])


if __name__ == '__main__':
    unittest.main()
