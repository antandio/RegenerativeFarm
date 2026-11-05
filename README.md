# Regenerative Farming Portal

A bilingual (English / Hungarian) Django application supporting regenerative agriculture transition, farm profile and parcel management, crop rotation and work planning, weather-informed scheduling, knowledge base management, and consent-gated AI advisory and agentic planning workflows.

## What It Is For

Moving a farm from conventional to regenerative practice touches everything at once: soil, crop rotation, cover crops, livestock, machines, people and weather. The portal keeps all of that in one shared farm model, so every plan, answer and AI suggestion is based on the farm's own data instead of generic advice.

It is built for small and medium farms and their teams: the farm manager who plans the season, co-workers who record field work, and advisers or observers who need read-only insight.

Every decision is measured against a built-in regenerative policy (`portal/data/regenerative.md`) based on six principles: context first, minimal soil disturbance, permanent soil cover, plant diversity, living roots all year, and livestock integration.

## What It Can Do

### Farm records

- **Farm profile and locations**, with extra custom fields.
- **Plots and parcels** drawn as polygons on an interactive map.
- **Machines and equipment**, with capacity data and parcel assignments.
- **Livestock groups**, with an immutable event history and livestock work items.
- **Crop cycles and rotation**, with field observations and sowing reconciliation that flags where the actual sowing differs from the plan.
- **Field log**: a journal of the farming work actually done, with a provenance panel showing where each entry and piece of advice came from.
- **External contacts**, such as suppliers, advisers and authorities.

### Planning

- **Work plan**: generated from crop cycles, equipment and the weather, with weekly capacity checks that highlight machine overload.
- **Utilisation**: summary, timeline (Gantt) and per-machine views that reveal hidden bottlenecks.
- **Weather**: a 7-day Open-Meteo forecast and a climate baseline of up to 10 years of monthly means, used to annotate planned work.
- **Tasks**: to-do items created by hand or from accepted AI advice, with status tracking.

### Knowledge

- **Regenerative principles**: the built-in policy, readable in the portal.
- **Rules**: custom rule categories and definitions that can be switched on or off per farm.
- **Documents**: private uploads (never served as public static files) with Markdown preview, which can be added to the farm's knowledge base.
- **News sources**: a personal collection of links to agricultural news portals.

### AI assistance (optional, consent-gated)

All AI features stay off until a farm manager explicitly consents to cloud processing. The consent can be revoked at any time, and every use is limited by per-farm and global quotas.

- **Structured advice**: a 3-step analysis of the farm with concrete recommendations that can be turned into tasks.
- **Chat**: questions about the whole farm or a single parcel. Answers cite the farm data they used, and any proposed change needs explicit confirmation.
- **Planning agent**: turns a goal (for example, "plan cover crops for the autumn") into a typed changeset across 28 kinds of farm data. The changeset passes policy and agronomy gates and an independent critic, and nothing is written until a user approves it.
- **Safety screen**: AI output is checked for content the portal must not give, such as chemical dose rates, legal or subsidy guarantees, guaranteed yields and veterinary prescriptions. Instruction-like text inside farm fields is treated as data, never followed.

### Team and account

- **Roles**: manager, worker and viewer, with invitations by e-mail.
- **Bilingual interface**: Hungarian (default) and English.
- **Data rights**: personal data export and account deletion.

Screenshots, diagrams and visuals of these features are in [`screenshots/`](screenshots/README.md).

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
