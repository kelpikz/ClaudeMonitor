from __future__ import annotations

import logging
import threading

import pytest

from claudemonitor.models import Rect
from claudemonitor.taskbar_companion import (
    DisabledTaskbarCompanion,
    TaskbarCompanion,
    companion_slot,
    create_taskbar_companion,
    leftmost_abutting_edge,
    retry_delay_seconds,
    taskbar_child_rect,
)


TASKBAR = Rect(left=0, top=1032, right=1920, bottom=1080)
NOTIFICATION = Rect(left=1542, top=1032, right=1920, bottom=1080)


class TestLeftmostAbuttingEdge:
    """Placement must step over taskbar plugins that already sit before the clock."""

    def test_steps_over_a_single_neighbouring_plugin(self):
        # A TrafficMonitor-style plugin occupies 1440..1594 right before the
        # notification area, so the companion must anchor left of it.
        children = [Rect(1440, 1032, 1594, 1080)]

        assert leftmost_abutting_edge(Rect(1594, 1032, 1920, 1080), children) == 1440

    def test_chains_across_several_abutting_plugins(self):
        children = [
            Rect(1440, 1032, 1594, 1080),
            Rect(1300, 1032, 1445, 1080),
            Rect(200, 1032, 400, 1080),
        ]

        assert leftmost_abutting_edge(Rect(1594, 1032, 1920, 1080), children) == 1300

    def test_ignores_windows_that_do_not_touch_the_anchor(self):
        children = [Rect(200, 1032, 400, 1080), Rect(900, 1032, 1100, 1080)]

        assert leftmost_abutting_edge(Rect(1594, 1032, 1920, 1080), children) == 1594

    def test_ignores_windows_on_a_different_taskbar_row(self):
        # A flyout or tooltip whose right edge happens to line up with the clock
        # but which sits above the taskbar must not push the label sideways.
        children = [Rect(1440, 200, 1594, 260)]

        assert leftmost_abutting_edge(Rect(1594, 1032, 1920, 1080), children) == 1594


class TestTaskbarChildRect:
    """The reserved slot is expressed in taskbar-relative coordinates."""

    def test_sits_immediately_before_the_notification_area(self):
        assert taskbar_child_rect(TASKBAR, NOTIFICATION, width=180) == Rect(
            left=1362,
            top=0,
            right=1542,
            bottom=48,
        )

    def test_never_extends_past_the_left_taskbar_edge(self):
        narrow_taskbar = Rect(left=0, top=1032, right=200, bottom=1080)
        notification = Rect(left=100, top=1032, right=200, bottom=1080)

        assert taskbar_child_rect(narrow_taskbar, notification, width=180).left == 0

    def test_inconsistent_geometry_never_yields_a_negative_width(self):
        # A restarting Explorer can report a taskbar whose left edge is right of
        # the notification area it supposedly contains.
        taskbar = Rect(left=100, top=1032, right=1920, bottom=1080)
        notification = Rect(left=50, top=1032, right=1920, bottom=1080)

        assert taskbar_child_rect(taskbar, notification, width=180).width >= 0


class TestCompanionSlot:
    """Yielding space to plugins must never squeeze the label into a sliver."""

    def test_yields_space_to_an_abutting_plugin(self):
        siblings = [Rect(1440, 1032, 1542, 1080)]

        slot = companion_slot(TASKBAR, NOTIFICATION, siblings, width=180, minimum_width=60)

        assert slot == Rect(left=1260, top=0, right=1440, bottom=48)

    def test_falls_back_to_the_clock_edge_when_plugins_leave_too_little_room(self):
        # A chain of plugins reaching the taskbar's left edge would otherwise
        # leave a 40px stub, which is narrower than the text needs.
        siblings = [Rect(0, 1032, 1542, 1080)]

        slot = companion_slot(TASKBAR, NOTIFICATION, siblings, width=180, minimum_width=60)

        assert slot == Rect(left=1362, top=0, right=1542, bottom=48)


