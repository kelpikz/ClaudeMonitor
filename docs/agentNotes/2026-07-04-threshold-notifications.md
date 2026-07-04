# Threshold notifications

Added desktop notifications for 5h usage remaining crossing downward through
50%, 30%, and 10%.

- New `claudemonitor.notifications` module owns the stateful crossing logic.
  It only notifies after an observed transition, so starting the app already
  below a threshold does not spam a notification.
- Fetch errors and missing 5h data do not update the tracked previous value,
  which prevents stale/offline responses from triggering alerts.
- `main.py` creates one `ThresholdNotifier` inside the poll loop setup and
  sends returned notifications through `tray.notify()`.
- `tray.notify()` delegates to `pystray.Icon.notify`, avoiding a new desktop
  notification dependency.

Tests added for the end-to-end crossing path, helper functions, reset re-arming,
fetch-error behavior, and tray notification delegation.
