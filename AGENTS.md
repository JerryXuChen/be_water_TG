# Repository Guidelines

## Project Structure & Module Organization

`main.py` starts Flask; `web_app.py` defines APIs, and `web_manager.py` coordinates background state and Server-Sent Events. Core logic lives in `src/`: `activity_observer.py`, `participation_policy.py`, `safety_guard.py`, `message_generator.py`, and `state_store.py` form the authorized-group participation pipeline. `ui/send_loop.py` orchestrates candidates; the other visual `ui/` modules are legacy Flet code. Browser assets live in `templates/` and `static/`. Tests mirror behavior under `tests/`, with shared fixtures in `tests/conftest.py`.

## Build, Test, and Development Commands

Create a Python 3.12+ environment, then run:

```bash
pip install -r requirements.txt   # install runtime and test dependencies
copy .env.example .env            # create local Windows configuration
python main.py                     # serve the Web UI at http://127.0.0.1:5000
pytest tests/ -v                   # run the complete test suite
pytest tests/test_selector.py -v  # run one focused test module
```

`run.bat` also launches the Web UI, but currently contains a machine-specific Python path; update it before relying on it elsewhere. There is no separate build step.

## Coding Style & Naming Conventions

Use four-space indentation, LF line endings, a final newline, and Python type hints. Follow existing Python conventions: `snake_case` for modules, functions, fixtures, and variables; `PascalCase` for classes; and `UPPER_CASE` for constants. Keep asynchronous operations explicit with `async`/`await`, and preserve the separation between Flask/thread coordination and Telegram async logic. No formatter or linter is configured, so match nearby code and keep imports grouped as standard library, third-party, then local.

## Testing Guidelines

Tests use `pytest` and `pytest-asyncio`. Name files `test_<area>.py` and tests `test_<behavior>()`. Add regression coverage for state transitions, concurrency, configuration compatibility, API responses, and error handling when those paths change. Use fixtures and mocks rather than real Telegram or AI network calls. The repository defines no coverage threshold; passing the full suite is the acceptance baseline.

## Commit & Pull Request Guidelines

History follows Conventional Commit-style prefixes such as `feat:`, `fix:`, `test:`, `refactor:`, `docs:`, and `chore:`. Keep subjects concise and scoped to one logical change. Pull requests should explain behavior changes, list validation commands and results, link relevant issues, and include screenshots for visible `templates/` or `static/` changes.

## Security & Configuration

Never commit `.env`, `*.session`, `*.session-journal`, `state/`, API keys, phone numbers, Telegram credentials, or audit databases. Add new settings to `.env.example` with safe placeholder values and preserve backward-compatible parsing where existing configuration keys are supported.
