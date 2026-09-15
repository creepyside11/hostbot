"""Discover bot users from project SQLite databases without requiring a tracking API."""
import os
import sqlite3
import time
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from sqlite_inspector import discover_files, resolve_database, open_readonly

EXACT_TABLES = {'users','user','members','member','clients','client','subscribers','subscriber','customers','customer','accounts','account'}
ID_COLUMNS = ('telegram_user_id','telegram_id','tg_user_id','tg_id','user_id','chat_id','id')
USERNAME_COLUMNS = ('username','user_name','tg_username','login','email')
FIRST_NAME_COLUMNS = ('first_name','firstname','display_name','full_name','name')
LAST_NAME_COLUMNS = ('last_name','lastname','surname')
LANGUAGE_COLUMNS = ('language_code','language','lang')
FIRST_SEEN_COLUMNS = ('first_seen','created_at','created','registered_at','joined_at','registration_date')
LAST_SEEN_COLUMNS = ('last_seen','updated_at','updated','last_activity','last_active','seen_at')
INTERACTION_COLUMNS = ('interactions','messages_count','message_count','requests_count','uses_count','events_count')
MAX_USERS_PER_BOT = 10000
MAX_DATABASES_PER_BOT = 12


def pg_connect():
    return psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10, row_factory=dict_row,
                           options='-c statement_timeout=15000')


def qident(value):
    return '"' + str(value).replace('"', '""') + '"'


def pick(columns, names):
    lower = {str(column).lower(): str(column) for column in columns}
    for name in names:
        if name in lower:
            return lower[name]
    return None


def parse_user_id(value):
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or not text.lstrip('-').isdigit():
        return None
    try:
        number = int(text)
    except ValueError:
        return None
    return number if number != 0 and -(2**63) < number < 2**63-1 else None


def parse_time(value):
    if value is None or value == '':
        return None
    try:
        if isinstance(value, (int, float)) or str(value).replace('.', '', 1).isdigit():
            number = float(value)
            if number > 1_000_000_000_000:
                number /= 1000
            if 0 < number < 32_503_680_000:
                return datetime.fromtimestamp(number, tz=timezone.utc)
        text = str(value).strip().replace('Z', '+00:00')
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def candidate_tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    candidates = []
    for row in rows:
        name = str(row[0])
        columns = [str(item[1]) for item in conn.execute(f'PRAGMA table_info({qident(name)})').fetchall()]
        id_column = pick(columns, ID_COLUMNS)
        if not id_column:
            continue
        lower_name = name.lower()
        score = 0
        if lower_name in EXACT_TABLES:
            score += 12
        elif any(token in lower_name for token in ('user','member','client','subscriber','customer','account')):
            score += 6
        if id_column.lower() in ID_COLUMNS[:-1]:
            score += 8
        elif lower_name in EXACT_TABLES:
            score += 3
        if pick(columns, USERNAME_COLUMNS + FIRST_NAME_COLUMNS):
            score += 2
        if score >= 10:
            candidates.append((score, name, columns, id_column))
    candidates.sort(reverse=True, key=lambda item: item[0])
    return candidates


def scan_database(path, remaining=MAX_USERS_PER_BOT):
    found = {}
    with open_readonly(path) as conn:
        for _, table, columns, id_column in candidate_tables(conn):
            if len(found) >= remaining:
                break
            username = pick(columns, USERNAME_COLUMNS)
            first_name = pick(columns, FIRST_NAME_COLUMNS)
            last_name = pick(columns, LAST_NAME_COLUMNS)
            language = pick(columns, LANGUAGE_COLUMNS)
            first_seen = pick(columns, FIRST_SEEN_COLUMNS)
            last_seen = pick(columns, LAST_SEEN_COLUMNS)
            interactions = pick(columns, INTERACTION_COLUMNS)
            selected = [('uid', id_column), ('username', username), ('first_name', first_name), ('last_name', last_name),
                        ('language_code', language), ('first_seen', first_seen), ('last_seen', last_seen), ('interactions', interactions)]
            sql = ','.join(f'{qident(column)} AS {qident(alias)}' if column else f'NULL AS {qident(alias)}' for alias, column in selected)
            limit = min(remaining - len(found), MAX_USERS_PER_BOT)
            try:
                rows = conn.execute(f'SELECT {sql} FROM {qident(table)} WHERE {qident(id_column)} IS NOT NULL LIMIT ?', (limit,)).fetchall()
            except sqlite3.Error:
                continue
            for row in rows:
                user_id = parse_user_id(row['uid'])
                if user_id is None:
                    continue
                interactions_value = 1
                try:
                    interactions_value = max(1, min(int(row['interactions'] or 1), 2_147_483_647))
                except (TypeError, ValueError):
                    pass
                candidate = {
                    'telegram_user_id': user_id,
                    'username': str(row['username'])[:255] if row['username'] not in (None, '') else None,
                    'first_name': str(row['first_name'])[:255] if row['first_name'] not in (None, '') else None,
                    'last_name': str(row['last_name'])[:255] if row['last_name'] not in (None, '') else None,
                    'language_code': str(row['language_code'])[:32] if row['language_code'] not in (None, '') else None,
                    'first_seen': parse_time(row['first_seen']),
                    'last_seen': parse_time(row['last_seen']),
                    'interactions': interactions_value,
                }
                existing = found.get(user_id)
                if existing:
                    for key in ('username','first_name','last_name','language_code'):
                        if not existing.get(key) and candidate.get(key):
                            existing[key] = candidate[key]
                    times = [v for v in (existing.get('first_seen'), candidate.get('first_seen')) if v]
                    existing['first_seen'] = min(times) if times else None
                    times = [v for v in (existing.get('last_seen'), candidate.get('last_seen')) if v]
                    existing['last_seen'] = max(times) if times else None
                    existing['interactions'] = max(existing.get('interactions', 1), candidate['interactions'])
                else:
                    found[user_id] = candidate
    return list(found.values())


