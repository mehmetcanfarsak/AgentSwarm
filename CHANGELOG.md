# Changelog

All notable changes to Agentainer are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
