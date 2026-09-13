"""Shared PostgreSQL connection helpers for the Emerald worker."""
import os
import socket

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row


def _params():
    value = os.environ.get('DATABASE_URL', '')
    if not value:
        raise RuntimeError('DATABASE_URL не задан у исполнителя.')
    return conninfo_to_dict(value)


def endpoint():
    try:
        params = _params()
        host = str(params.get('host') or 'localhost').split(',')[0]
        port = str(params.get('port') or '5432').split(',')[0]
        return host, port
    except Exception:
        return 'unknown', '5432'


def validate_worker_database_url():
    host, _ = endpoint()
    if '-pooler.' in host.lower() and host.lower().endswith('.neon.tech'):
        raise RuntimeError(
            'DATABASE_URL исполнителя указывает на Neon pooled endpoint (-pooler). '
            'Для worker нужен Direct connection из Neon, потому что исполнитель держит session advisory lock.'
        )


def connection_string():
    value = os.environ['DATABASE_URL']
    params = conninfo_to_dict(value)
    host = str(params.get('host') or '').lower()
    if host.endswith('.neon.tech') and not params.get('sslmode'):
        return make_conninfo(value, sslmode='require')
    return value


def connect():
    validate_worker_database_url()
    return psycopg.connect(
        connection_string(),
        connect_timeout=10,
        row_factory=dict_row,
        options='-c statement_timeout=15000',
    )


def operational_error_summary(error):
    host, port = endpoint()
    text = str(error).lower()
    if 'password authentication failed' in text or 'authentication failed' in text:
        reason = 'ошибка авторизации'
    elif 'could not translate host name' in text or 'name or service not known' in text or 'nodename nor servname' in text:
        reason = 'ошибка DNS'
    elif 'connection refused' in text:
        reason = 'соединение отклонено'
    elif 'network is unreachable' in text or 'no route to host' in text:
        reason = 'сеть недоступна'
    elif 'timeout' in text or 'timed out' in text:
        reason = 'таймаут подключения'
    elif 'ssl' in text or 'certificate' in text:
        reason = 'ошибка SSL/TLS'
    elif isinstance(error, socket.gaierror):
        reason = 'ошибка DNS'
    else:
        reason = 'ошибка подключения PostgreSQL'
    return f'PostgreSQL {host}:{port}: {reason}.'