class TestRetryDelay:
    """A persistently broken window must back off instead of retrying at 1 Hz."""

    def test_the_first_retry_waits_the_base_delay(self):
        assert retry_delay_seconds(1, base=1.0, cap=60.0) == 1.0

    def test_each_further_failure_doubles_the_wait(self):
        assert retry_delay_seconds(2, base=1.0, cap=60.0) == 2.0
        assert retry_delay_seconds(4, base=1.0, cap=60.0) == 8.0

    def test_the_wait_is_capped(self):
        assert retry_delay_seconds(20, base=1.0, cap=60.0) == 60.0

    def test_a_zero_base_stays_zero_for_fast_tests(self):
        assert retry_delay_seconds(5, base=0.0, cap=60.0) == 0.0


class _FakeNativeWindow:
    """Record the controller's requests and let tests inject native failures."""

    def __init__(self, *, pump_rounds: int = 2, attach_succeeds: bool = True):
        self.calls: list[tuple[object, ...]] = []
        self.pump_rounds = pump_rounds
        self.attach_succeeds = attach_succeeds
        self.notification_rect = NOTIFICATION
        self.taskbar_rect = TASKBAR
        self.sibling_rects: list[Rect] = []
        self.next_handle = 30
        # Raise this error from the next matching call, then clear it.
        self.fail_once_on: str | None = None
        self.always_fail_on: str | None = None
        self.window_created = threading.Event()
        self.messages_pumped = threading.Event()
        self.visibility_changed = threading.Event()

    def _record(self, *call: object) -> None:
        self.calls.append(call)
        name = call[0]
        if self.fail_once_on == name:
            self.fail_once_on = None
            raise OSError(f"simulated native failure in {name}")
        if self.always_fail_on == name:
            raise OSError(f"simulated native failure in {name}")

    def find_taskbar(self):
        self._record("find_taskbar")
        return 10

    def find_notification_area(self, taskbar):
        self._record("find_notification_area", taskbar)
        return 20

    def get_rect(self, handle):
        self._record("get_rect", handle)
        return self.taskbar_rect if handle == 10 else self.notification_rect

    def create_window(self, *, text):
        self._record("create_window", text)
        self.next_handle += 1
        self.window_created.set()
        return self.next_handle

    def attach_to_taskbar(self, handle, taskbar):
        self._record("attach_to_taskbar", handle, taskbar)
        return self.attach_succeeds

    def list_sibling_rects(self, taskbar, exclude_handle):
        self._record("list_sibling_rects", taskbar, exclude_handle)
        return list(self.sibling_rects)

    def set_colorkey_transparency(self, handle):
        self._record("set_colorkey_transparency", handle)

    def refresh_theme(self, handle):
        self._record("refresh_theme", handle)

    def move_window(self, handle, rect, *, topmost):
        self._record("move_window", handle, rect, topmost)

    def set_text(self, handle, text):
        self._record("set_text", handle, text)

    def set_visible(self, handle, visible):
        self._record("set_visible", handle, visible)
        self.visibility_changed.set()

    def pump_messages(self, stop_requested, duration_seconds):
        self._record("pump_messages", duration_seconds)
        self.messages_pumped.set()
        self.pump_rounds -= 1
        if self.pump_rounds <= 0:
            stop_requested.set()

    def close_window(self, handle):
        self._record("close_window", handle)


class _RecordingStopEvent:
    """Wrap the stop event so the controller's retry delays can be asserted."""

    def __init__(self, stop_after_waits: int) -> None:
        self._event = threading.Event()
        self._stop_after_waits = stop_after_waits
        self.waits: list[float] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        if len(self.waits) >= self._stop_after_waits:
            self._event.set()
        return self._event.is_set()

    def set(self) -> None:
        self._event.set()

    def clear(self) -> None:
        self._event.clear()

    def is_set(self) -> bool:
        return self._event.is_set()


def _handle(native: _FakeNativeWindow) -> int:
    """Return the handle of the most recently created fake window."""
    return native.next_handle


