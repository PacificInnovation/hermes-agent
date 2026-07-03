"""Tests for repeat-failure notification dedup (cron/jobs.py + cron/scheduler.py).

A cron job that keeps failing with the same error (e.g. expired provider
login) must notify once, stay quiet for the re-notify interval, and post a
recovery notice when it starts working again — instead of spamming the
origin chat on every tick.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest

import cron.jobs as cron_jobs
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


@pytest.fixture(autouse=True)
def _isolated_jobs_store(tmp_path, monkeypatch):
    """cron.jobs binds HERMES_DIR/CRON_DIR/JOBS_FILE at import time — before
    conftest's per-test HERMES_HOME redirect — so patch the module attributes
    directly (same pattern as tests/cron/test_jobs.py). Without this, these
    tests write real jobs into the developer's live cron store."""
    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")


def _job_dict(**overrides):
    """In-memory job record for pure-function tests. Never persisted."""
    job = {"id": "j1", "name": "monitor"}
    job.update(overrides)
    return job


def _persisted_job():
    return create_job(prompt="check broker deals", schedule="every 1h")


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
# _should_notify_failure (pure function — in-memory job dicts)
# =========================================================================

class TestShouldNotifyFailure:
    def test_first_failure_notifies(self):
        assert _should_notify_failure(_job_dict(), AUTH_ERROR) is True

    def test_different_error_always_notifies(self):
        job = _job_dict(
            last_failure_sig=failure_signature("some other error"),
            last_failure_notified_at=_hermes_now().isoformat(),
        )
        assert _should_notify_failure(job, AUTH_ERROR) is True

    def test_same_error_recently_notified_is_suppressed(self):
        job = _job_dict(
            last_failure_sig=failure_signature(AUTH_ERROR),
            last_failure_notified_at=_hermes_now().isoformat(),
        )
        assert _should_notify_failure(job, AUTH_ERROR) is False

    def test_same_error_never_notified_notifies(self):
        # Signature recorded but notification never delivered (e.g. the
        # delivery itself failed) — must retry the notification.
        job = _job_dict(
            last_failure_sig=failure_signature(AUTH_ERROR),
            last_failure_notified_at=None,
        )
        assert _should_notify_failure(job, AUTH_ERROR) is True

    def test_same_error_renotifies_after_interval(self):
        stale = _hermes_now() - timedelta(hours=DEFAULT_FAILURE_RENOTIFY_HOURS, minutes=1)
        job = _job_dict(
            last_failure_sig=failure_signature(AUTH_ERROR),
            last_failure_notified_at=stale.isoformat(),
        )
        assert _should_notify_failure(job, AUTH_ERROR) is True

    def test_exact_boundary_renotifies(self, monkeypatch):
        # elapsed == interval must notify (>= comparison, not >).
        frozen = _hermes_now()
        monkeypatch.setattr("cron.scheduler._hermes_now", lambda: frozen)
        notified = frozen - timedelta(hours=DEFAULT_FAILURE_RENOTIFY_HOURS)
        job = _job_dict(
            last_failure_sig=failure_signature(AUTH_ERROR),
            last_failure_notified_at=notified.isoformat(),
        )
        assert _should_notify_failure(job, AUTH_ERROR) is True

    def test_naive_legacy_timestamp_does_not_crash(self):
        naive = (_hermes_now() - timedelta(minutes=5)).replace(tzinfo=None)
        job = _job_dict(
            last_failure_sig=failure_signature(AUTH_ERROR),
            last_failure_notified_at=naive.isoformat(),
        )
        assert _should_notify_failure(job, AUTH_ERROR) is False

    def test_garbage_timestamp_notifies(self):
        job = _job_dict(
            last_failure_sig=failure_signature(AUTH_ERROR),
            last_failure_notified_at="not-a-timestamp",
        )
        assert _should_notify_failure(job, AUTH_ERROR) is True

    def test_env_zero_disables_suppression(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_FAILURE_RENOTIFY_HOURS", "0")
        job = _job_dict(
            last_failure_sig=failure_signature(AUTH_ERROR),
            last_failure_notified_at=_hermes_now().isoformat(),
        )
        assert _should_notify_failure(job, AUTH_ERROR) is True

    def test_env_negative_clamps_to_zero(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_FAILURE_RENOTIFY_HOURS", "-5")
        job = _job_dict(
            last_failure_sig=failure_signature(AUTH_ERROR),
            last_failure_notified_at=_hermes_now().isoformat(),
        )
        assert _should_notify_failure(job, AUTH_ERROR) is True

    def test_env_invalid_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_FAILURE_RENOTIFY_HOURS", "banana")
        job = _job_dict(
            last_failure_sig=failure_signature(AUTH_ERROR),
            last_failure_notified_at=_hermes_now().isoformat(),
        )
        assert _should_notify_failure(job, AUTH_ERROR) is False


# =========================================================================
# mark_job_run streak bookkeeping (persisted jobs)
# =========================================================================

class TestMarkJobRunStreak:
    def test_failure_increments_streak_and_records_sig(self):
        job = _persisted_job()
        mark_job_run(job["id"], False, AUTH_ERROR, failure_notified=True)
        saved = get_job(job["id"])
        assert saved["failure_streak"] == 1
        assert saved["last_failure_sig"] == failure_signature(AUTH_ERROR)
        assert saved["last_failure_notified_at"]

        mark_job_run(job["id"], False, AUTH_ERROR)
        saved = get_job(job["id"])
        assert saved["failure_streak"] == 2

    def test_unnotified_failure_does_not_stamp_notified_at(self):
        job = _persisted_job()
        mark_job_run(job["id"], False, AUTH_ERROR, failure_notified=False)
        saved = get_job(job["id"])
        assert saved["failure_streak"] == 1
        assert saved.get("last_failure_notified_at") is None

    def test_success_resets_streak_fields(self):
        job = _persisted_job()
        mark_job_run(job["id"], False, AUTH_ERROR, failure_notified=True)
        mark_job_run(job["id"], True)
        saved = get_job(job["id"])
        assert saved["failure_streak"] == 0
        assert saved.get("last_failure_sig") is None
        assert saved.get("last_failure_notified_at") is None

    def test_error_change_updates_signature(self):
        job = _persisted_job()
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
        job = _persisted_job()
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
        job = _persisted_job()
        saved = get_job(job["id"])
        assert saved["failure_streak"] == 0
        assert saved.get("last_failure_sig") is None
        assert saved.get("last_failure_notified_at") is None


# =========================================================================
# Scheduler wiring — suppression, stamping, recovery through tick()
# =========================================================================

def _origin_job(**overrides):
    job = {
        "id": "j1",
        "name": "monitor",
        "deliver": "origin",
        "origin": {"platform": "telegram", "chat_id": "123"},
    }
    job.update(overrides)
    return job


def _tick(job, run_result, deliver=None, deliver_side_effect=None):
    """Run one scheduler tick with the standard patch set; return the mocks."""
    deliver_kwargs = (
        {"side_effect": deliver_side_effect}
        if deliver_side_effect is not None
        else {"return_value": deliver}
    )
    with patch("cron.scheduler.get_due_jobs", return_value=[job]), \
         patch("cron.scheduler.run_job", return_value=run_result), \
         patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
         patch("cron.scheduler.advance_next_run"), \
         patch("cron.scheduler._deliver_result", **deliver_kwargs) as deliver_mock, \
         patch("cron.scheduler.mark_job_run") as mark_mock:
        from cron.scheduler import tick
        tick(verbose=False)
    return deliver_mock, mark_mock


class TestTickWiring:
    ERR = "RuntimeError: token expired"

    def test_first_failure_notifies_and_stamps(self):
        deliver_mock, mark_mock = _tick(_origin_job(), (False, "# out", "", self.ERR))
        deliver_mock.assert_called_once()
        assert "failed" in deliver_mock.call_args.args[1]
        mark_mock.assert_called_once_with(
            "j1", False, self.ERR, delivery_error=None, failure_notified=True)

    def test_repeat_identical_failure_suppressed_through_tick(self):
        job = _origin_job(
            failure_streak=1,
            last_failure_sig=failure_signature(self.ERR),
            last_failure_notified_at=_hermes_now().isoformat(),
        )
        deliver_mock, mark_mock = _tick(job, (False, "# out", "", self.ERR))
        deliver_mock.assert_not_called()
        mark_mock.assert_called_once_with(
            "j1", False, self.ERR, delivery_error=None, failure_notified=False)

    def test_streak_ordinal_on_renotify_after_window(self):
        stale = _hermes_now() - timedelta(hours=DEFAULT_FAILURE_RENOTIFY_HOURS + 1)
        job = _origin_job(
            failure_streak=2,
            last_failure_sig=failure_signature(self.ERR),
            last_failure_notified_at=stale.isoformat(),
        )
        deliver_mock, mark_mock = _tick(job, (False, "# out", "", self.ERR))
        assert "(failure #3 in a row)" in deliver_mock.call_args.args[1]
        mark_mock.assert_called_once_with(
            "j1", False, self.ERR, delivery_error=None, failure_notified=True)

    def test_recovery_notice_after_notified_streak(self):
        job = _origin_job(
            failure_streak=3,
            last_failure_notified_at=_hermes_now().isoformat(),
        )
        deliver_mock, mark_mock = _tick(job, (True, "# out", "all good", None))
        content = deliver_mock.call_args.args[1]
        assert "recovered after 3 failed run(s)" in content
        assert "all good" in content
        mark_mock.assert_called_once_with(
            "j1", True, None, delivery_error=None, failure_notified=False)

    def test_recovery_notice_delivered_even_when_silent(self):
        job = _origin_job(
            failure_streak=2,
            last_failure_notified_at=_hermes_now().isoformat(),
        )
        deliver_mock, _ = _tick(job, (True, "# out", "[SILENT]", None))
        deliver_mock.assert_called_once()
        content = deliver_mock.call_args.args[1]
        assert "recovered after 2 failed run(s)" in content
        assert "[SILENT]" not in content

    def test_unnotified_streak_success_has_no_recovery_banner(self):
        job = _origin_job(failure_streak=2, last_failure_notified_at=None)
        deliver_mock, _ = _tick(job, (True, "# out", "ok", None))
        deliver_mock.assert_called_once()
        assert "recovered" not in deliver_mock.call_args.args[1]

    def test_unnotified_streak_silent_success_delivers_nothing(self):
        job = _origin_job(failure_streak=2, last_failure_notified_at=None)
        deliver_mock, _ = _tick(job, (True, "# out", "[SILENT]", None))
        deliver_mock.assert_not_called()

    def test_repeated_soft_failure_stays_undelivered(self):
        job = _origin_job(failure_streak=1)
        deliver_mock, mark_mock = _tick(job, (True, "# out", "   \n", None))
        deliver_mock.assert_not_called()
        assert mark_mock.call_args.kwargs["failure_notified"] is False

    def test_delivery_exception_does_not_start_suppression_window(self):
        deliver_mock, mark_mock = _tick(
            _origin_job(), (False, "# out", "", self.ERR),
            deliver_side_effect=RuntimeError("platform down"))
        deliver_mock.assert_called_once()
        mark_mock.assert_called_once_with(
            "j1", False, self.ERR, delivery_error="platform down", failure_notified=False)

    def test_delivery_error_return_does_not_start_suppression_window(self):
        deliver_mock, mark_mock = _tick(
            _origin_job(), (False, "# out", "", self.ERR), deliver="telegram 502")
        deliver_mock.assert_called_once()
        mark_mock.assert_called_once_with(
            "j1", False, self.ERR, delivery_error="telegram 502", failure_notified=False)


# =========================================================================
# format_auth_error relogin copy (delivered into chat by cron failures)
# =========================================================================

class TestReloginErrorCopy:
    def test_relogin_names_provider_and_host_action(self):
        from hermes_cli.auth import AuthError, format_auth_error
        err = AuthError("token expired", provider="nous", relogin_required=True)
        rendered = format_auth_error(err)
        assert "hermes auth add nous --type oauth" in rendered
        assert "cannot be done from chat" in rendered

    def test_relogin_placeholder_when_provider_missing(self):
        from hermes_cli.auth import AuthError, format_auth_error
        err = AuthError("token expired", provider=None, relogin_required=True)
        assert "hermes auth add <provider>" in format_auth_error(err)


# =========================================================================
# _format_job exposure of dedup state
# =========================================================================

class TestFormatJobDedupFields:
    def test_legacy_job_without_dedup_fields_renders_defaults(self):
        from tools.cronjob_tools import _format_job
        out = _format_job({"id": "x", "prompt": "p"})
        assert out["failure_streak"] == 0
        assert out["last_failure_notified_at"] is None

    def test_failing_job_renders_stored_values(self):
        from tools.cronjob_tools import _format_job
        ts = _hermes_now().isoformat()
        out = _format_job({
            "id": "x", "prompt": "p",
            "failure_streak": 4,
            "last_failure_notified_at": ts,
        })
        assert out["failure_streak"] == 4
        assert out["last_failure_notified_at"] == ts


# =========================================================================
# End-to-end suppression story (persisted state transitions)
# =========================================================================

class TestSuppressionLifecycle:
    def test_hourly_auth_failure_notifies_once_then_recovers(self):
        """The 2026-07-03 incident shape: hourly job, dead provider login.

        Run 1 fails → notify. Runs 2..N fail identically → suppressed.
        Re-auth happens out-of-band → next run succeeds → recovery fires
        (streak > 0 AND a notice was actually delivered), fields reset.
        """
        job = _persisted_job()

        # Run 1: first failure → notify.
        assert _should_notify_failure(get_job(job["id"]), AUTH_ERROR) is True
        mark_job_run(job["id"], False, AUTH_ERROR, failure_notified=True)

        # Runs 2-4: identical failure inside the window → suppressed.
        for _ in range(3):
            assert _should_notify_failure(get_job(job["id"]), AUTH_ERROR) is False
            mark_job_run(job["id"], False, AUTH_ERROR, failure_notified=False)

        saved = get_job(job["id"])
        assert saved["failure_streak"] == 4
        assert saved.get("last_failure_notified_at")  # notice was seen → recovery fires

        # Operator re-authenticates; next run succeeds.
        mark_job_run(job["id"], True)
        saved = get_job(job["id"])
        assert saved["failure_streak"] == 0
        assert saved.get("last_failure_sig") is None

    def test_soft_failure_streak_never_notified_blocks_recovery_banner(self):
        """A streak of soft failures (empty responses) is never delivered, so
        the recovery banner condition (last_failure_notified_at set) must stay
        false — no 'recovered' message about failures nobody heard of."""
        job = _persisted_job()
        for _ in range(3):
            mark_job_run(
                job["id"], False,
                "Agent completed but produced empty response (model error, timeout, or misconfiguration)",
                failure_notified=False,
            )
        saved = get_job(job["id"])
        assert saved["failure_streak"] == 3
        assert saved.get("last_failure_notified_at") is None  # gate closed
