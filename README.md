# Regenerative Farming Portal

A bilingual (English / Hungarian) Django application supporting regenerative agriculture transition, farm profile and parcel management, crop rotation and work planning, weather-informed scheduling, knowledge base management, and consent-gated AI advisory and agentic planning workflows.

## Key Features

- **Farm & Domain Management**: Locations, parcels (with interactive polygon mapping), equipment, livestock groups/events, crop rotation cycles, work items, utilization timelines, farming journal, and external contacts.
- **Knowledge Base & Canonical Regenerative Policy**: Built-in regenerative agriculture guidelines (`portal/data/regenerative.md`), custom rules, farm-level rule activations, uploaded Markdown/document previews, and news sources.
- **Structured AI Advice & Interactive Chat**: Background worker queues for 3-step structured farm advice and parcel-scoped interactive chat with explicit cloud-processing consent and quota controls.
- **7-Stage Agentic Planning Loop**: Sourced context assembly, regenerative policy enforcement, typed changeset proposals across 28 command families, independent critic verification, and mandatory user approval prior to execution.

## Quick Start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
cp portal-config.example.toml portal-config.toml
chmod 600 .env portal-config.toml
# Edit .env to set a strong DJANGO_SECRET_KEY and configure portal-config.toml

.venv/bin/python manage.py migrate
.venv/bin/python manage.py seed_demo
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8080 --workers 2
```

## Background Workers

Systemd unit templates are provided under `deploy/`:
- `deploy/regenerative-portal.service`
- `deploy/regenerative-advice-worker.service`
- `deploy/regenerative-chat-worker.service`
- `deploy/regenerative-agent-worker.service`

## Running Tests

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py test
```
