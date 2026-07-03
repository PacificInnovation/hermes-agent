"""Tests for repeat-failure notification dedup (cron/jobs.py + cron/scheduler.py).

A cron job that keeps failing with the same error (e.g. expired provider
login) must notify once, stay quiet for the re-notify interval, and post a
recovery notice when it starts working again — instead of spamming the
origin chat on every tick.
"""

from datetime import timedelta

import pytest

from cron.jobs import (
    create_job,
    failure_signature,
    get_job,
    mark_job_run,
    resume_job,
    trigger_job,
    update_job,
)
from cron.scheduler import _should_notify_failure, DEFAULT_FAILURE_RENOTIFY_HOURS
from hermes_time import now as _hermes_now


AUTH_ERROR = (
    "RuntimeError: No access token found for Nous Portal login. "
    "Run `hermes model` to re-authenticate."
)


def _make_job(**overrides):
    job = create_job(schedule="every 1h", prompt="check broker deals")
    if overrides:
        job = {**job, **overrides}
    return job


# =========================================================================
# failure_signature
# =========================================================================

class TestFailureSignature:
    def test_stable_for_same_error(self):
        assert failure_signature(AUTH_ERROR) == failure_signature(AUTH_ERROR)

    def test_differs_for_different_errors(self):
        assert failure_signature(AUTH_ERROR) != failure_signature("Invalid refresh token")

    def test_ignores_surrounding_whitespace(self):
        assert failure_signature(f"  {AUTH_ERROR}\n") == failure_signature(AUTH_ERROR)

    def test_ignores_volatile_traceback_tail(self):
        # Only the first 300 chars count — line-number churn in a long
        # traceback must not defeat dedup.
        head = "x" * 300
        assert failure_signature(head + "line 1706") == failure_signature(head + "line 1728")

    def test_none_and_empty_are_equivalent(self):
        assert failure_signature(None) == failure_signature("")


# =========================================================================
# _should_notify_failure
# =========================================================================

