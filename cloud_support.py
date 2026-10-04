"""Invite-only access and durable event storage for the cloud pilot."""
import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st


def setting(name, default=None):
    try:
        return st.secrets.get(name, os.environ.get(name, default))
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return os.environ.get(name, default)


def resolve_participant(code, registry):
    # Namespace derives from stable alias, so rotating a code keeps the same history.
    if not code:
        return None
    for alias, expected in registry.items():
        if not isinstance(expected, str) or len(expected) < 16:
            continue
        if hmac.compare_digest(code.encode('utf-8'), expected.encode('utf-8')):
            profile = hashlib.sha256(('writeloop-participant:' + alias).encode('utf-8')).hexdigest()
            return profile, alias
    return None


def require_participant():
    registry = setting('participants', {})
    if not registry:
        st.info('试用入口尚未配置，请项目负责人在后台添加个人试用码。')
        st.stop()
    # Revalidate on every rerun: a removed or rotated credential loses access.
    identity = resolve_participant(st.session_state.get('_invite', ''), registry)
    if identity:
        return identity
    st.session_state.pop('_invite', None)
    st.markdown('### 进入我的学习档案')
    st.caption('输入项目负责人单独发给你的试用码。每人一个，请保存好。')
    with st.form('participant_login'):
        code = st.text_input('个人试用码', type='password', key='invite_input')
        consent = st.checkbox('我同意作文及相关学习记录发送至 DeepSeek 分析，并保存在项目云端数据库；项目负责人可管理记录。')
        submit = st.form_submit_button('进入学习档案')
    if submit:
        wait_until = st.session_state.get('_login_retry_at', 0)
        if time.monotonic() < wait_until:
            st.warning('请稍等几秒后再试。')
        elif not consent:
            st.warning('请先确认试用数据的处理方式。')
        else:
            identity = resolve_participant(code.strip(), registry)
            if identity:
                # Clear all previous widgets/reports before switching identity.
                st.session_state.clear()
                st.session_state['_invite'] = code.strip()
                st.rerun()
            st.session_state['_login_retry_at'] = time.monotonic() + 3
            st.error('试用码不正确，请核对项目负责人发给你的代码。')
    st.stop()


class CloudStore:
    """Fresh per-operation connection; never shares a participant's cached results."""
    def __init__(self):
        self.config = dict(setting('database', {}))
        self.test_path = os.environ.get('WRITELOOP_TEST_SQLITE', '')
        if not self.config and not self.test_path:
            raise RuntimeError('Persistent database is required')
        if self.test_path:
            # Explicit offline-test switch; not a production fallback.
            Path(self.test_path).parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.test_path)) as db, db:
                db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, profile TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL, created TEXT NOT NULL, body TEXT NOT NULL)')
        else:
            with self._connect() as db:
                db.execute('CREATE SCHEMA IF NOT EXISTS writeloop')
                db.execute('CREATE TABLE IF NOT EXISTS writeloop.events (seq BIGSERIAL PRIMARY KEY, id TEXT UNIQUE NOT NULL, profile TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL, created TEXT NOT NULL, body TEXT NOT NULL)')
                db.execute('CREATE INDEX IF NOT EXISTS events_profile_source ON writeloop.events (profile, source, seq)')

    def _connect(self):
        import psycopg
        allowed = {'host', 'port', 'dbname', 'user', 'password', 'sslmode', 'sslrootcert'}
        options = {k: v for k, v in self.config.items() if k in allowed}
        options.setdefault('sslmode', 'require')
        return psycopg.connect(**options, connect_timeout=15, prepare_threshold=None,
                               options='-c statement_timeout=20000')

    def save(self, profile, source, kind, body):
        event = {'id': uuid.uuid4().hex, 'profile': profile, 'source': source,
                 'kind': kind, 'created': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'body': body}
        values = (event['id'], profile, source, kind, event['created'], json.dumps(body, ensure_ascii=False))
        if self.test_path:
            with closing(sqlite3.connect(self.test_path)) as db, db:
                db.execute('INSERT INTO events (id,profile,source,kind,created,body) VALUES (?,?,?,?,?,?)', values)
        else:
            with self._connect() as db:
                db.execute('INSERT INTO writeloop.events (id,profile,source,kind,created,body) VALUES (%s,%s,%s,%s,%s,%s)', values)
        return event

    def list(self, profile, source):
        if self.test_path:
            with closing(sqlite3.connect(self.test_path)) as db, db:
                rows = db.execute('SELECT id,kind,created,body FROM events WHERE profile=? AND source=? ORDER BY created,seq', (profile, source)).fetchall()
        else:
            with self._connect() as db:
                rows = db.execute('SELECT id,kind,created,body FROM writeloop.events WHERE profile=%s AND source=%s ORDER BY created,seq', (profile, source)).fetchall()
        return [{'id': i, 'kind': k, 'created': t, 'body': json.loads(b)} for i, k, t, b in rows]
