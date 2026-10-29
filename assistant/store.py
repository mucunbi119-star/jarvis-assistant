"""会話と長期記憶を分けて保存する。秘密情報は保存しない。"""
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import DB_PATH

DEFAULTS = dict(assistant_name='ルミナ', user_name='', tone='polite', answer_length='standard',
                timezone='Asia/Tokyo', mode='demo', voice_uri='', speech_rate=1.0,
                speech_volume=0.8, speech_enabled=False, onboarding_complete=False,
                local_model=os.environ.get('OLLAMA_MODEL', 'qwen3:4b'))


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY,title TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY,conversation_id TEXT,role TEXT,
          content TEXT,sources TEXT,demo INTEGER,created_at TEXT);
        CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY,content TEXT,source TEXT,
          created_at TEXT,updated_at TEXT);
        CREATE INDEX IF NOT EXISTS conversation_messages ON messages(conversation_id,created_at);
        ''')


def get_settings():
    with connect() as db:
        row = db.execute('SELECT value FROM settings WHERE id=1').fetchone()
    return DEFAULTS | (json.loads(row['value']) if row else {})


def save_settings(values):
    settings = get_settings() | values
    with connect() as db:
        db.execute('INSERT OR REPLACE INTO settings VALUES(1,?)', (json.dumps(settings),))
    return settings


def conversations():
    with connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM conversations ORDER BY created_at DESC')]


def new_conversation():
    item = dict(id=uid(), title='新しい会話', created_at=now())
    with connect() as db:
        db.execute('INSERT INTO conversations VALUES(:id,:title,:created_at)', item)
    return item


def conversation(cid):
    with connect() as db:
        item = db.execute('SELECT * FROM conversations WHERE id=?', (cid,)).fetchone()
        if not item:
            return None
        messages = [dict(r) for r in db.execute('SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at,rowid', (cid,))]
    for msg in messages:
        msg['sources'] = json.loads(msg['sources'])
        msg['demo'] = bool(msg['demo'])
    return dict(item) | {'messages': messages}


def add_message(cid, role, content, sources=None, demo=False):
    msg = dict(id=uid(), conversation_id=cid, role=role, content=content,
               sources=sources or [], demo=demo, created_at=now())
    with connect() as db:
        db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?)',
                   (msg['id'], cid, role, content, json.dumps(msg['sources']), int(demo), msg['created_at']))
        if role == 'user':
            db.execute("UPDATE conversations SET title=? WHERE id=? AND title='新しい会話'", (content[:35], cid))
    return msg


def memories():
    with connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM memories ORDER BY updated_at DESC')]


def contains_secret(text):
    # Avoid storing even a labelled password or code. This is conservative, not a DLP guarantee.
    return bool(re.search(r'(?i)(パスワード|暗証番号|認証コード|API[ _-]*キー|password|secret|token|api[ _-]*key|sk-[A-Za-z0-9_-]{8,}|\b\d{6}\b)', text))


def save_memory(content, source='手動入力', memory_id=None):
    if not content.strip():
        raise ValueError('記憶する内容を入力してください。')
    if contains_secret(content) or contains_secret(source):
        raise ValueError('パスワード・認証コード・APIキーを含む可能性があるため記憶できません。')
    item = dict(id=memory_id or uid(), content=content.strip(), source=source[:100], created_at=now(), updated_at=now())
    with connect() as db:
        if memory_id:
            old = db.execute('SELECT created_at FROM memories WHERE id=?', (memory_id,)).fetchone()
            if not old:
                raise KeyError(memory_id)
            item['created_at'] = old['created_at']
            db.execute('UPDATE memories SET content=:content,source=:source,updated_at=:updated_at WHERE id=:id', item)
        else:
            db.execute('INSERT INTO memories VALUES(:id,:content,:source,:created_at,:updated_at)', item)
    return item
