# AutoHunt repository workflow

## Standing user request

The user requested that project changes be committed and pushed to their GitHub
account, `7r1nity`, after each completed implementation. Prefer an existing
AutoHunt repository owned by that account. If no matching repository exists,
create `AutoHunt` as a private repository unless the user requests public visibility.
The account's repositories page is not a Git remote URL.

## Delivery

- Inspect the local branch, remote, status and applicable instructions first.
- Verify changes with relevant tests before committing and pushing.
- Stage only intended source, tests, configuration templates and documentation.
- Never commit `.env` secrets, resumes, application/chat history, databases,
  runtime backups, screenshots or browser login/profile data. Preserve them locally.
- Do not overwrite existing remote history or force-push. If authentication,
  branch protection or conflicts block delivery, report the blocker and preserve
  local changes. Do not claim a push succeeded without checking it.
- Do not start recruitment searches or submit applications just to verify Git delivery.
- This agreement covers AutoHunt's workspace, not the separate mock site's checkout.
- Per the user's updated request, use `Codex <noreply@openai.com>` as both
  author and committer for future Codex-created commits in this repository.
  GitHub has verified this email maps to the `codex` account and its avatar.
  Keep this identity in repository-local Git configuration, not global settings.
  Push using the user's existing `7r1nity` authentication; commit attribution does
  not change the authenticated pusher or grant Codex repository access.
  Do not rewrite historical commits or add a redundant Codex co-author trailer.

## Implementation

Use the existing rule → small model → main model routing and structured session
state. Shared action semantics must agree with tool authorization. Keep real-site
submission disabled; local mock submission must retain preview, one-use authority,
resume binding, verification takeover and uncertain-outcome protection.
