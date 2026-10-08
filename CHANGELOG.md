# Changelog

## Unreleased

- Added a non-invasive GitHub Releases update check in the UI.
- Added **Check Updates / Open Release** without automatic downloading or installation.
- Added `.github/CODEOWNERS` so Freedomundead is the default reviewer/owner for contributions.
- Kept the frozen Timer lifecycle unchanged.

## 0.4.1 - Public beta

- Prepared the project for public GitHub release.
- Added creator credit: Freedomundead.
- Reframed Timer mode as the supported product.
- Renamed the experimental Advanced surface to Labs.
- Labs desktop auto-dispatch defaults off.
- Added MIT license, contribution guide, roadmap, security guidance, GitHub issue templates, and public architecture/development docs.
- Kept the proven timer/dispatch core unchanged.

## 0.4.0

- UI/UX polish built on the proven v0.3.6 timer lifecycle.
- Added live status, lifecycle card, presets, and improved Timer layout.
- Added Goal Builder experiments to the advanced surface.
- Added frozen-core hash regression protection.

## 0.3.6

- Working Windows desktop dispatch baseline.
- Fixed Win32 `GetCurrentThreadId` import to use `kernel32.dll`.
- Verified real limit → wait → reset → one continuation dispatch lifecycle.
