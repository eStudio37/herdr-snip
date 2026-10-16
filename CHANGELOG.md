# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Sound effects on macOS through `afplay`, in testing. Over SSH they stay off unless the config sets `sound = on`.

### Changed

- On macOS the sound player listens on a socket file in the state folder, since macOS has no abstract unix sockets.

## [0.1.0] - 2026-10-15

### Added

- Freeze the active pane with one key and box part of it with two clicks. The frozen copy lines up cell for cell with the live screen, and the box shows its real colors as the mouse moves.
- A question card, then an agent card listing every agent in the Herdr server with its status. Blocked agents can't be picked; the last agent sent to is preselected.
- Messages carry a header naming the operator and the agent, pane, session and folder the box came from.
- A braille hit spark on each click, and 8-bit sound effects on WSL.
- Settings for the operator's name, sound and volume.

[Unreleased]: https://github.com/eStudio37/herdr-snip/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/eStudio37/herdr-snip/releases/tag/v0.1.0
