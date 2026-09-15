import sqlite3
import tempfile
import unittest
from pathlib import Path

from user_discovery import candidate_tables, scan_database


class UserDiscoveryTests(unittest.TestCase):
    def test_detects_users_table_without_tracking_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'bot.db'
            conn=sqlite3.connect(path)
            conn.execute('CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, created_at TEXT)')
            conn.execute("INSERT INTO users(id,username,first_name,created_at) VALUES(123456789,'ivan','Ivan','2026-09-01T10:00:00Z')")
            conn.commit();conn.close()
            users=scan_database(path)
            self.assertEqual(len(users),1)
            self.assertEqual(users[0]['telegram_user_id'],123456789)
            self.assertEqual(users[0]['username'],'ivan')
            self.assertEqual(users[0]['first_name'],'Ivan')
            self.assertIsNotNone(users[0]['first_seen'])

    def test_detects_bot_users_with_user_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'data.sqlite'
            conn=sqlite3.connect(path)
            conn.execute('CREATE TABLE bot_users(user_id INTEGER, email TEXT, updated_at INTEGER)')
            conn.execute('INSERT INTO bot_users(user_id,email,updated_at) VALUES(42,\'u@example.com\',1700000000)')
            conn.commit();conn.close()
            users=scan_database(path)
            self.assertEqual(users[0]['telegram_user_id'],42)
            self.assertEqual(users[0]['username'],'u@example.com')
            self.assertIsNotNone(users[0]['last_seen'])

    def test_ignores_unrelated_generic_id_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'data.db'
            conn=sqlite3.connect(path)
            conn.execute('CREATE TABLE orders(id INTEGER PRIMARY KEY, name TEXT)')
            conn.execute("INSERT INTO orders(name) VALUES('test')")
            self.assertEqual(candidate_tables(conn),[])
            conn.close()

if __name__=='__main__': unittest.main()
