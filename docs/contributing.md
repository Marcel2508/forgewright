# Contributing

Thanks for considering a contribution! This document covers how to set up a
development environment, run the tests, and land a change.

## Ground rules

- Keep the diff focused. Unrelated refactors belong in a separate MR.
- Add or update tests for any behaviour change.
- Conventional Commits: `feat:`, `fix:`, `refactor:`, `test:`, `docs:`,
  `chore:` — keep commits small and self-contained.
- Be kind in code review, both as reviewer and author.

## Dev setup

```bash
git clone <your-fork> forgewright
cd forgewright
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

That installs the package in editable mode along with `pytest`.

## Running tests

```bash
.venv/bin/pytest                       # full suite
.venv/bin/pytest tests/ -v             # verbose
.venv/bin/pytest tests/test_decision.py # single module
.venv/bin/pytest -k "fingerprint"      # filter by name
```

The suite uses pure pytest with fixtures in `tests/conftest.py`:

- `tmp_config` — a `Config` with all paths redirected to `tmp_path`.
- `mock_platform` — a concrete `Platform` subclass that records calls instead
  of hitting the network.
- `mock_agent` — an `Agent` implementation that returns a fixed
  `AgentResult` without invoking any subprocess.
- `make_note()`, `make_issue()`, `make_mr()` — factories for the dataclasses
  in `forgewright/types.py`.

Handler tests stub out git operations (`clone_or_update_mirror`,
`make_worktree`, `push_branch`, `cleanup_worktree`, `run`) so they don't
touch the filesystem or fork subprocesses.

## Linting and formatting

The project does not enforce a specific formatter. Match the surrounding
style (PEP 8-ish, 80–100 column soft wrap, type hints on public functions).
If you run `ruff` or `black` locally, stage only the lines relevant to your
change — do not mass-reformat unrelated files in the same MR.

## Project layout

See [Architecture](architecture.md) for the full breakdown. A quick tour:

- `forgewright/platform/` — abstract platform interface plus GitLab/GitHub
  implementations.
- `forgewright/agent/` — abstract agent interface plus Claude Code/OpenCode
  implementations.
- `forgewright/prompts.py` — the three prompt templates.
- `forgewright/handlers.py` — orchestration for issues, MR updates, MR reviews.
- `forgewright/decision.py` — trigger rules and fingerprinting.
- `tests/` — pytest suite mirroring the module layout.

## Common changes

### Adding a new Platform method

1. Add the abstract method to `forgewright/platform/base.py`.
2. Implement it in `forgewright/platform/gitlab.py` and
   `forgewright/platform/github.py`.
3. Call it from the handlers or posting modules.
4. Add a unit test using `mock_platform` from `tests/conftest.py`.

### Adding a new Platform

1. Create `forgewright/platform/<name>.py` with a class implementing
   `Platform`.
2. Register it in `forgewright/platform/__init__.py:create_platform()`.
3. Add any new config keys to `Config` in `forgewright/config.py` and
   document them in `config.example.yaml` and
   [`docs/configuration.md`](configuration.md).
4. Add tests covering the happy path and at least one error case.

### Adding a new Agent

1. Create `forgewright/agent/<name>.py` implementing `Agent`.
2. Register it in `forgewright/agent/__init__.py:create_agent()`.
3. Add config keys (binary path, model, timeout) to `Config` and the example
   YAML.
4. Add a test under `tests/test_agent.py`.

### Changing a prompt template

All prompts live in `forgewright/prompts.py` and are rendered with
`str.format()`. The handler in `forgewright/handlers.py` supplies the format
kwargs. If you add a new placeholder:

1. Add `{your_placeholder}` to the template.
2. Pass `your_placeholder=…` from every `.format()` call that uses that
   template in `handlers.py`.
3. Any existing literal `{` or `}` in the template must be doubled to `{{` /
   `}}`.

### Modifying trigger conditions

Edit `forgewright/decision.py`. `should_process_issue()` and
`should_process_mr()` return `(bool, reason_string)` — the reason string is
what shows up in the log when the bot decides to skip. The `fingerprint_*`
helpers decide what counts as a change.

## Submitting a change

1. Branch off `main` (or whatever the default branch of your fork is).
2. Open a merge request against this project's default branch.
3. Describe the motivation briefly — link to an issue if one exists.
4. Make sure CI is green: `pytest` must pass, Docker image must build.
5. Expect at least one round of review comments. Push fixups to the same
   branch; squashing before merge is fine.

## Security disclosures

If you find a security issue, please do not open a public issue. Email the
maintainer directly, or use your platform's private vulnerability reporting
feature if available.
