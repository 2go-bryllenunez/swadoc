# Swadoc — Swagger Auto-Documentation Script

Swadoc crawls a target codebase's registered API routes, evaluates the quality of existing OpenAPI/Swagger annotations, enriches underdocumented routes using an LLM, generates a valid OpenAPI 3.0 specification, and optionally opens a GitHub pull request for review.

**Supported frameworks:** Laravel (PHP), AdonisJS (Node.js), Express (Node.js)

**Supported LLM providers:** AWS Bedrock (Claude), Anthropic (direct), OpenAI

---

## Requirements

- Python 3.14+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- For Laravel projects: PHP + Composer + L5-Swagger installed in the target project
- For AdonisJS/Express projects: Node.js installed

---

## Installation

### Using uv (recommended)

```bash
git clone https://github.com/your-org/swadoc.git
cd swadoc
uv sync --extra dev
```

### Using pip

```bash
git clone https://github.com/your-org/swadoc.git
cd swadoc
pip install -e .
```

---

## Configuration

Swadoc reads configuration from a `.env` file in the current working directory, or from environment variables. CLI flags always take precedence over environment variables.

Create a `.env` file in the `swadoc` directory (copy the template below):

### AWS Bedrock (recommended)

```env
# LLM Provider
LLM_PROVIDER=bedrock
LLM_MODEL=anthropic.claude-3-5-sonnet-20241022-v2:0

# AWS credentials
AWS_ACCESS_KEY_ID=your_access_key_id
AWS_SECRET_ACCESS_KEY=your_secret_access_key
AWS_REGION=us-east-1

# GitHub — only needed when using --open-pr
# GITHUB_TOKEN=ghp_...
# GITHUB_REPO=owner/repo
```

Available Bedrock model IDs:
| Model | ID |
|---|---|
| Claude 3.5 Sonnet v2 | `anthropic.claude-3-5-sonnet-20241022-v2:0` |
| Claude 3 Haiku | `anthropic.claude-3-haiku-20240307-v1:0` |
| Claude 3 Opus | `anthropic.claude-3-opus-20240229-v1:0` |

### Anthropic (direct API)

```env
LLM_PROVIDER=anthropic
LLM_MODEL=claude-3-5-sonnet-20241022
LLM_API_KEY=sk-ant-...
```

### OpenAI

```env
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o
LLM_API_KEY=sk-...
```

### All supported environment variables

| Variable | Required | Description |
|---|---|---|
| `LLM_PROVIDER` | Yes | `bedrock`, `anthropic`, or `openai` |
| `LLM_MODEL` | Yes | Model identifier for the chosen provider |
| `LLM_API_KEY` | Anthropic/OpenAI only | API key |
| `AWS_ACCESS_KEY_ID` | Bedrock only | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | Bedrock only | AWS secret key |
| `AWS_REGION` | Bedrock only | AWS region (e.g. `us-east-1`) |
| `GITHUB_TOKEN` | Only with `--open-pr` | GitHub personal access token |
| `GITHUB_REPO` | Only with `--open-pr` | Repository in `owner/repo` format |
| `PROJECT_PATH` | No | Default project path (overridden by `--project-path`) |
| `OUTPUT_PATH` | No | Default output path (overridden by `--output-path`) |

---

## Usage

### Basic syntax

```bash
swadoc --code-base <php|node> --project-path <path> [options]
```

### Options

| Flag | Description |
|---|---|
| `--code-base` | Target framework language: `php` (Laravel) or `node` (AdonisJS/Express) |
| `--project-path` | Path to the root of the target codebase |
| `--output-path` | Output path for the generated OpenAPI document (default: `./openapi.json`) |
| `--dry-run` | Evaluate and enrich routes but write no files and open no PRs |
| `--open-pr` | Create a GitHub pull request with all changes after the run |
| `--help` | Show help and exit |

### Examples

**Laravel project, output to JSON:**
```bash
swadoc --code-base php --project-path /var/www/my-laravel-app
```

**AdonisJS project, custom output path:**
```bash
swadoc --code-base node --project-path /home/user/my-adonis-app --output-path ./docs/openapi.yaml
```

**Express project, dry run (no files written):**
```bash
swadoc --code-base node --project-path ./my-express-app --dry-run
```

**Laravel project, open a GitHub PR after enrichment:**
```bash
swadoc --code-base php --project-path /var/www/my-laravel-app --open-pr
```

### Running with uv (without installing globally)

```bash
uv run swadoc --code-base php --project-path /var/www/my-laravel-app
```

---

## Output

After a successful run, Swadoc produces:

- **OpenAPI document** at the configured output path (default `./openapi.json`)
- **Run report** at `./swagger-autodoc-report.json` — structured JSON with per-route details
- **Stdout summary** — counts of discovered, documented, and enriched routes, plus validation results
- **Backup directory** at `./autodoc-backup/` — original copies of all modified source files

### Example stdout summary

```
Routes discovered: 24
Routes documented: 18
Routes enriched: 6
Validation warnings: 0
Validation errors: 0
Output path: /home/user/my-app/openapi.json
PR URL: none
```

### Run report structure (`swagger-autodoc-report.json`)

```json
{
  "summary": {
    "start_timestamp": "2024-01-15T10:30:00Z",
    "duration_ms": 45230,
    "routes_discovered": 24,
    "routes_documented": 18,
    "routes_enriched": 6,
    "validation_warnings": 0,
    "validation_errors": 0
  },
  "routes": [
    {
      "method": "GET",
      "uri": "/api/users",
      "handler": "UserController@index",
      "classification": "Documented_Route",
      "enrichment_outcome": "not_attempted",
      "validation_warnings": [],
      "validation_errors": [],
      "missing_fields": []
    }
  ],
  "dry_run": false,
  "output_path": "/home/user/my-app/openapi.json",
  "pr_url": null
}
```

---

## How it works

1. **Discover** — invokes the framework's native route listing command (`php artisan route:list --json` for Laravel, `node ace route:list --json` for AdonisJS, or a static AST crawl for Express)
2. **Evaluate** — reads each handler's source code and checks whether its existing annotation meets all quality criteria (summary, operationId, responses, request body, headers, path params)
3. **Enrich** — sends underdocumented routes to the LLM with the handler source as context; the LLM returns a structured PHPDoc or JSDoc annotation block
4. **Write** — inserts the annotation immediately before the handler function, preserving indentation and line endings; backs up each file before modification
5. **Generate** — invokes the framework's native Swagger generator (L5-Swagger or swagger-jsdoc) to produce the OpenAPI document
6. **Validate** — validates the generated document against the OpenAPI 3.0 schema
7. **Report** — prints a summary to stdout and writes the JSON run report
8. **PR** *(optional)* — creates a branch, commits changes, and opens a GitHub pull request

---

## Rollback

If anything goes wrong after files have been modified, restore originals from the backup directory:

```bash
# Restore a single file
cp autodoc-backup/app/Http/Controllers/UserController.php \
   /var/www/my-laravel-app/app/Http/Controllers/UserController.php

# Restore all files (bash)
cp -r autodoc-backup/. /var/www/my-laravel-app/
```

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success — zero validation errors |
| `1` | Failure — validation errors, config error, or unhandled exception |

---

## Development

```bash
# Run tests
uv run pytest

# Run a specific test file
uv run pytest tests/test_config.py -v

# Check imports
uv run python -c "from swadoc.cli import main; print('OK')"
```
