# Roadmap

The roadmap is intentionally Timer-first.

## Stable / current

- [x] Read Codex 5-hour and weekly allowance state.
- [x] Detect real usage-limit denial.
- [x] Persist reset/wait state.
- [x] Recheck allowance after reset.
- [x] Dispatch one continuation message to the open Codex Desktop chat.
- [x] Prevent repeated sends during the same allowance cycle.
- [x] Editable/persisted continuation message.
- [x] Presets.
- [x] Timer-focused UI.
- [x] Frozen-core regression protection.

## High-value polish

- [ ] Windows system-tray mode.
- [ ] Native Windows notifications for limit detected / reset / dispatch failure.
- [ ] Start with Windows.
- [ ] Optional keep-awake / wake-timer behavior.
- [ ] Small local event history: armed, limit hit, reset, sent, failure.
- [ ] Stronger post-dispatch verification.
- [ ] Accessibility and keyboard-navigation pass.

## Compatibility

- [ ] Wider Windows 11 version testing.
- [ ] Codex Desktop release compatibility matrix.
- [ ] Automated test harness for foreground-window behavior.

## Labs / research

These are not required for Timer mode:

- [ ] Managed Goal queues.
- [ ] Project/scope task targeting.
- [ ] Existing-task adoption experiments.
- [ ] Owner-dispatch APIs if Codex exposes a stable supported interface in the future.

Ideas are welcome, but stable Timer behavior takes priority over Labs complexity.
