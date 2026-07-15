# Contributing to Trinity

Thank you for your interest in contributing to Trinity! This document provides guidelines for contributing to the project.

## License

Trinity is licensed under the [Apache License 2.0](LICENSE). Unless you explicitly state otherwise, any contribution intentionally submitted for inclusion in Trinity by you shall be licensed under the Apache License 2.0, without any additional terms or conditions (per Section 5 of the license).

## Code of Conduct

- Be respectful and inclusive
- Focus on constructive feedback
- Help others learn and grow

## How to Contribute

> **Where things are tracked.** Trinity is open-core. The public issue tracker
> ([abilityai/trinity](https://github.com/abilityai/trinity/issues)) is for
> **bugs and core maintenance**. Feature ideas and roadmap discussion happen in
> [Discussions](https://github.com/abilityai/trinity/discussions) — maintainers
> triage accepted proposals into the product roadmap.

### Reporting Bugs

1. Check if the issue already exists in [GitHub Issues](https://github.com/abilityai/trinity/issues)
2. If not, create a new issue with:
   - Clear, descriptive title
   - Steps to reproduce
   - Expected vs actual behavior
   - Environment details (OS, Docker version, etc.)
   - Relevant logs or screenshots

### Suggesting Features

Feature ideas go in **Discussions**, not Issues — the public tracker stays focused on bugs, and maintainers curate the roadmap from accepted proposals.

1. Open a [Discussion](https://github.com/abilityai/trinity/discussions) describing the use case and the problem you're solving
2. Propose a solution (optional) and be open to alternatives
3. Accepted ideas are picked up by maintainers and tracked on the roadmap

### Pull Requests

The project follows a 4-stage SDLC: Todo → In Progress → In Dev → Done, tracked via GitHub Issues labels (`status-in-progress`, `status-in-dev`).

**All PRs target the `dev` branch.** `main` only receives release cuts (`dev` → `main`) performed by maintainers — a PR opened against `main` will be asked to retarget. Direct pushes to both `dev` and `main` are blocked by branch protection; everything lands via PR and is **squash-merged**.

1. **Fork and clone** the repository
2. **Find or create an issue** — every PR must link to an issue. Bugs go in [GitHub Issues](https://github.com/abilityai/trinity/issues); feature ideas start in [Discussions](https://github.com/abilityai/trinity/discussions) (see above) and get an issue once accepted
3. **Create a feature branch** from `dev`:
   ```bash
   git checkout dev && git pull origin dev
   git checkout -b feature/<issue-number>-your-feature-name
   ```
   Use `fix/<issue-number>-<slug>` for bug fixes.
4. **Make your changes** following our coding standards, keeping the diff minimal (see [PR Validation](#pr-validation-what-reviewers-check) below)
5. **Test your changes** locally — new behavior needs tests (see [Test Expectations](#test-expectations))
6. **Update documentation** as required by the change type (see [Documentation Requirements](#documentation-requirements))
7. **Commit with clear messages**:
   ```bash
   git commit -m "feat: Add support for custom metrics"
   ```
8. **Push and create a PR against `dev`** — the description **must contain a closing keyword** for the linked issue: `Fixes #N`, `Closes #N`, or `Resolves #N`. A bare `#N` or `Refs #N` links the issue but does **not** trigger the status automation that moves it through the SDLC, so the issue strands in `status-in-progress` after merge.

### Commit Message Format

We use conventional commits:

- `feat:` New feature
- `fix:` Bug fix
- `docs:` Documentation only
- `style:` Formatting, no code change
- `refactor:` Code change that neither fixes nor adds
- `test:` Adding tests
- `chore:` Maintenance tasks

Examples:
```
feat: Add agent custom metrics API
fix: Correct context percentage calculation
docs: Update deployment guide for production
```

## PR Validation (what reviewers check)

Every PR is validated against the Trinity development methodology before merge — maintainers run an automated validation pass and **no PR merges without passing it**. Running through this checklist yourself before opening the PR is the fastest path to a first-pass approval.

### Process Checklist

- [ ] PR targets `dev` (not `main`)
- [ ] PR title/body carries a **closing keyword** (`Fixes #N` / `Closes #N` / `Resolves #N`) referencing an existing issue
- [ ] Commit messages are descriptive and use the conventional prefix (`feat`/`fix`/`refactor`/`docs`)
- [ ] PR is focused — fewer than ~50 changed files (larger PRs will be asked to split)
- [ ] **Minimal necessary changes**: no unrelated refactoring, no cosmetic formatting of untouched code, no unrequested documentation files

### Documentation Requirements

Documentation scales with the change type:

| Change Type | Required Docs |
|-------------|---------------|
| Bug fix | Descriptive commit message only |
| Feature / API change | `docs/memory/architecture.md` and/or `docs/memory/feature-flows/*.md` as needed |
| New capability | `docs/memory/requirements/` (via the `docs/memory/requirements.md` index) **+** a feature flow |
| Refactor | Descriptive commit message only (unless it changes architecture) |
| Docs only | No additional docs needed |

Specifically:

- **New/changed API endpoints, DB schema, or integrations** → update `docs/memory/architecture.md` (endpoint tables, schema section)
- **New or changed feature behavior** → create/update the matching `docs/memory/feature-flows/*.md` and list new flows in the `docs/memory/feature-flows.md` index. Follow the section structure of existing flow docs (Overview, User Story, Entry Points, Frontend Layer, Backend Layer, Side Effects, Error Handling, Security Considerations, Testing, Related Flows)
- **New capability** → add a requirements entry **before** implementing (Requirements-Driven Development is a project rule)

### Test Expectations

- **Feature / refactor PRs** — each new surface needs a **non-happy-path test**, not just the success case: a new endpoint gets a non-admin/ownership-scoped call and a parameter-validation check; a new background service gets a restart-survival test; new WebSocket/session state gets a multi-worker assertion. Happy-path-only coverage on a new surface will be flagged.
- **Bug-fix PRs** — include a **regression test that names the issue** (e.g. `test_1234_...` or a docstring referencing `#1234`) and cover every call-site of the fixed behavior, not just the reported path.

### Security Checklist (hard blockers)

This is a **public repository** — every diff is scanned, and any of these blocks the merge:

- [ ] No API keys or tokens (`sk-`, `ghp_`, `xoxb-`, `AKIA…`, etc.)
- [ ] No real email addresses — use placeholders like `user@example.com`
- [ ] No public IP addresses or internal URLs/domains
- [ ] No `.env` files with real values (`.example` templates are fine)
- [ ] No hardcoded secrets — read from `process.env` / `os.environ` instead
- [ ] No credential files (`.pem`, `.key`, `id_rsa`, service-account JSON, etc.)
- [ ] `docker-compose*.yml` / `Dockerfile` changes are justified in the PR description

### Packaging Gotchas (recurring failure classes)

These have each broken deploys before, so validation checks them explicitly:

- **New top-level backend module** — `docker/backend/Dockerfile` copies top-level `src/backend/*.py` files by explicit name (subdirectories like `routers/`, `services/`, `db/` are copied wholesale). If you add a new top-level module, add it to the Dockerfile `COPY` list or it's silently dropped from the image and crashes on deploy.
- **New environment variable** — a new `os.getenv("X")` in the backend must be wired into `backend.environment:` in **both** `docker-compose.yml` and `docker-compose.prod.yml`, and documented in `.env.example`. Prod compose launches standalone (no `env_file:`), so dev-only wiring leaves the setting inert on deploy.
- **DB schema change** — Trinity runs dual-track migrations: add **both** a SQLite migration (`src/backend/db/migrations.py`) **and** a PostgreSQL Alembic revision (`src/backend/migrations/versions/`), plus the DDL update in `src/backend/db/schema.py` / `db/tables.py`. The `schema-parity` CI check guards part of this.

### CI Checks

Branch protection requires these checks green before merge:

| Required check | What it does |
|----------------|--------------|
| `Analyze (python)` / `Analyze (javascript-typescript)` | CodeQL static analysis on every PR |
| `schema-parity` | SQLite schema ↔ migration parity (self-skips when no schema files change) |
| `verify-non-root` | Container security: non-root UID guard (self-skips when no Docker surface changes) |

Other workflows (backend unit tests, frontend build, image smoke tests) run on PRs and are informational but reviewers expect them green.

### After You Open the PR

1. A maintainer reviews the code and runs the validation pass, resulting in **APPROVE**, **REQUEST CHANGES** (with a concrete fix list), or **NEEDS DISCUSSION** (scope/architecture questions)
2. Address requested changes and push to the same branch — re-review happens on the same PR
3. On approval the PR is **squash-merged to `dev`**; automation moves the linked issue to `status-in-dev`
4. Your change ships to `main` at the next release cut — the release PR closes the issue

## Development Setup

### Prerequisites

- Docker and Docker Compose v2+
- Node.js 20+ (for frontend development)
- Python 3.11+ (for backend development)

### Local Development

```bash
# 1. Clone your fork
git clone https://github.com/YOUR_USERNAME/trinity.git
cd trinity

# 2. Configure environment
cp .env.example .env
# Edit .env with required values

# 3. Build base image
./scripts/deploy/build-base-image.sh

# 4. Start services
./scripts/deploy/start.sh

# 5. Access the platform
# Web UI: http://localhost
# API: http://localhost:8000/docs
```

### Running Tests

```bash
# Backend tests
cd tests
python -m pytest -v

# Frontend (if applicable)
cd src/frontend
npm run test
```

## Code Standards

### Python (Backend)

- Follow PEP 8
- Use type hints
- Document public functions with docstrings
- Keep functions focused and small

### TypeScript/JavaScript (Frontend, MCP Server)

- Use TypeScript for new code
- Follow existing code style
- Use meaningful variable names
- Add comments for complex logic

### Vue.js (Frontend)

- Use Composition API
- Follow Vue.js style guide
- Keep components focused
- Use Pinia for state management

## Project Structure

```
trinity/
├── src/
│   ├── backend/          # FastAPI - Python
│   ├── frontend/         # Vue.js 3 - TypeScript
│   ├── mcp-server/       # MCP Server - TypeScript
│   └── audit-logger/     # Audit Service - Python
├── docker/
│   ├── base-image/       # Agent base image
│   └── ...               # Service Dockerfiles
├── config/               # Configuration files
├── docs/                 # Documentation
└── tests/                # Test suite
```

## Areas for Contribution

### Good First Issues

Look for issues labeled `good first issue` - these are suitable for newcomers.

### Feature Development

- Agent template improvements
- UI/UX enhancements
- MCP tool additions
- Documentation improvements
- Test coverage

### Documentation

- Improve existing docs
- Add examples and tutorials
- Fix typos and clarify language
- Translate to other languages

## Questions?

- Open a [Discussion](https://github.com/abilityai/trinity/discussions) for questions
- Join our community (link coming soon)
- Email: [hello@ability.ai](mailto:hello@ability.ai)

## Recognition

Contributors will be recognized in:
- GitHub contributors list
- Release notes for significant contributions
- Special thanks section (for major features)

Thank you for contributing to Trinity!
