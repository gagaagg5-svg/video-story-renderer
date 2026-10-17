import unittest
from duration_utils import parse_duration, format_duration, duration_from_interval

class DurationTests(unittest.TestCase):
    def test_seconds(self):
        for text in ['480', '480.5', '480,5']:
            self.assertAlmostEqual(parse_duration(text), float(text.replace(',', '.')))
    def test_minutes(self):
        self.assertEqual(parse_duration('08:00'), 480)
    def test_hours(self):
        self.assertEqual(parse_duration('01:08:15'), 4095)
    def test_fractional(self):
        self.assertEqual(parse_duration('01:02:03,5'), 3723.5)
    def test_invalid(self):
        for text in ['', '0', '-1', 'NaN', 'inf', '1:60', '60:00', '1:00:00:00', '1.5:00']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_duration(text)
    def test_millisecond_carry(self):
        self.assertEqual(format_duration(59.9999), '00:01:00')
        self.assertEqual(format_duration(3723.5), '01:02:03.5')
        for n in [0.001, 0.5, 59.999, 3600.123]:
            self.assertAlmostEqual(parse_duration(format_duration(n)), n, places=3)

    def test_csv_start_end_interval(self):
        self.assertEqual(duration_from_interval('00:00:00', '00:00:35'), 35)
        self.assertEqual(duration_from_interval('00:08:00', '00:16:30'), 510)
        with self.assertRaises(ValueError):
            duration_from_interval('00:10:00', '00:09:59')

if __name__ == '__main__':
    unittest.main()
