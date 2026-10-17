# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.1] - 2026-10-17

### Security

- Screen text can no longer pass for the operator's words. The fence around the box is always longer than any run of backticks inside it, the window title and folder go in their own fenced block, and the message says fenced text is screen content, not instructions.
- Agent names, titles, folders and Herdr errors are drawn and sent without control characters, so an escape sequence in a folder name can't act on the terminal. 8-bit control characters in the frozen screen are dropped too.
- The state folder is private (0700), and the frozen screen is deleted when the overlay closes. Snip refuses a state folder that belongs to another user.
- On Linux the sound player now listens on a socket file in that private folder. The abstract socket it used had no owner, so another local user could take its name and read the timing of your keystrokes.
- The sound player accepts only known sound names, and replaces a stale socket only if the path really is a socket.
- Malformed color codes in the frozen screen no longer close the overlay.

## [0.2.0] - 2026-10-17

### Added

- Sound effects on macOS through `afplay`, checked by ear on macOS 15. Over SSH they stay off unless the config sets `sound = on`.

### Changed

- On macOS the sound player listens on a socket file in the state folder, since macOS has no abstract unix sockets.

## [0.1.0] - 2026-10-15

### Added

- Freeze the active pane with one key and box part of it with two clicks. The frozen copy lines up cell for cell with the live screen, and the box shows its real colors as the mouse moves.
- A question card, then an agent card listing every agent in the Herdr server with its status. Blocked agents can't be picked; the last agent sent to is preselected.
- Messages carry a header naming the operator and the agent, pane, session and folder the box came from.
- A braille hit spark on each click, and 8-bit sound effects on WSL.
- Settings for the operator's name, sound and volume.

[Unreleased]: https://github.com/eStudio37/herdr-snip/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/eStudio37/herdr-snip/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/eStudio37/herdr-snip/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/eStudio37/herdr-snip/releases/tag/v0.1.0
