# Runbook

## 1. Πρώτη εγκατάσταση

```bash
cp .env.example .env && cp guru.example.yaml guru.yaml     # token, salt, Ollama URL, μοντέλα, guild_ids
ollama pull gpt-oss:20b && ollama pull bge-m3              # ή ό,τι ορίσεις στο guru.yaml
docker compose up -d --build                                # postgres + guru + searxng
docker compose exec guru guru doctor                        # ✅/⚠️/❌ με το τι να διορθώσεις
```

Στο Discord (server Administrator): `/setup template:aion2` → `/settings`:

| Ρύθμιση | Τι κάνει |
|---------|----------|
| 🤖 Who can talk to the AI | Ρόλοι με πρόσβαση· οι υπόλοιποι: τα μηνύματά τους σβήνονται (ρυθμίζεται) |
| ➕ Contributors | `μάθε:`, 📌, «Add to knowledge», `/kb add`, `/kb ingest-url` |
| 🛡️ Moderators | Ψήφοι στο review channel, verify/retract, FAQ έγκριση, χωρίς rate limits |
| ⚙️ Admins | `/settings`, πηγές, δικαιώματα |
| ⭐ Trusted | Η πληροφορία τους μετρά ως αξιόπιστη (tier 3)· 📌 από trusted = επιβεβαίωση |
| 🏠 Home / 🧾 Review / 📚 FAQ channels | Πού διαβάζει, πού ρωτά την ομάδα, πού δημοσιεύει FAQ (forum) |
| 🚫 / ⏱️ | Τι γίνεται με μη εξουσιοδοτημένους, όρια χρήσης |

**Discord permissions του bot:** View Channel, Send Messages, Read Message History, Embed Links, Add Reactions,
**Manage Messages** (διαγραφή μη εξουσιοδοτημένων), και στο FAQ forum: Create Posts, Send Messages in Threads,
Manage Threads (lock/archive). Developer Portal → **Message Content Intent** ενεργό.

## 2. Μοντέλα (RTX 5070 Ti 16 GB / 32 GB RAM)

- Ένα generative μοντέλο ~20B για όλα τα tasks (`guru.yaml → llm_tasks`). Αλλαγή μοντέλου = αλλαγή στο yaml + restart.
- Πριν αλλάξεις μόνιμα μοντέλο: `guru eval extraction eval/…yaml` και `guru eval queries eval/…yaml` με κάθε υποψήφιο
  και σύγκρινε recall/precision/latency. Φτιάξε τα δικά σου eval αρχεία από πραγματικά (ανωνυμοποιημένα) μηνύματα.
- Αν το VRAM δεν φτάνει για LLM + embeddings: μικρότερο embedding μοντέλο ή `num_ctx` μικρότερο στο task.
- **Αλλαγή embedding μοντέλου:** άλλαξε `embeddings.model/dims` → restart. Το `knowledge.embed` (κάθε 10′) ξαναϋπολογίζει
  όλα τα claims με το νέο μοντέλο· το παλιό αποσύρεται αυτόματα. Το learned gate επανεκπαιδεύεται μόνο με labels
  της νέας διάστασης (θα χρειαστούν νέες αποφάσεις moderators).

## 3. Βαθμονόμηση (μετά από ~1–2 εβδομάδες χρήσης)

| Ρύθμιση (profile config) | Πότε την αλλάζεις |
|--------------------------|-------------------|
| `search.vector_min_similarity` | Πολλά άσχετα αποτελέσματα → αύξησε· «δεν ξέρω» σε παραφράσεις → μείωσε |
| `search.min_coverage` | Ίδιο, για λεξικό ταίριασμα |
| `search.dedupe.tau_high/tau_low` | Διπλότυπα claims → μείωσε `tau_high` |
| `ingestion.prefilter.threshold` | Πολλά άχρηστα στο review → αύξησε |
| `review.claim_keep.auto.enabled` | Όταν `/admin learning` δείχνει «auto ENABLED» και εμπιστεύεσαι τα metrics |

Αλλαγές: `/admin config-export` → edit → `/admin config-import` (diff + επιβεβαίωση) ή `guru config apply`.

## 4. Backups & restore

```bash
deploy/backup.sh                      # pg_dump -Fc → backups/guru-YYYYmmdd-HHMM.dump, κρατά 14 ημέρες
# cron (host): 17 3 * * * cd /opt/aion2guru && deploy/backup.sh >> backups/backup.log 2>&1
```

**Restore (και δοκιμή restore κάθε μήνα):**
```bash
docker compose stop guru
docker compose exec -T postgres dropdb -U guru guru && docker compose exec -T postgres createdb -U guru guru
docker compose exec -T postgres pg_restore -U guru -d guru < backups/guru-….dump
docker compose start guru && docker compose exec guru guru audit --verify
```

## 5. Αναβάθμιση

```bash
git pull && docker compose up -d --build     # τα migrations εφαρμόζονται αυτόματα στην εκκίνηση
docker compose exec guru guru doctor
```

## 6. Συνήθη προβλήματα

| Σύμπτωμα | Αιτία / λύση |
|----------|--------------|
| Δεν απαντά σε mentions | Message Content intent· ο χρήστης δεν είναι σε ρόλο «AI users»· `guru doctor` |
| Δεν σβήνει μηνύματα | Λείπει Manage Messages στο channel (log: `discord.forbidden`) |
| Δεν φαίνονται commands | Βάλε `guild_ids` στο guru.yaml (το global sync αργεί έως 1 ώρα) |
| Αργές/καθόλου AI απαντήσεις | `guru doctor` (Ollama/model)· circuit breaker ανοίγει μετά από 3 αποτυχίες για 60s· οι απαντήσεις γίνονται deterministic στο μεταξύ |
| Κολλημένα jobs | `guru jobs` → `guru jobs --retry ID` (dead letters) |
| FAQ δεν δημοσιεύεται | Δικαιώματα στο forum (Create Posts, Manage Threads)· `faq_publications.sync_status = 'error'` |
| Λάθος γνώση | `/kb show K-12` (πηγές) → `/kb retract K-12 reason` ή `/kb obsolete` |
| Λάθος ρύθμιση | `/admin config-rollback <version>` |
| Αίτημα διαγραφής χρήστη | Ο ίδιος: `/kb forget-me` |

## 7. Παρακολούθηση

`http://127.0.0.1:8080/metrics` (Prometheus): `guru_queries_total{answered_by}` (στόχος: ≥50% χωρίς LLM),
`guru_query_latency_seconds`, `guru_llm_tokens_total`, `guru_access_denied_total`, `guru_rate_limited_total`,
`guru_ingest_decisions_total`, `guru_review_decisions_total`, `guru_jobs_processed_total{outcome="dead"}`.
Audit: `/admin audit` ή `guru audit --verify` (hash chain).

## 8. Staging smoke checklist (πριν από κάθε σημαντική αλλαγή)

1. `guru doctor` χωρίς ❌.
2. `@bot` από χρήστη χωρίς ρόλο → το μήνυμα σβήνεται.
3. `@bot μάθε: <fact>` από contributor → 📝, εμφανίζεται στο review channel, ✅ Keep → `/kb show`.
4. `@bot <ερώτηση>` → απάντηση με πηγές/badge· 👍 λειτουργεί.
5. 📌 σε μήνυμα από trusted → 📝 → claim verified.
6. Edit/delete του αρχικού μηνύματος → `/kb show` δείχνει τη σωστή κατάσταση.
7. `/kb ingest-url <σελίδα>` → claim με web πηγή (μετά το extraction).
8. FAQ: έγκριση στο review → post στο FAQ forum· `/faq ask` το βρίσκει.
