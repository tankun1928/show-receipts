# Launch & discoverability playbook

How people find a skill, and what moves each surface. Ranked by impact.

## 1. GitHub repo settings (do before the first post)

**Name:** `show-receipts` (short, memorable, says the idea: "show me the receipts" = prove it).

**About / description** (GitHub search weighs this heavily):

> 🧾 Proof-of-done for AI coding agents. Stops Claude Code, Codex & Cursor from claiming "tests pass" or "fixed" without evidence: hooks record what actually ran, a Stop hook checks every claim.

**Topics** (GitHub allows 20; these are the ones the skill directories and topic pages use):

```
claude-code  claude-skills  agent-skills  claude-code-skills  claude-code-plugin  skills
ai-agents  coding-agents  verification  hallucination  llm  guardrails  hooks
codex  cursor  gemini-cli  developer-tools  testing  ai-safety  trust
```

The topic pages `agent-skills`, `claude-skills` and `claude-code-skills` are sorted by stars and
are among the main ways people browse skills, so the first few dozen stars matter most.

**Social preview:** Settings → Social preview → upload `docs/social-preview.png` (1280×640). It
is the image shown on X, LinkedIn, Slack, Discord and Facebook when the link is shared.

**Release:** tag `v0.1.0` with a short changelog. Releases show up in watchers' feeds and in
"recently updated" sorts.

## 2. Install surfaces (each one is also a search index)

| Surface | How it gets listed | What ranks it |
|---|---|---|
| skills.sh / `npx skills` | automatic once someone runs `npx skills add OWNER/show-receipts` | install count (all-time and trending) |
| Claude Code `/plugin` marketplaces | `.claude-plugin/marketplace.json` (already in the repo) | installs; inclusion in popular marketplaces |
| Directory sites (claudemarketplaces.com, claudeskills.info, mcpmarket.com skills leaderboard) | most crawl GitHub topics; some take a submission form | stars, installs |
| In-agent triggering | the `description` in `SKILL.md` | match with what the user types |

Ask early users to install with `npx skills add` (it counts toward the skills.sh leaderboard), and
put that command first in every post.

## 3. Awesome lists (one PR each, the same day)

| List | Section to target |
|---|---|
| VoltAgent/awesome-agent-skills | Development / Code quality |
| ComposioHQ/awesome-claude-skills | Development & Code Tools |
| travisvn/awesome-claude-skills | Community skills |
| hesreallyhim/awesome-claude-code | Hooks (it's a hook + skill) |
| BehiSecc/awesome-claude-skills | Development |
| sickn33/agentic-awesome-skills | follow its submission format |

One-line entry (keep their format, don't oversell):

```markdown
- [show-receipts](https://github.com/OWNER/show-receipts) - Proof-of-done: hooks record every command and exit code, and a Stop hook checks the agent's "tests pass / fixed / committed" claims against them.
```

## 4. Launch posts

Lead with the failure everyone has seen, then the 10-second demo (the blocked-claim snippet at the
top of the README, or a GIF of it recorded with `vhs`).

- **Show HN:** `Show HN: show-receipts – make AI coding agents prove "tests pass"`
- **r/ClaudeAI, r/ClaudeCode, r/ChatGPTCoding, r/cursor:** title = the pain, not the tool:
  "My agent kept saying 'all tests pass' when they didn't, so I made it show receipts"
- **X / Threads:** demo GIF + "Your AI agent grades its own homework. This makes it show receipts."
  Tag the authors of skills it complements (superpowers, karpathy-skills): it's an add-on to
  their workflows, not a competitor.
- **Thai dev communities** (Facebook groups, Discord): post with `README.th.md`. Few skills have
  Thai docs, and the checker understands Thai claims.

## 5. Keep it ranked

- Answer false-positive issues fast and add each one as a test: reliability is the product.
- Ship small releases often; "recently updated" and trending lists reward activity.
- Add a "Used by" section once teams adopt it.

Don't buy stars or fake installs. It breaks GitHub's terms, directories delist for it, and a
skill about proof can't afford to be caught faking numbers.
