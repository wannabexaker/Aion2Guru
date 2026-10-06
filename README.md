# Aion2Guru

Discord AI **knowledge system** (όχι chatbot): η PostgreSQL είναι η μνήμη, το LLM είναι interpreter και μπαίνει τελευταίο στη σειρά.
Generic core (`guru`) με profiles· το πρώτο profile είναι το AION 2.

| Έγγραφο | Περιεχόμενο |
|---------|-------------|
| [docs/DESIGN.md](docs/DESIGN.md) | Architecture, data flow, lifecycle, routing, ingestion, FAQ, permissions, LLM strategy, security, testing |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Κλειδωμένες αποφάσεις, ασάφειες, ρίσκα |
| [docs/BACKLOG.md](docs/BACKLOG.md) | Tasks, dependencies, milestones |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Εγκατάσταση, μοντέλα, βαθμονόμηση, backups, troubleshooting, smoke checklist |

## Κατάσταση

| Milestone | Status |
|-----------|--------|
| M0 Foundations (DB, migrations, audit hash chain, job queue, runner) | ✅ |
| M1 Walking skeleton χωρίς AI (config/profiles, `/settings`, permissions, access enforcement, rate limits, `/kb`, `/ask`, mentions, FTS retrieval) | ✅ |
| M2 Discord ingestion (home channel, 📌, «Add to knowledge», `μάθε:`), Ollama extraction με validators, embeddings dedupe, conflicts, edit/delete sync, moderator review channel, learned gate | ✅ |
| M3 Vector retrieval (FTS → trigram → vector, RRF), LLM σύνθεση μόνο όταν χρειάζεται με grounding checks και fallback, conflicts με όλες τις πλευρές, answer cache (epoch/visibility), LLM quota, follow-ups | ✅ |
| M4 Web: SSRF-safe fetcher, robots, conditional GET, extraction/dates/near-dup/chunking, web claims, liveness, SearxNG discovery (budget, κενά γνώσης), source reputation, `/kb ingest-url` | ✅ |
| M5 FAQ: candidates (verified/δημοφιλή), draft (LLM με grounding ή template), έγκριση στο review channel (quorum, four-eyes), forum/text reconciler, banners/deprecation όταν αλλάζει η γνώση, FAQ-first απαντήσεις, `/faq` | ✅ |
| M6 Hardening: `guru doctor`, `guru eval`, security suite, `/kb forget-me`, backups, [runbook](docs/RUNBOOK.md) | ✅ |

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
5. Χρήση:
   - `@bot ερώτηση` οπουδήποτε ή `/ask` → απάντηση μόνο από τη βάση, με πηγές και κατάσταση (✅/☑️/⚠️/⚔️).
   - Ό,τι γράφεται στο **home channel** διαβάζεται· τα χρήσιμα εξάγονται από το local LLM και πάνε για έγκριση
     στο **review channel** (✅ Keep / ❌ Reject / ✏️ Edit). Οι απαντήσεις του bot βαθμολογούνται εκεί (👍/👎).
   - Γνώση από την ομάδα: `@bot μάθε: …`, 📌 σε οποιοδήποτε μήνυμα (trusted), δεξί κλικ → Apps → **Add to knowledge**,
     `/kb add`. Από trusted μέλη καταχωρείται ως επιβεβαιωμένη.
   - `/kb show K-12` (πηγές), `/kb verify|retract|obsolete`, `/kb ingest-url`, `/admin learning`.
   - FAQ: επιβεβαιωμένη/δημοφιλής γνώση → draft → έγκριση στο review channel → δημοσίευση στο FAQ channel
     (forum συνιστάται). `/faq ask` απαντά μόνο από εγκεκριμένο FAQ· `/faq create K-12`.

**Μοντέλα (guru.yaml):** οποιοδήποτε Ollama μοντέλο ανά task (π.χ. `gpt-oss:20b`, `hermes3`). Embeddings μέσω Ollama
(`bge-m3`, multilingual). Με 16 GB VRAM το 20B μοντέλο + bge-m3 χωράνε οριακά· αν όχι, μικρότερο embedding μοντέλο.
Η αυτόματη απόφαση «κρατάμε/όχι» ενεργοποιείται μόνο όταν, με αρκετές αποφάσεις moderators, η μετρημένη ακρίβεια
ξεπεράσει το όριο (`review.claim_keep.auto` στο config).

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
guru doctor                                   # έλεγχος DB/Discord/Ollama/embeddings/SearxNG/profile
guru eval extraction eval/extraction.example.yaml   # μέτρηση με τα πραγματικά μοντέλα
guru eval queries eval/queries.example.yaml
```

## Development

```bash
make sync
make testdb-up        # Postgres + pgvector σε Docker (ή GURU_TEST_DATABASE_URL σε υπάρχοντα server)
make check            # ruff + mypy (strict) + pytest (unit + integration σε πραγματικό Postgres)
```
