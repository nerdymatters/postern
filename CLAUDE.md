## gstack (recommended)

This project uses [gstack](https://github.com/garrytan/gstack) for AI-assisted workflows.
Install it for the best experience:

```bash
git clone --depth 1 https://github.com/garrytan/gstack.git ~/.claude/skills/gstack
cd ~/.claude/skills/gstack && ./setup --team
```

Skills like /qa, /ship, /review, /investigate, and /browse become available after install.
Use /browse for all web browsing (Aside first, the bundled gstack browser as fallback). Use ~/.claude/skills/gstack/... for gstack file paths.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
- Author a backlog-ready spec/issue → invoke /spec

## Testing

- Python: `.venv/bin/python -m pytest tests -q` (pytest; `tests/postern/` is the application suite, `tests/day1/` the measurement harness). Always `.venv/bin/python`, never bare `python3`.
- SPA: `cd dashboard/web && bun run test` (Vitest) and `bun run build` (type-check + Vite build). Use Bun for the frontend.
- Status Worker: `cd status && bun install --frozen-lockfile && bun test`. Wrangler requires Node 24; see [local status checks](docs/runbooks/status.md#local-checks) for the office Mac runtime fallback.
- CI runs Python, SPA and status Worker checks in `.github/workflows/tests.yml` (pytest on macOS because the converter needs `sandbox-exec`; the status job also runs a Wrangler deployment dry run).
