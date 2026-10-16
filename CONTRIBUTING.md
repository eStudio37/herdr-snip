# Contributing

Issues and pull requests are welcome.

## Reporting a bug

Use the bug report form. The most useful details are your Herdr version
(`herdr --version`), your OS and terminal, and the contents of
`~/.local/state/herdr-snip/result.json` from the snip that went wrong.

## Making a change

1. Link your clone so Herdr runs it: `herdr plugin link "$PWD"`.
2. Keep to the Python standard library, Python 3.9 or later. The plugin has no
   install step, and that should stay true.
3. Run the tests and the linter before opening a pull request:

   ```sh
   python3 -m unittest discover -s tests -v
   ruff check bin/snip bin/snip-open bin/snip-sound tests/fake-herdr lib tests
   ```

4. Add a line to `CHANGELOG.md` under "Unreleased".

The end-to-end tests drive the real picker in a pseudo-terminal. If a change
alters what the picker draws or sends, extend `tests/test_snip.py` so the new
behavior is checked the same way.
