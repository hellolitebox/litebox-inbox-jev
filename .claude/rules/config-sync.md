---
paths:
  - "categories.yaml"
  - "accounts.example.yaml"
  - ".env.example"
  - "docker-compose.yml"
  - "deploy/**"
  - "src/inbox_jev/config.py"
---

# Config and docs move together

- Keep README.md, `docs/operations.md`, the comments in `categories.yaml`, `accounts.example.yaml`
  and `docs/guide.md` (for what it explains, e.g. the digest's sections) in sync with behaviour in
  the same PR. `.env.example` lists every setting (a test checks it is complete).
- New categories or labels go in `categories.yaml`, not in code. A new `act_categories` key goes
  into `accounts.yaml` only after the code that knows it is deployed (`docs/operations.md` →
  "Rolling out a category change").
- Server config (`accounts.yaml`, `.env`) is not in git: a change that needs it says so in the PR's
  deploy notes, and the server copy is backed up before editing.
- After a deploy that changes `deploy/systemd/`, the units are copied and `daemon-reload` run: say
  so in the deploy notes.
