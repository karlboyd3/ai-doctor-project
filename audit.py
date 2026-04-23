import sqlite3
import os
from datetime import datetime

_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'audit.db')

def _init():
    with sqlite3.connect(_DB) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS audit_log (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            ts     TEXT NOT NULL,
            user   TEXT,
            action TEXT,
            rtype  TEXT,
            rid    TEXT,
            ip     TEXT
        )""")

_init()

def log(action, rtype='', rid='', user=None, ip=None):
    try:
        from flask import session, request as freq
        u = user or session.get('staff_user', 'system')
        i = ip or (freq.remote_addr if freq else '')
    except Exception:
        u = user or 'system'
        i = ip or ''
    try:
        with sqlite3.connect(_DB) as c:
            c.execute(
                "INSERT INTO audit_log(ts,user,action,rtype,rid,ip) VALUES(?,?,?,?,?,?)",
                (datetime.now().isoformat(), u, action, rtype, str(rid), i)
            )
    except Exception:
        pass

def recent(n=200):
    try:
        with sqlite3.connect(_DB) as c:
            return c.execute(
                "SELECT ts,user,action,rtype,rid,ip FROM audit_log ORDER BY id DESC LIMIT ?",
                (n,)
            ).fetchall()
    except Exception:
        return []