def scan_bot(bot_id):
    users = {}
    discovered = discover_files(bot_id).get('files', [])[:MAX_DATABASES_PER_BOT]
    for item in discovered:
        if len(users) >= MAX_USERS_PER_BOT:
            break
        try:
            path = resolve_database(bot_id, item['path'])
            rows = scan_database(path, MAX_USERS_PER_BOT - len(users))
        except (ValueError, RuntimeError, sqlite3.Error, OSError):
            continue
        for row in rows:
            users[row['telegram_user_id']] = row
    return list(users.values())


def ensure_schema():
    with pg_connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS bot_analytics_users (
            bot_id uuid NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
            telegram_user_id bigint NOT NULL,
            username text,
            first_name text,
            last_name text,
            language_code text,
            first_seen timestamptz NOT NULL DEFAULT now(),
            last_seen timestamptz NOT NULL DEFAULT now(),
            interactions bigint NOT NULL DEFAULT 1,
            PRIMARY KEY(bot_id,telegram_user_id)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS bot_analytics_first_seen ON bot_analytics_users(bot_id,first_seen DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS bot_analytics_last_seen ON bot_analytics_users(bot_id,last_seen DESC)")


def sync_bot(conn, bot_id, users):
    now = datetime.now(timezone.utc)
    for user in users:
        first = user.get('first_seen')
        last = user.get('last_seen')
        has_time = bool(first or last)
        insert_first = first or last or now
        insert_last = last or first or now
        conn.execute("""INSERT INTO bot_analytics_users(bot_id,telegram_user_id,username,first_name,last_name,language_code,first_seen,last_seen,interactions)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(bot_id,telegram_user_id) DO UPDATE SET
              username=COALESCE(EXCLUDED.username,bot_analytics_users.username),
              first_name=COALESCE(EXCLUDED.first_name,bot_analytics_users.first_name),
              last_name=COALESCE(EXCLUDED.last_name,bot_analytics_users.last_name),
              language_code=COALESCE(EXCLUDED.language_code,bot_analytics_users.language_code),
              first_seen=CASE WHEN %s THEN LEAST(bot_analytics_users.first_seen,EXCLUDED.first_seen) ELSE bot_analytics_users.first_seen END,
              last_seen=CASE WHEN %s THEN GREATEST(bot_analytics_users.last_seen,EXCLUDED.last_seen) ELSE bot_analytics_users.last_seen END,
              interactions=GREATEST(bot_analytics_users.interactions,EXCLUDED.interactions)""",
            (bot_id, user['telegram_user_id'], user.get('username'), user.get('first_name'), user.get('last_name'),
             user.get('language_code'), insert_first, insert_last, user.get('interactions', 1), has_time, has_time))


def sync_once():
    ensure_schema()
    with pg_connect() as conn:
        bots = conn.execute("SELECT id FROM bots WHERE desired!='deleted'").fetchall()
    total = 0
    for bot in bots:
        try:
            users = scan_bot(str(bot['id']))
            if not users:
                continue
            with pg_connect() as conn:
                sync_bot(conn, bot['id'], users)
            total += len(users)
        except Exception:
            continue
    return total


def watch_forever():
    while True:
        try:
            sync_once()
        except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
            pass
        except Exception:
            print('SQLite users: временная ошибка, повтор через 60 секунд.', flush=True)
        time.sleep(60)
