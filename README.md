# forgewright

**A self-hosted, platform- and agent-agnostic coding bot.** Mention
`@forgewright` on a GitLab issue or a GitHub pull request, and the bot opens a
Draft MR/PR with working code, answers questions inline, or posts a full code
review with line-level comments. Runs either Claude Code or OpenCode under
the hood, against either GitLab or GitHub — pick your combination in one
line of YAML.

```yaml
platform_type: "github"     # or "gitlab"
agent_type:    "claude"     # or "opencode"
bot_username:  "forgewright"
```

That's the whole switch. The rest of the system — the webhook server, the
polling loop, the worktree isolation, the prompt templates, the threaded
reply parsing — is shared across every combination.

---

## Multi-platform: GitLab and GitHub, same code path

Both platforms are first-class. They are not two separate tools glued
together — they implement the same 22-method `Platform` abstract base class
(`forgewright/platform/base.py`) and plug into the same pipeline.

|                              | **GitLab** | **GitHub** |
|------------------------------|:---:|:---:|
| Issues — react to `@mention` | ✅ | ✅ |
| Draft merge/pull requests    | ✅ | ✅ |
| Threaded discussion replies  | ✅ | ✅ |
| Inline code-review comments  | ✅ | ✅ |
| Pipeline / Check run context | ✅ | ✅ |
| Webhook signature validation | `X-Gitlab-Token` | HMAC-SHA256 |
| Self-hosted support          | Any GitLab instance | GHE + github.com |

Want to run it against your self-hosted GitLab **and** your public-facing
GitHub organization? Run two instances of forgewright with two config files
— same binary, same systemd unit template, same logs.

Adding a third platform (Forgejo, Gitea, Bitbucket) is a single-file task:
implement `Platform` in `forgewright/platform/<name>.py`, register it in
`forgewright/platform/__init__.py:create_platform()`, done. See
[docs/contributing.md](docs/contributing.md).

---

## Multi-agent: Claude Code or OpenCode, same prompt pipeline

The agent is equally pluggable. Both implementations live behind a one-method
interface (`forgewright/agent/base.py`):

```python
class Agent:
    def run(self, prompt: str, cwd: Path) -> AgentResult: ...
```

|                                 | **Claude Code** | **OpenCode** |
|---------------------------------|:---:|:---:|
| Local CLI dispatch              | `claude -p …` | `opencode --non-interactive --prompt …` |
| Works in a dedicated worktree   | ✅ | ✅ |
| Honours `claude_timeout_sec`    | ✅ | ✅ |
| Writes `.claude/last-run-summary.md` | ✅ | ✅ |
| Model override via config       | `claude_model` | `opencode_model` |
| Auth                            | `claude login` (Max OAuth) or `ANTHROPIC_API_KEY` | whatever provider key OpenCode needs |

Same prompts, same structured summary format, same posting logic. The agent
just runs in a sandboxed worktree and writes its output — forgewright reads
the summary and turns it into real comments, replies, and commits.

Swapping agents is one line:

```yaml
# Claude Code
agent_type: "claude"
claude_binary: "/usr/local/bin/claude"
claude_model: "claude-sonnet-4-6"    # optional

# OpenCode
agent_type: "opencode"
opencode_binary: "/usr/local/bin/opencode"
opencode_model: null                  # optional
```

Adding a third agent (Aider, a custom local model, a hosted endpoint) is
again a single-file task: implement `Agent`, register it in the factory,
done.

---

## What it does

Three trigger paths, all driven by the same `@<bot_username>` mention and
the same git-worktree pipeline:

- **Issue mention** — `@forgewright please add X` on an issue → agent works
  on a scratch branch, opens a **Draft MR/PR** with a full description, or
  replies inline if the user just asked a question.
- **MR/PR update** — a new comment, label change, failing pipeline, or new
  commit on a bot-authored MR/PR re-runs the agent; replies land as
  **threaded discussions** instead of generic top-level comments.
- **MR/PR review** — mention `@forgewright` on an MR/PR *not* authored by
  the bot and it will produce a thorough code review with **inline comments**
  on specific files and lines.

### Other highlights

- **Hybrid polling + webhook** — webhooks for sub-second response, polling as
  a safety net. Both run in parallel safely thanks to file locking and
  fingerprinting.
- **Language matching** — the agent replies in whatever language the user
  wrote in.
- **Configurable mention** — `bot_username: jarvis` ⇒ reacts to `@jarvis`
  instead. The name `forgewright` is just the default.
- **Hardened defaults** — Draft MRs/PRs only, `forgewright/…` branch prefix,
  per-branch locks, agent timeout, tokens never in git config, hardened
  systemd units.

