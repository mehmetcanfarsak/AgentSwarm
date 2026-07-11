# Security Policy

## Reporting a vulnerability

Agentainer launches other programs' CLIs and types prompts into them, so it sits
close to whatever privileges those agents run with. If you find a vulnerability —
especially anything that could let one agent reach another outside its
`can_talk_to` ACL, or execute untrusted input — please report it privately rather
than opening a public issue.

- **Email / private report:** open a [security advisory](https://github.com/mehmetcanfarsak/AgentSwarm/security/advisories/new) on the repository.
- Please include: the config you ran, the `agentainer` command, and the observed
  vs. expected behaviour. A minimal repro config is ideal.

## Safe operation guidance

- Agents run with whatever privileges their `command` is launched with. Flags like
  `claude --dangerously-skip-permissions`, `codex --yolo`, and `gemini --yolo`
  let agents act without confirmation — convenient for an unattended swarm, but it
  means several models are running tools unsupervised. **Point `root` at a
  disposable directory** and never run a swarm over a checkout you can't afford to
  lose.
- Do not embed API keys in committed example configs; treat `command:` strings as
  sensitive.
- Prefer `up --resume` and the bundled mock `quickstart.yaml` for evaluation
  before pointing real agents at real repositories.
