# Cotinga

A personal nutrition tracker for logging meals and tracking calories, protein,
carbs, and fat against daily targets.

Built with Django, SQLite, Django templates, CSS, and JavaScript. Includes Docker,
Caddy, and GitHub Actions configuration.

## Development

```bash
uv sync --locked --all-groups
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

Open <http://127.0.0.1:8000/> and sign in to use your journal.

## Checks

```bash
uv run pre-commit run --all-files
uv run pytest --cov
```
