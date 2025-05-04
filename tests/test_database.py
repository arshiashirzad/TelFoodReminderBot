import unittest
import sqlite3
from bot.database import Database

class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.db = Database('test.db')

    def test_save_reservation(self):
        self.db.save_reservation(12345)
        conn = sqlite3.connect('test.db')
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM reservations WHERE user_id = 12345")
        result = cursor.fetchone()
        conn.close()
        self.assertIsNotNone(result)
