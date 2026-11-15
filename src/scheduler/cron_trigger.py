"""A CronTrigger whose next fire time never goes backwards (#3940).

APScheduler's job loop asks the trigger for the next fire time after the
previous one, and keeps asking while the answer is not in the future. If the
trigger ever answers with a time at or before the previous fire time, the loop
never advances: the scheduler pins a core and grows without bound. APScheduler
3.11.0 did exactly that at every DST fall-back for minute-stepped crons with
`hour='*'` in a zone leaving summer time, and took down two hosts on
2026-10-25. The pinned version is fixed (3.11.1+); this guard is so that a
future regression of the same shape degrades one schedule's timing instead of
the host.

Every comparison and every addition here is done on UTC instants, never on
the zone-local datetimes the trigger returns. Two datetimes that share a
tzinfo object compare by wall clock, ignoring their offsets, so inside the
repeated hour `01:30+01:00` (00:30Z) compares as LATER than `01:00+00:00`
(01:00Z). And local arithmetic resets `fold`: the second 01:00 plus a minute
is the FIRST 01:01, an hour earlier. Either mistake reintroduces the bug.

Standalone on purpose: it imports nothing from the scheduler package, so the
unit suite can exercise it without Redis or a database.
"""
import logging
from datetime import timedelta, timezone

from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# The last resort, when even the retry below goes backwards: fire a minute after
# the previous run rather than spin. That is one extra, mistimed run of the
# user's job, so it is only reached if the retry fails too.
_CLAMP = timedelta(minutes=1)


def _utc(dt):
    return dt.astimezone(timezone.utc)


class MonotonicCronTrigger(CronTrigger):
    """`CronTrigger`, except a next fire time is always after the previous one.

    An answer that is not after `previous_fire_time` (compared as instants) is
    retried once, asking the parent for the first fire time after
    `previous_fire_time + 1s` given in UTC — on 3.11.0 that alone yields the
    correct schedule at every fold. If the retry is not after the previous fire
    time either, the answer is `previous_fire_time + 1 minute`, computed in UTC
    and returned in the trigger's zone. Logged once per trigger. `None` from the
    parent (no more fire times, e.g. past `end_date`) passes through unchanged;
    a real fire time is never turned into `None`, which APScheduler would read
    as "remove this job".
    """

    def get_next_fire_time(self, previous_fire_time, now):
        nxt = super().get_next_fire_time(previous_fire_time, now)
        if nxt is None or previous_fire_time is None:
            return nxt
        if _utc(nxt) > _utc(previous_fire_time):
            return nxt

        retry = super().get_next_fire_time(None, _utc(previous_fire_time) + timedelta(seconds=1))
        if retry is not None and _utc(retry) > _utc(previous_fire_time):
            fixed = retry
        else:
            fixed = (_utc(previous_fire_time) + _CLAMP).astimezone(self.timezone)
        if not getattr(self, "_warned_backwards", False):
            self._warned_backwards = True
            logger.warning(
                "[#3940] CronTrigger answered %s after %s (tz=%s, %s); firing at %s "
                "instead so the job loop advances. APScheduler regression at a DST "
                "transition?",
                _utc(nxt).isoformat(), _utc(previous_fire_time).isoformat(), self.timezone,
                " ".join(f"{f.name}={f}" for f in self.fields if not f.is_default),
                _utc(fixed).isoformat(),
            )
        return fixed