def _calls_named(native: _FakeNativeWindow, name: str) -> list[tuple[object, ...]]:
    return [call for call in native.calls if call[0] == name]


def _stop_after(
    native: _FakeNativeWindow,
    call_name: str,
    occurrences: int,
    companion: TaskbarCompanion,
) -> None:
    """Ask the companion to shut down once a native call has happened N times."""
    original_record = native._record
    seen = [0]

    def record(*call: object) -> None:
        if call[0] == call_name:
            seen[0] += 1
            if seen[0] >= occurrences:
                companion._stop_requested.set()
        original_record(*call)

    native._record = record


def _stepping_clock(step_seconds: float):
    """Return a monotonic-style clock that advances by a fixed amount per read."""
    elapsed = [0.0]

    def clock() -> float:
        elapsed[0] += step_seconds
        return elapsed[0]

    return clock


class TestStartupSequence:
    """The label is created hidden, placed, and only then revealed."""

    def test_native_window_is_created_with_a_clear_loading_message(self):
        native = _FakeNativeWindow()

        TaskbarCompanion(native=native)._run()

        assert _calls_named(native, "create_window")[0] == (
            "create_window",
            "Claude: loading...",
        )

    def test_usage_text_supplied_before_start_is_rendered_initially(self):
        native = _FakeNativeWindow()
        companion = TaskbarCompanion(native=native)
        companion.update("Claude: 80% (3 hours)")

        companion._run()

        assert _calls_named(native, "create_window")[0] == (
            "create_window",
            "Claude: 80% (3 hours)",
        )

    def test_window_is_positioned_before_it_is_ever_shown(self):
        # Showing first would flash a 1x1 speck at the taskbar's left edge.
        native = _FakeNativeWindow()

        TaskbarCompanion(native=native)._run()

        names = [call[0] for call in native.calls]
        assert names.index("move_window") < names.index("set_visible")

    def test_transparency_is_applied_even_when_parenting_fails(self):
        # Without the colour key the fallback popup paints a solid black box.
        native = _FakeNativeWindow(attach_succeeds=False)

        TaskbarCompanion(native=native)._run()

        assert ("set_colorkey_transparency", _handle(native)) in native.calls


class TestVisibility:
    def test_visibility_can_be_disabled_before_start(self):
        native = _FakeNativeWindow()
        companion = TaskbarCompanion(native=native, initial_visible=False)

        companion.start()
        assert native.window_created.wait(timeout=1)
        try:
            assert not native.messages_pumped.wait(timeout=0.1)
            assert not _calls_named(native, "move_window")
            assert ("set_visible", _handle(native), True) not in native.calls
        finally:
            companion.stop()

    def test_hidden_companion_resumes_native_work_when_made_visible(self):
        native = _FakeNativeWindow(pump_rounds=1)
        companion = TaskbarCompanion(native=native, initial_visible=False)

        companion.start()
        assert native.window_created.wait(timeout=1)
        companion.set_visible(True)
        try:
            assert native.messages_pumped.wait(timeout=1)
            assert ("set_visible", _handle(native), True) in native.calls
            assert _calls_named(native, "move_window")
        finally:
            companion.stop()

    def test_usage_text_updates_while_native_window_is_running(self):
        native = _FakeNativeWindow()
        companion = TaskbarCompanion(native=native)
        original_pump = native.pump_messages

        def update_during_first_pump(stop_requested, duration_seconds):
            companion.update("Claude: 80% (3 hours)")
            original_pump(stop_requested, duration_seconds)

        native.pump_messages = update_during_first_pump

        companion._run()

        assert ("set_text", _handle(native), "Claude: 80% (3 hours)") in native.calls

    def test_visibility_updates_while_native_window_is_running(self):
        native = _FakeNativeWindow()
        companion = TaskbarCompanion(native=native)
        original_pump = native.pump_messages

        def hide_during_first_pump(stop_requested, duration_seconds):
            companion.set_visible(False)
            original_pump(stop_requested, duration_seconds)

        native.pump_messages = hide_during_first_pump

        companion.start()
        try:
            assert native.visibility_changed.wait(timeout=1)
            assert ("set_visible", _handle(native), False) in native.calls
        finally:
            companion.stop()


