"""Hosted (multi-user) mode — per-session credentials, held in memory only.

Local mode stores one operator's token in the OS keychain and runs one process
client. When ``WORKBOX_HOSTED=1`` (a shared deploy — e.g. Kubernetes), there is
no keychain and no shared token: each user logs in with their own site + email +
API token, which lives **only in this process's memory**, keyed by a session
cookie. Nothing is written to disk, and the token is never sent back to a client.

A session caches its :class:`~core.client.WorkboxClient` so every request (and
its streaming response) reuses one connection pool; it is closed on logout or
when the session is swept for age. On pod restart all sessions are gone and users
simply log in again.

This module is inert unless the app enables it — importing it has no effect on
local keychain mode.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.auth import Credentials

if TYPE_CHECKING:
    from core.client import WorkboxClient

#: idle lifetime of a session before it is swept (and its client dropped)
SESSION_TTL = 12 * 3600

#: how many concurrent sessions to keep — a backstop, not a real limit for a
#: small team. Oldest-touched sessions are evicted past this.
_MAX_SESSIONS = 200


@dataclass
class Session:
    sid: str
    created_at: float
    touched_at: float
    creds: Credentials | None = None
    site_url: str = ""
    email: str = ""
    #: cached client built from creds (see module docstring). Not serialised.
    client: "WorkboxClient | None" = None

    @property
    def authed(self) -> bool:
        return self.creds is not None


class SessionStore:
    """In-memory session table with idle-TTL sweeping. Thread-safe."""

    def __init__(self, ttl: float = SESSION_TTL) -> None:
        self._d: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._ttl = ttl

    def _dead(self, now: float) -> list[Session]:
        expired = [s for s in self._d.values() if now - s.touched_at > self._ttl]
        if len(self._d) - len(expired) > _MAX_SESSIONS:
            live = sorted((s for s in self._d.values() if s not in expired),
                          key=lambda s: s.touched_at)
            expired += live[: len(self._d) - len(expired) - _MAX_SESSIONS]
        return expired

    def _evict(self, sessions: list[Session]) -> list[Session]:
        for s in sessions:
            self._d.pop(s.sid, None)
        return sessions

    def new(self) -> Session:
        now = time.time()
        sid = secrets.token_urlsafe(24)
        with self._lock:
            evicted = self._evict(self._dead(now))
            sess = Session(sid=sid, created_at=now, touched_at=now)
            self._d[sid] = sess
        _close_clients(evicted)
        return sess

    def get(self, sid: str) -> Session | None:
        if not sid:
            return None
        now = time.time()
        with self._lock:
            sess = self._d.get(sid)
            if sess is None:
                return None
            if now - sess.touched_at > self._ttl:
                self._d.pop(sid, None)
                dead = [sess]
                sess = None
            else:
                sess.touched_at = now
                dead = []
        _close_clients(dead)
        return sess

    def save(self, sess: Session) -> None:
        with self._lock:
            self._d[sess.sid] = sess

    def delete(self, sid: str) -> Session | None:
        with self._lock:
            sess = self._d.pop(sid, None)
        return sess

    def count(self) -> int:
        """Live (non-expired) session count, for monitoring."""
        now = time.time()
        with self._lock:
            return sum(1 for s in self._d.values() if now - s.touched_at <= self._ttl)


def _close_clients(sessions: list[Session]) -> None:
    """Best-effort close of swept sessions' clients. Called outside the lock; the
    aclose is scheduled on the running loop when there is one (we are always in
    async request context here), else the client is just dropped."""
    import asyncio

    for s in sessions:
        client = s.client
        s.client = None
        if client is None:
            continue
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(client.aclose())
        except RuntimeError:
            pass  # no loop — drop it; httpx frees the pool on GC