---

## Quickstart

Native install (systemd + venv):

```bash
git clone https://github.com/Marcel2508/forgewright.git
cd forgewright
sudo bash install.sh
sudo systemctl edit forgewright.service
# Environment=PLATFORM_TOKEN=glpat-xxxx      (or ghp_xxxx / github_pat_xxxx)
sudo -u forgewright /opt/forgewright/venv/bin/python -m forgewright --dry-run -v
```

Docker install (all-in-container — no host systemd or cron):

```bash
bash install.docker.sh --profile claude     # or --profile opencode
# then edit /opt/forgewright/config.yaml and /opt/forgewright/.env
```

The installer pulls the prebuilt image from GHCR
(`ghcr.io/marcel2508/forgewright-{claude,opencode}:latest`) and brings up
two long-running containers per profile — a webhook receiver and a poller
with supercronic inside it.

Full setup — picking platform, picking agent, configuring tokens, enabling
the webhook server — is covered in
**[docs/installation.md](docs/installation.md)**.

## Documentation

- **[Architecture](docs/architecture.md)** — package layout, the two
  abstractions (`Platform`, `Agent`), data flow, decision logic.
- **[Installation](docs/installation.md)** — native and Docker installs,
  GitLab and GitHub setup, Claude Code and OpenCode setup, webhook setup,
  updates, uninstall.
- **[Configuration](docs/configuration.md)** — every config key with
  defaults and tuning guidance.
- **[Contributing](docs/contributing.md)** — dev setup, tests, and how to
  add a new platform or agent (both are one-file changes).

## Trigger rules (why the mention matters)

The mention string is just `@<bot_username>` from your config. The examples
in this README use `@forgewright`, but if you set `bot_username: jarvis`
the bot reacts to `@jarvis` instead — nothing is hard-coded.

To avoid runaway auto-coding the bot **only acts** when:

- **Issue** — `@<bot_username>` appears in the description or a note (by
  anyone other than the bot itself), *and* something changed since the last
  run.
- **MR/PR (own)** — source branch starts with `branch_prefix` (default
  `forgewright/`). First-seen MRs/PRs only trigger on real human activity;
  later polls need an actual fingerprint delta.
- **MR/PR (review)** — `@<bot_username>` appears in the description or notes
  of an MR/PR *not* authored by the bot.
- **MR/PR (assigned/reviewer)** — the bot has been added as reviewer or
  assignee.

The fingerprint is
`(updated_at, last_note_id, labels, pipeline_sha, pipeline_status, head_sha)`,
computed from **human notes only** — the bot's own replies are excluded to
prevent self-triggering loops.

## Typical workflow

1. You open an issue and write `@forgewright please fix …`.
2. Within seconds (webhook) or 5 minutes (polling), the bot clones the repo,
   runs the agent, pushes a `forgewright/issue-42-…` branch, and opens a
   Draft MR/PR titled `Draft: <title> (#42)` that closes the issue and
   explains what it changed, why, and how to test.
3. You review the diff and leave comments — including inline comments on
   specific lines. The next poll/event re-runs the agent with that feedback
   and posts replies directly in the discussion threads. If code changes
   were requested, it pushes new commits.
4. If you just asked a question, the bot answers inline without making
   unnecessary code changes.
5. Mention `@forgewright` on any MR/PR for a code review.
6. When you're happy, mark the MR/PR Ready and merge. The
   `forgewright/…` branch is deleted automatically.

## Safety

- Draft MRs/PRs only — nothing can auto-merge.
- Branches are `forgewright/…`-prefixed; the bot never pushes to protected
  branches. Add branch protection for extra defence.
- Per-branch lockfile prevents two agents on the same worktree.
- Global poller lockfile prevents overlapping poll cycles.
- Every agent run is capped at `claude_timeout_sec` (default 1 h) — applies
  to both Claude Code and OpenCode.
- Tokens are passed via `GIT_ASKPASS` — never in git config or error
  messages.
- Bot-authored notes are excluded from fingerprints, so the bot cannot
  trigger itself.
- Systemd units run with hardened flags (`ProtectSystem=strict`,
  `ProtectHome=tmpfs`, `PrivateTmp=true`, `NoNewPrivileges=true`).

`claude --dangerously-skip-permissions` is acceptable here because the agent
runs in a throwaway worktree owned by a dedicated system user inside the
target host. It can touch the repo files the worktree contains; it cannot
reach files outside its workdir. The same isolation applies when running
OpenCode.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

Issues and MRs welcome. See
**[docs/contributing.md](docs/contributing.md)** for dev setup, test
commands, and guidance on adding new platforms, agents, or prompt changes.