class TestPlacement:
    def test_companion_embeds_into_the_taskbar_like_trafficmonitor(self):
        # TrafficMonitor's approach: SetParent into Shell_TrayWnd, color-key
        # transparency, and parent-relative placement before TrayNotifyWnd.
        native = _FakeNativeWindow(pump_rounds=2)

        TaskbarCompanion(native=native)._run()

        assert ("attach_to_taskbar", _handle(native), 10) in native.calls
        assert _calls_named(native, "move_window")[0] == (
            "move_window",
            _handle(native),
            Rect(left=1362, top=0, right=1542, bottom=48),
            False,
        )

    def test_embedded_companion_yields_space_to_other_taskbar_plugins(self):
        native = _FakeNativeWindow(pump_rounds=2)
        native.sibling_rects = [Rect(1440, 1032, 1542, 1080)]

        TaskbarCompanion(native=native)._run()

        assert _calls_named(native, "move_window")[0][2] == Rect(
            left=1260, top=0, right=1440, bottom=48
        )

    def test_embedded_companion_does_not_reposition_while_geometry_is_stable(self):
        native = _FakeNativeWindow(pump_rounds=3)

        TaskbarCompanion(native=native)._run()

        assert len(_calls_named(native, "move_window")) == 1

    def test_embedded_companion_follows_notification_area_changes(self):
        native = _FakeNativeWindow(pump_rounds=2)
        original_pump = native.pump_messages

        def pump_and_grow_notification_area(stop_requested, duration_seconds):
            original_pump(stop_requested, duration_seconds)
            native.notification_rect = Rect(1500, 1032, 1920, 1080)

        native.pump_messages = pump_and_grow_notification_area

        TaskbarCompanion(native=native)._run()

        move_calls = _calls_named(native, "move_window")
        assert move_calls[0][2] == Rect(left=1362, top=0, right=1542, bottom=48)
        assert move_calls[-1][2] == Rect(left=1320, top=0, right=1500, bottom=48)

    def test_an_empty_slot_is_never_pushed_to_windows(self, caplog):
        # SetWindowPos rejects a negative width, which would restart the whole
        # failure/rebuild cycle once per second on transient bad geometry.
        native = _FakeNativeWindow(pump_rounds=3)
        native.taskbar_rect = Rect(left=100, top=1032, right=1920, bottom=1080)
        native.notification_rect = Rect(left=50, top=1032, right=1920, bottom=1080)

        with caplog.at_level(logging.WARNING):
            TaskbarCompanion(native=native)._run()

        assert not _calls_named(native, "move_window")
        # The condition is reported once per streak, not once per second.
        assert len([r for r in caplog.records if "empty slot" in r.message]) == 1


class TestFallbackPopup:
    """Windows 11 periodically raises the taskbar above every topmost window."""

    def test_fallback_uses_absolute_screen_coordinates_and_stays_topmost(self):
        native = _FakeNativeWindow(pump_rounds=1, attach_succeeds=False)

        TaskbarCompanion(native=native)._run()

        move_calls = _calls_named(native, "move_window")
        assert move_calls[0][2] == Rect(left=1362, top=1032, right=1542, bottom=1080)
        assert move_calls[0][3] is True

    def test_topmost_is_reasserted_periodically_rather_than_every_second(self):
        native = _FakeNativeWindow(pump_rounds=6, attach_succeeds=False)

        TaskbarCompanion(native=native, clock=_stepping_clock(0.5))._run()

        # Six stable passes covering three seconds must not re-stack the window
        # six times; only the initial placement is required.
        assert len(_calls_named(native, "move_window")) == 1

    def test_topmost_is_reasserted_once_the_interval_elapses(self):
        native = _FakeNativeWindow(pump_rounds=3, attach_succeeds=False)

        TaskbarCompanion(native=native, clock=_stepping_clock(10.0))._run()

        assert len(_calls_named(native, "move_window")) == 3


