# Changelog

All notable changes to Agentainer are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [0.1.7]

### Fixed
- **Green CI.** The GitHub Actions pipeline had been failing on every run. The
  `test` job's `validate.sh` hard-required PyYAML for its parser-parity checks
  (a fresh runner has none); those now `SKIP` gracefully so a pure-stdlib clone
  passes, while the matrix installs PyYAML to actually run them. The `lint` job's
  18 ShellCheck findings in `validate.sh` are all resolved.

### CI
- Matrix installs PyYAML (test-only) so parser-parity checks run; a new
  stdlib-only job proves the zero-dependency `minyaml` fallback end to end.

## [0.1.6]

### Added
- `agentainer --version` / `agentainer -v` print the installed version (read from
  `package.json`, the single source of truth).

### Tests
- Cover the new `read_version` helper and version flag (415 pytest cases, 50
  `validate.sh` checks; `lib/` stays at 100% line coverage).

## [0.1.5]

### Added
- Background **liveness supervisor** (started at `up`; `swarm.supervise` /
  `supervise_interval_ms`, default 15s) that reconciles stale-busy and dead
  agents on a timer so one silent agent cannot wedge the swarm. `status` reports
  whether it is alive; `down` stops it.
- `up` now nudges that resumed agents sit idle until messaged.

### Changed
- `capture: none` on a hook-backed type (claude/codex) is auto-upgraded to
  `capture: hook` at load time (with a warning); gemini/hermes keep `none`.

### Fixed
- `resume_args` / `resume_command` now fall back through `defaults`, not just the
  agent/type.

### Tests
- Expanded the mock suite to keep `lib/` at 100% line coverage (411 pytest
  cases, 50 `validate.sh` checks).

## [0.1.4]

### Added
- `quickstart.yaml` — a key-free swarm (mock agents) so new users can feel the
  routing, status, and logs with no API keys.
- `CONTRIBUTING.md`, `SECURITY.md`, and GitHub issue templates.
- Marketing README: banner/architecture/demo/screenshot SVGs, keyword-rich
  intro, FAQ, and table of contents for discovery (SEO + LLM answer engines).

## [0.1.3]

### Added
- GitHub Actions CI (tests) and npm publish workflow.
- `agentainer doctor` dependency check (also run as npm `postinstall`).

### Fixed
- Stricter number coercion in the fallback YAML parser (`minyaml.py`).
- UTF-8-safe YAML unescaping and broadcast reply nags.

## [0.1.0]

### Added
- First tagged release: `up`/`down`/`status`/`send`/`broadcast`/`logs`/`queue`/
  `idle`/`inbox`/`sessions`/`validate`/`attach`/`restart`.
- Per-agent tmux sessions + working directories, `can_talk_to` ACL, tagged
  message envelopes, turn-completion capture (hook/pane/none), busy backpressure
  with queued delivery, auto-forwarding with hop guard, and `up --resume`.
