from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RatePolicy:
    max_requests: int
    window_seconds: float
    min_interval_seconds: float


@dataclass(frozen=True, slots=True)
class RateReservation:
    allowed: bool
    wait_seconds: float
    window_count: int
    reserved_at: float | None


class DurableRateLimiter:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.connection = connection
        self.clock = clock
        self.sleeper = sleeper

    def reserve(self, provider: str, bucket: str, policy: RatePolicy) -> RateReservation:
        now = self.clock()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            block = self.connection.execute(
                "SELECT blocked_until FROM provider_blocks WHERE provider=? AND bucket=?",
                (provider, bucket),
            ).fetchone()
            blocked_until = float(block[0]) if block else 0.0
            rows = self.connection.execute(
                "SELECT reserved_at FROM provider_rate_reservations "
                "WHERE provider=? AND bucket=? AND reserved_at>? ORDER BY reserved_at",
                (provider, bucket, now - policy.window_seconds),
            ).fetchall()
            timestamps = [float(row[0]) for row in rows]
            waits = [max(0.0, blocked_until - now)]
            if timestamps:
                waits.append(max(0.0, timestamps[-1] + policy.min_interval_seconds - now))
            if len(timestamps) >= policy.max_requests:
                waits.append(max(0.0, timestamps[0] + policy.window_seconds - now))
            wait = max(waits)
            if wait > 0:
                self.connection.rollback()
                return RateReservation(False, wait, len(timestamps), None)
            self.connection.execute(
                "INSERT INTO provider_rate_reservations(provider, bucket, reserved_at) "
                "VALUES (?, ?, ?)",
                (provider, bucket, now),
            )
            self.connection.commit()
            return RateReservation(True, 0.0, len(timestamps) + 1, now)
        except Exception:
            self.connection.rollback()
            raise

    def wait_and_reserve(self, provider: str, bucket: str, policy: RatePolicy) -> None:
        while True:
            reservation = self.reserve(provider, bucket, policy)
            if reservation.allowed:
                return
            self.sleeper(reservation.wait_seconds)

    def block(self, provider: str, bucket: str, seconds: float, reason: str) -> None:
        until = self.clock() + max(0.0, seconds)
        self.connection.execute(
            """
            INSERT INTO provider_blocks(provider, bucket, blocked_until, reason)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(provider, bucket) DO UPDATE SET
              blocked_until=max(provider_blocks.blocked_until, excluded.blocked_until),
              reason=excluded.reason
            """,
            (provider, bucket, until, reason),
        )
        self.connection.commit()

    def state(self, provider: str, bucket: str, policy: RatePolicy) -> tuple[int, float]:
        now = self.clock()
        count = self.connection.execute(
            "SELECT count(*) FROM provider_rate_reservations "
            "WHERE provider=? AND bucket=? AND reserved_at>?",
            (provider, bucket, now - policy.window_seconds),
        ).fetchone()[0]
        block = self.connection.execute(
            "SELECT blocked_until FROM provider_blocks WHERE provider=? AND bucket=?",
            (provider, bucket),
        ).fetchone()
        return int(count), max(0.0, float(block[0]) - now) if block else 0.0