class TestThemeRefresh:
    """A taskbar child never receives WM_SETTINGCHANGE, so the controller has to
    ask the native layer to re-read light/dark mode itself."""

    def test_theme_is_read_once_on_the_first_visible_pass(self):
        native = _FakeNativeWindow(pump_rounds=6)

        TaskbarCompanion(native=native, clock=_stepping_clock(0.5))._run()

        assert len(_calls_named(native, "refresh_theme")) == 1

    def test_theme_is_re_read_once_the_interval_elapses(self):
        native = _FakeNativeWindow(pump_rounds=3)

        TaskbarCompanion(native=native, clock=_stepping_clock(10.0))._run()

        assert len(_calls_named(native, "refresh_theme")) == 3

    def test_a_hidden_label_does_no_theme_work(self):
        native = _FakeNativeWindow()
        companion = TaskbarCompanion(native=native, initial_visible=False)

        companion.start()
        assert native.window_created.wait(timeout=1)
        try:
            assert not _calls_named(native, "refresh_theme")
        finally:
            companion.stop()


class TestNativeFailureRecovery:
    """A transient Windows failure must not silently delete the label forever."""

    def _run_until_failures(
        self,
        failing_call: str,
        *,
        stop_after: int,
        stop_on: str | None = None,
    ) -> _FakeNativeWindow:
        """Fail one native call repeatedly and stop after enough attempts."""
        native = _FakeNativeWindow(pump_rounds=99)
        native.always_fail_on = failing_call
        companion = TaskbarCompanion(native=native, failure_backoff_seconds=0.0)
        _stop_after(native, stop_on or failing_call, stop_after, companion)

        companion._run()
        return native

    def test_transient_geometry_failure_is_retried_on_the_next_pass(self):
        # A taskbar child can be destroyed between enumeration and measurement,
        # which surfaces here as a one-off OSError from the native layer.
        native = _FakeNativeWindow(pump_rounds=3)
        native.fail_once_on = "list_sibling_rects"

        TaskbarCompanion(native=native, failure_backoff_seconds=0.0)._run()

        assert _calls_named(native, "move_window")
        assert len(_calls_named(native, "create_window")) == 1

    def test_persistent_failure_recreates_the_native_window(self):
        # Explorer restarting invalidates our HWND; every later call fails until
        # the window is rebuilt against the new taskbar.
        native = self._run_until_failures("move_window", stop_after=6)

        assert len(_calls_named(native, "create_window")) > 1

    @pytest.mark.parametrize(
        "failing_call",
        ["attach_to_taskbar", "set_colorkey_transparency"],
    )
    def test_setup_failing_after_creation_still_destroys_the_window(self, failing_call):
        # Without this, every retry orphans one HWND — roughly 3,600 an hour,
        # which eventually exhausts the process-wide user-handle limit.
        native = self._run_until_failures(
            failing_call,
            stop_after=4,
            stop_on="create_window",
        )

        created = len(_calls_named(native, "create_window"))
        assert created >= 4
        assert len(_calls_named(native, "close_window")) == created

    def test_every_created_window_is_eventually_destroyed(self):
        native = self._run_until_failures("move_window", stop_after=6)

        created = len(_calls_named(native, "create_window"))
        assert len(_calls_named(native, "close_window")) == created

    def test_native_window_is_destroyed_when_its_message_loop_finishes(self):
        native = _FakeNativeWindow()

        TaskbarCompanion(native=native)._run()

        assert native.calls[-1] == ("close_window", _handle(native))

    def test_only_the_first_failure_of_a_streak_logs_a_traceback(self, caplog):
        # A 1 kB traceback every second overwrites the whole 4 MB log ring in
        # about an hour, destroying the history the user is told to read.
        with caplog.at_level(logging.WARNING):
            self._run_until_failures("move_window", stop_after=6)

        tracebacks = [record for record in caplog.records if record.exc_info]
        assert len(tracebacks) == 1

    def test_retry_waits_escalate_while_the_failure_persists(self):
        native = _FakeNativeWindow(pump_rounds=99)
        native.always_fail_on = "find_taskbar"
        companion = TaskbarCompanion(native=native, failure_backoff_seconds=0.5)
        stop_event = _RecordingStopEvent(stop_after_waits=4)
        companion._stop_requested = stop_event

        companion._run()

        assert stop_event.waits == [0.5, 1.0, 2.0, 4.0]


