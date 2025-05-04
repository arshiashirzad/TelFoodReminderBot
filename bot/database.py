import mysql.connector
from datetime import datetime

class Database:
    def __init__(self, host, user, password, database):
        self.connection = mysql.connector.connect(
            host=host,
            user=user,
            password=password,
            database=database,
            charset='utf8mb4'
        )
        self._initialize_database()
    
    def _initialize_database(self):
        """Create reservations table if it doesn't exist."""
        cursor = self.connection.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reservations (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id BIGINT NOT NULL UNIQUE,
                reserved BOOLEAN DEFAULT 0,
                reservation_time DATETIME NULL
            )
        ''')
        self.connection.commit()

    def save_reservation(self, user_id):
        """Save a reservation for the user."""
        cursor = self.connection.cursor()
        query = '''
            INSERT INTO reservations (user_id, reserved, reservation_time)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE
                reserved = VALUES(reserved),
                reservation_time = VALUES(reservation_time)
        '''
        cursor.execute(query, (user_id, True, datetime.now()))
        self.connection.commit()

    def get_users_without_reservation(self):
        """Fetch users who haven't reserved."""
        cursor = self.connection.cursor(dictionary=True)
        query = '''
            SELECT user_id FROM reservations
            WHERE reserved = 0
        '''
        cursor.execute(query)
        users = cursor.fetchall()
        return users

    def reset_reservations(self):
        """Reset all reservations (useful for weekly schedule)."""
        cursor = self.connection.cursor()
        query = '''
            UPDATE reservations
            SET reserved = 0, reservation_time = NULL
        '''
        cursor.execute(query)
        self.connection.commit()

    def close_connection(self):
        """Close the database connection."""
        self.connection.close()
