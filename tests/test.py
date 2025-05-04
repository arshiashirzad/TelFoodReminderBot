import unittest
from bot.database import Database

class TestBot(unittest.TestCase):
    def test_reservation(self):
        db = Database('test.db')
        db.save_reservation(12345)
        rows = db.fetch_reservations()
        self.assertEqual(len(rows), 1)  # Check reservations are saved