class TestHealth:
    """A label that cannot be rebuilt must stop claiming to be shown."""

    def test_a_new_companion_is_considered_healthy(self):
        assert TaskbarCompanion(native=_FakeNativeWindow()).healthy is True

    def test_repeated_rebuild_failures_mark_the_companion_unhealthy(self):
        native = _FakeNativeWindow(pump_rounds=99)
        native.always_fail_on = "move_window"
        companion = TaskbarCompanion(native=native, failure_backoff_seconds=0.0)
        _stop_after(native, "move_window", 6, companion)

        companion._run()

        assert companion.healthy is False

    def test_a_successful_pass_restores_health(self):
        native = _FakeNativeWindow(pump_rounds=99)
        native.always_fail_on = "move_window"
        companion = TaskbarCompanion(native=native, failure_backoff_seconds=0.0)
        original_record = native._record
        attempts = [0]

        def recover_then_stop(*call: object) -> None:
            if call[0] == "move_window":
                attempts[0] += 1
                if attempts[0] >= 6:
                    native.always_fail_on = None
            if call[0] == "pump_messages":
                companion._stop_requested.set()
            original_record(*call)

        native._record = recover_then_stop
        companion._run()

        assert companion.healthy is True


class TestLifecycle:
    def test_stopping_allows_the_companion_to_be_started_again(self):
        native = _FakeNativeWindow(pump_rounds=99)
        companion = TaskbarCompanion(native=native)

        companion.start()
        assert native.window_created.wait(timeout=1)
        companion.stop()
        native.window_created.clear()

        companion.start()
        try:
            assert native.window_created.wait(timeout=1)
        finally:
            companion.stop()

    def test_stopping_from_the_companion_thread_does_not_orphan_it(self):
        # Clearing _thread while it is still running would let a later start()
        # spawn a second UI thread alongside the first.
        native = _FakeNativeWindow(pump_rounds=99)
        companion = TaskbarCompanion(native=native)
        original_pump = native.pump_messages

        def stop_from_inside(stop_requested, duration_seconds):
            companion.stop()
            original_pump(stop_requested, duration_seconds)

        native.pump_messages = stop_from_inside
        companion.start()
        try:
            assert native.window_created.wait(timeout=1)
        finally:
            companion.stop()

        assert companion._thread is None


class TestCompanionFactory:
    """The taskbar label is optional, so it must never block the tray icon."""

    def test_a_working_native_layer_yields_a_real_companion(self):
        companion = create_taskbar_companion(
            initial_visible=True,
            build_native=_FakeNativeWindow,
        )

        assert isinstance(companion, TaskbarCompanion)

    def test_a_broken_native_layer_yields_a_harmless_stand_in(self, caplog):
        def unavailable():
            raise OSError("user32 export missing")

        with caplog.at_level(logging.ERROR):
            companion = create_taskbar_companion(
                initial_visible=True,
                build_native=unavailable,
            )

        assert isinstance(companion, DisabledTaskbarCompanion)
        assert companion.healthy is False
        assert "taskbar" in caplog.text

    def test_the_stand_in_absorbs_every_call_the_app_makes(self):
        companion = DisabledTaskbarCompanion()

        companion.start()
        companion.update("Claude: 80% (3 hours)")
        companion.set_visible(True)
        companion.stop()

        assert companion.visible is False
