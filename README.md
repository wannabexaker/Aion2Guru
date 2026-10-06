# Aion2Guru

Discord AI **knowledge system** (όχι chatbot): η PostgreSQL είναι η μνήμη, το LLM είναι interpreter και μπαίνει τελευταίο στη σειρά.
Generic core (`guru`) με profiles· το πρώτο profile είναι το AION 2.

| Έγγραφο | Περιεχόμενο |
|---------|-------------|
| [docs/DESIGN.md](docs/DESIGN.md) | Architecture, data flow, lifecycle, routing, ingestion, FAQ, permissions, LLM strategy, security, testing |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Κλειδωμένες αποφάσεις, ασάφειες, ρίσκα |
| [docs/BACKLOG.md](docs/BACKLOG.md) | Tasks, dependencies, milestones |

## Κατάσταση

| Milestone | Status |
|-----------|--------|
| M0 Foundations (DB, migrations, audit hash chain, job queue, runner) | ✅ |
| M1 Walking skeleton χωρίς AI (config/profiles, `/settings`, permissions, access enforcement, rate limits, `/kb`, `/ask`, mentions, FTS retrieval) | ✅ |
| M2 Discord ingestion + local LLM extraction + moderator review | ⏳ |

## Εγκατάσταση (ένας host, Docker)

1. **Discord Developer Portal** → New Application → Bot:
   - Ενεργοποίησε **Message Content Intent** (privileged).
   - Invite με scopes `bot applications.commands` και permissions: View Channels, Send Messages,
     Read Message History, Embed Links, Add Reactions, **Manage Messages** (για τη διαγραφή μηνυμάτων
     χρηστών χωρίς πρόσβαση), Create Public Threads, Send Messages in Threads.
2. Ρυθμίσεις:
   ```bash
   cp .env.example .env              # GURU_DISCORD_TOKEN, GURU_HASH_SALT, POSTGRES_PASSWORD
   cp guru.example.yaml guru.yaml    # guild_ids (άμεσο sync των commands), Ollama URL, μοντέλα
   ```
3. `docker compose up -d --build` (το Ollama τρέχει στον host· το container το βρίσκει ως `host.docker.internal`).
4. Στο Discord (ως server Administrator):
   - `/setup template:aion2` → δημιουργεί το profile.
   - `/settings` → ποιοι μιλούν στο AI, contributors, moderators, admins, trusted ρόλοι, home channel,
     moderator review channel, τι γίνεται με μηνύματα χωρίς πρόσβαση, όρια χρήσης.
5. Χρήση: `@bot ερώτηση` οπουδήποτε, `/ask`, `/kb add` (γνώση από την ομάδα), `/kb show K-12`.

Health/metrics: `http://127.0.0.1:8080/healthz`, `/readyz`, `/metrics` (μόνο localhost).

## CLI

```bash
guru migrate                                  # εφαρμογή migrations
guru run --roles bot,worker,api               # ή ξεχωριστά roles ανά process
guru config validate src/guru/profiles/aion2.yaml
guru config apply profile.yaml                # diff + επιβεβαίωση + νέα version
guru config export aion2 > aion2.yaml
guru config rollback aion2 3
guru config import-entities aion2 items.csv   # entity_type,canonical_name,aliases(|),category
guru jobs [--retry ID]
guru audit [--verify]
```

## Development

```bash
make sync
make testdb-up        # Postgres + pgvector σε Docker (ή GURU_TEST_DATABASE_URL σε υπάρχοντα server)
make check            # ruff + mypy (strict) + pytest (unit + integration σε πραγματικό Postgres)
```