class TestShouldNotifyFailure:
    def test_first_failure_notifies(self):
        job = _make_job()
        assert _should_notify_failure(job, AUTH_ERROR) is True

    def test_different_error_always_notifies(self):
        job = _make_job(
            last_failure_sig=failure_signature("some other error"),
            last_failure_notified_at=_hermes_now().isoformat(),
        )
        assert _should_notify_failure(job, AUTH_ERROR) is True

    def test_same_error_recently_notified_is_suppressed(self):
        job = _make_job(
            last_failure_sig=failure_signature(AUTH_ERROR),
            last_failure_notified_at=_hermes_now().isoformat(),
        )
        assert _should_notify_failure(job, AUTH_ERROR) is False

    def test_same_error_never_notified_notifies(self):
        # Signature recorded but notification never delivered (e.g. the
        # delivery itself failed) — must retry the notification.
        job = _make_job(
            last_failure_sig=failure_signature(AUTH_ERROR),
            last_failure_notified_at=None,
        )
        assert _should_notify_failure(job, AUTH_ERROR) is True

    def test_same_error_renotifies_after_interval(self):
        stale = _hermes_now() - timedelta(hours=DEFAULT_FAILURE_RENOTIFY_HOURS, minutes=1)
        job = _make_job(
            last_failure_sig=failure_signature(AUTH_ERROR),
            last_failure_notified_at=stale.isoformat(),
        )
        assert _should_notify_failure(job, AUTH_ERROR) is True

    def test_naive_legacy_timestamp_does_not_crash(self):
        naive = (_hermes_now() - timedelta(minutes=5)).replace(tzinfo=None)
        job = _make_job(
            last_failure_sig=failure_signature(AUTH_ERROR),
            last_failure_notified_at=naive.isoformat(),
        )
        assert _should_notify_failure(job, AUTH_ERROR) is False

    def test_garbage_timestamp_notifies(self):
        job = _make_job(
            last_failure_sig=failure_signature(AUTH_ERROR),
            last_failure_notified_at="not-a-timestamp",
        )
        assert _should_notify_failure(job, AUTH_ERROR) is True

    def test_env_zero_disables_suppression(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_FAILURE_RENOTIFY_HOURS", "0")
        job = _make_job(
            last_failure_sig=failure_signature(AUTH_ERROR),
            last_failure_notified_at=_hermes_now().isoformat(),
        )
        assert _should_notify_failure(job, AUTH_ERROR) is True

    def test_env_invalid_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_FAILURE_RENOTIFY_HOURS", "banana")
        job = _make_job(
            last_failure_sig=failure_signature(AUTH_ERROR),
            last_failure_notified_at=_hermes_now().isoformat(),
        )
        assert _should_notify_failure(job, AUTH_ERROR) is False


# =========================================================================
# mark_job_run streak bookkeeping
# =========================================================================

class TestMarkJobRunStreak:
    def test_failure_increments_streak_and_records_sig(self):
        job = _make_job()
        mark_job_run(job["id"], False, AUTH_ERROR, failure_notified=True)
        saved = get_job(job["id"])
        assert saved["failure_streak"] == 1
        assert saved["last_failure_sig"] == failure_signature(AUTH_ERROR)
        assert saved["last_failure_notified_at"]

        mark_job_run(job["id"], False, AUTH_ERROR)
        saved = get_job(job["id"])
        assert saved["failure_streak"] == 2

    def test_unnotified_failure_does_not_stamp_notified_at(self):
        job = _make_job()
        mark_job_run(job["id"], False, AUTH_ERROR, failure_notified=False)
        saved = get_job(job["id"])
        assert saved["failure_streak"] == 1
        assert saved.get("last_failure_notified_at") is None

    def test_success_resets_streak_fields(self):
        job = _make_job()
        mark_job_run(job["id"], False, AUTH_ERROR, failure_notified=True)
        mark_job_run(job["id"], True)
        saved = get_job(job["id"])
        assert saved["failure_streak"] == 0
        assert saved.get("last_failure_sig") is None
        assert saved.get("last_failure_notified_at") is None

    def test_error_change_updates_signature(self):
        job = _make_job()
        mark_job_run(job["id"], False, AUTH_ERROR, failure_notified=True)
        mark_job_run(job["id"], False, "Invalid refresh token", failure_notified=True)
        saved = get_job(job["id"])
        assert saved["failure_streak"] == 2
        assert saved["last_failure_sig"] == failure_signature("Invalid refresh token")


# =========================================================================
# Dedup state reset on operator actions
# =========================================================================

class TestDedupStateReset:
    def _fail_once_notified(self):
        job = create_job(schedule="every 1h", prompt="check broker deals")
        mark_job_run(job["id"], False, AUTH_ERROR, failure_notified=True)
        return get_job(job["id"])

    def test_update_of_exec_field_resets_dedup_state(self):
        job = self._fail_once_notified()
        update_job(job["id"], {"prompt": "check broker deals v2"})
        saved = get_job(job["id"])
        assert saved["failure_streak"] == 0
        assert saved.get("last_failure_sig") is None
        assert saved.get("last_failure_notified_at") is None

    def test_update_of_cosmetic_field_keeps_dedup_state(self):
        job = self._fail_once_notified()
        update_job(job["id"], {"name": "renamed job"})
        saved = get_job(job["id"])
        assert saved["failure_streak"] == 1
        assert saved.get("last_failure_notified_at")

    def test_trigger_job_resets_dedup_state(self):
        job = self._fail_once_notified()
        trigger_job(job["id"])
        saved = get_job(job["id"])
        assert saved["failure_streak"] == 0
        assert saved.get("last_failure_notified_at") is None

    def test_resume_job_resets_dedup_state(self):
        job = self._fail_once_notified()
        resume_job(job["id"])
        saved = get_job(job["id"])
        assert saved["failure_streak"] == 0
        assert saved.get("last_failure_notified_at") is None

    def test_created_job_initializes_dedup_fields(self):
        job = create_job(schedule="every 1h", prompt="check broker deals")
        saved = get_job(job["id"])
        assert saved["failure_streak"] == 0
        assert saved.get("last_failure_sig") is None
        assert saved.get("last_failure_notified_at") is None


# =========================================================================
# End-to-end suppression story
# =========================================================================

class TestSuppressionLifecycle:
    def test_hourly_auth_failure_notifies_once_then_recovers(self):
        """The 2026-07-03 incident shape: hourly job, dead provider login.

        Run 1 fails → notify. Runs 2..N fail identically → suppressed.
        Re-auth happens out-of-band → next run succeeds → recovery expected
        (prev streak > 0), streak fields reset.
        """
        job = _make_job()

        # Run 1: first failure → notify.
        assert _should_notify_failure(get_job(job["id"]), AUTH_ERROR) is True
        mark_job_run(job["id"], False, AUTH_ERROR, failure_notified=True)

        # Runs 2-4: identical failure inside the window → suppressed.
        for _ in range(3):
            assert _should_notify_failure(get_job(job["id"]), AUTH_ERROR) is False
            mark_job_run(job["id"], False, AUTH_ERROR, failure_notified=False)

        saved = get_job(job["id"])
        assert saved["failure_streak"] == 4

        # Operator re-authenticates; next run succeeds. Recovery notice fires
        # only when a failure notice was actually delivered during the streak.
        assert int(saved.get("failure_streak") or 0) > 0
        assert saved.get("last_failure_notified_at")  # notice was seen → recovery fires
        mark_job_run(job["id"], True)
        saved = get_job(job["id"])
        assert saved["failure_streak"] == 0
        assert saved.get("last_failure_sig") is None

    def test_soft_failure_streak_never_notified_blocks_recovery_banner(self):
        """A streak of soft failures (empty responses) is never delivered, so
        the recovery banner condition (last_failure_notified_at set) must stay
        false — no 'recovered' message about failures nobody heard of."""
        job = _make_job()
        for _ in range(3):
            mark_job_run(
                job["id"], False,
                "Agent completed but produced empty response (model error, timeout, or misconfiguration)",
                failure_notified=False,
            )
        saved = get_job(job["id"])
        assert saved["failure_streak"] == 3
        assert saved.get("last_failure_notified_at") is None  # gate closed
