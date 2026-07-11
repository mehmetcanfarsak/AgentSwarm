# Contributing to Agentainer

Thanks for wanting to help! Agentainer stays useful by staying **small and
dependency-free**, so most contributions are about clarity and correctness, not
new moving parts. This guide covers what we expect.

## Principles

- **Zero runtime dependencies.** The orchestrator is Python 3 + bash + tmux. Do
  not add PyYAML or any other runtime package — the bundled `lib/minyaml.py`
  parser must keep working without it. Dev/test tooling is fine, but it must
  never be required to *run* a swarm.
- **stdlib-only code style.** Match the existing terse, focused style in
  `lib/` (`xx`/`!!`/`ok` status prefixes, small functions). No frameworks.
- **Don't lose the footguns.** The capture/type consistency rule, busy-agent
  backpressure, and resume behavior are subtle. If you touch those paths, keep
  the README's "gotchas" accurate — update docs in the same PR as the code.
- **No secrets.** Never commit API keys or agent `command` strings that embed
  them. Treat `command:` values as sensitive.

## Getting started

```bash
git clone https://github.com/mehmetcanfarsak/AgentSwarm.git && cd AgentSwarm
./agentainer --help

# a safe, key-free smoke test (mock agents, no model calls):
agentainer up    -c quickstart.yaml --no-prompt
agentainer status -c quickstart.yaml
agentainer send  -c quickstart.yaml --to orchestrator "say hello"
agentainer down  -c quickstart.yaml
```

## What to work on

- **Good first issues:** clearer error messages, more `validate` checks, example
  swarms for new shapes, docs/FAQ improvements.
- **Core work:** routing, capture modes, queue/backpressure, resume, the
  `minyaml` parser.
- **New agent types:** add a `command` + `capture` entry under `agent_types:`
  and document the capture reliability trade-off in the README.

## Before you open a PR

1. Run the full suite — it exercises the real tmux/hook/lock/queue paths with
   mock agents (no API key, nothing to pay for):

   ```bash
   tests/validate.sh
   ```

2. If you changed anything in `lib/` or `hooks/`, `tests/validate.sh` must pass.
3. Keep `README.md` honest: if behavior changes, update the config tables,
   examples matrix, or footgun notes in the same PR.
4. Validate any SVG you touched:

   ```bash
   for f in assets/*.svg; do
     python3 -c "import xml.dom.minidom; xml.dom.minidom.parse('$f')" && echo "OK $f"
   done
   ```

## Reporting bugs

Open an issue with: the config you ran, the `agentainer` command, the output of
`agentainer status` and `agentainer logs -n 30`, and — if capture misbehaves —
the contents of `.swarm/logs/hooks.log`. A minimal repro config beats a long
narrative.

## License

By contributing you agree your contributions are released under the project's
MIT license.
