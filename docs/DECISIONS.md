# Αποφάσεις, ασάφειες & ρίσκα — προς κλείδωμα

> Κάθε απόφαση έχει **πρόταση** (default αν δεν απαντηθεί) και ποια backlog tasks μπλοκάρει.
> Status: `OPEN` → `LOCKED (ημερομηνία, επιλογή)`.

## 0. Κλειδωμένα (2026-10-06)

| ID | Απόφαση | Συνέπεια στο design |
|----|---------|---------------------|
| D-01 | RTX 5070 Ti (16 GB VRAM), 32 GB RAM, ένας host | ~20B σε 4-bit (π.χ. gpt-oss-20b ≈ 13 GB) στη GPU· embeddings σε CPU |
| D-02 | **Local LLM** ως default. Cloud API μόνο προαιρετικά (pay-as-you-go key, όχι ChatGPT Plus) για bootstrap/eval — **εκκρεμεί επιβεβαίωση** | Per-task model routing ήδη στο design· κανένα Discord content στο cloud by default |
| D-03 | Γλώσσες: **en (βασική) + el + Greeklish**. Καμία άλλη. Απάντηση στη γλώσσα/γραφή της ερώτησης (και Greeklish) | Πηγές σε άλλες γλώσσες αγνοούνται· φεύγει το T-TRANSLATE/W-06· deterministic transliteration el→Greeklish για templates |
| D-04 | **Global** = βάση. Πληροφορίες άλλων regions κρατιούνται με ετικέτα region, χαμηλότερο ranking, ρητή σήμανση | `region` default `[GLOBAL]`· version timeline από Global patches |
| D-11 | Δεν υπάρχει λίστα πηγών: το σύστημα **ανακαλύπτει** πηγές, τις συγκρίνει και **προσαρμόζει την αξιοπιστία** τους. Η γνώση της ομάδας στο Discord = αξιόπιστη αναφορά | Μπαίνουν στο MVP: search provider (SearxNG self-hosted) + source discovery + **source reputation** (στατιστική συμφωνίας με verified γνώση, όχι AI). Tier 4 μόνο χειροκίνητα. Team role = trusted |
| D-12 | Config στη DB, versioned, YAML import/export | Όπως στο design |
| D-20 | Ρόλοι/trust/ποιους ακούει/σε ποιους απαντά: **ρυθμίσεις μέσα στο bot**, όχι hardcoded | `/settings` panel στο Discord με role/channel selectors· bootstrap μόνο για server Administrator |

## 1. Αποφάσεις

### 1.1 Blocking (χρειάζονται πριν από M1/M2)

| ID | Θέμα | Επιλογές | Πρόταση | Μπλοκάρει | Status |
|----|------|----------|---------|-----------|--------|
| D-01 | **Hardware / hosting** | GPU VRAM; CPU/RAM; ίδιος host για LLM; | 1 host, GPU ≥16 GB, 16 GB RAM, Docker Compose | A-01, A-03, H-05 | OPEN |
| D-02 | **LLM runtime** | Ollama · llama.cpp server · vLLM · υπάρχον endpoint | Ollama για απλότητα (MVP)· vLLM αν χρειαστεί concurrency. Ο κώδικας μένει provider-agnostic | A-01 | OPEN |
| D-03 | **Γλώσσες** | Ερωτήσεις: el/en/Greeklish; Πηγές: en/KR/TW; Canonical αποθήκευση; Γλώσσα απάντησης; | Ερωτήσεις el+en (+Greeklish aliases). Canonical = en. Απάντηση = γλώσσα ερώτησης. Μετάφραση πηγών μόνο αν D-04 περιλαμβάνει KR/TW | C-05, C-06, A-03, A-04 | OPEN |
| D-04 | **AION 2 regions/versions** | KR · TW · Global; ποιο versioning; | Ορισμός regions που παρακολουθούμε + πηγή versioning (official patch notes) | C-06, W-05 | OPEN |
| D-05 | **Passive ingestion** | explicit-only · hybrid · passive-all | **Hybrid**: explicit = κύριο σήμα, passive με prefilter → πάντα `unverified` | I-01, I-02 | OPEN |
| D-06 | **Privacy & retention** | Αποθήκευση raw content; διάρκειες; purge on delete; forget-me; | Μόνο candidates· context 14d· purge on delete άμεσα· `/kb forget-me`· hashed user ids σε logs | I-04, I-06 | OPEN |
| D-12 | **Config source of truth** | DB-versioned + YAML import/export · μόνο Git YAML | **DB-versioned** + YAML import/export + CLI. Επιτρέπει runtime αλλαγές από Discord με audit & rollback | C-02 | OPEN |
| D-15 | **Tenancy** | 1 guild · multi-guild | 1 guild στο MVP, schema multi-guild-ready | C-02 | OPEN |
| D-18 | **Ονοματολογία/δομή** | package `guru`; license; | `guru` (generic core), profile data σε `profiles/<slug>/` | F-01 | OPEN |
| D-20 | **Αρχικοί admins/trusted roles** | Role/user IDs | Ο χρήστης δίνει IDs πριν από το deploy | C-06 | OPEN |

### 1.2 Μη-blocking (μπορούν να κλειδώσουν αργότερα· ισχύει η πρόταση)

| ID | Θέμα | Επιλογές | Πρόταση | Αφορά |
|----|------|----------|---------|-------|
| D-07 | Default ελάχιστο state απάντησης | unverified (με σήμανση) · corroborated+ | **unverified με σαφές badge**, χαμηλότερο ranking (αλλιώς η άδεια αρχικά KB δεν απαντά τίποτα) | Q-02 |
| D-08 | FAQ publishing | approval · auto | **Approval** (X1)· auto ως opt-in policy αργότερα | FQ-03 |
| D-09 | FAQ channel | forum · text · deprecate=mark/delete | **Forum** (posts, tags, αναζήτηση)· deprecate = **mark + lock** | FQ-04 |
| D-10 | Internet scope & search provider | stored-only · live · SearxNG · Brave · άλλο | **Stored-only στο MVP**· SearxNG self-hosted στο P2 (χωρίς κόστος API) | W-*, P2 |
| D-11 | **Curated πηγές AION 2** | Official site, patch notes, wikis, community sites + tiers | Ο χρήστης δίνει λίστα· εγώ προτείνω tiers | W-02, C-06 |
| D-13 | Admin interface | Discord+CLI · web UI | **Discord + CLI** στο MVP | DS-02 |
| D-14 | Restricted channels | Επιτρέπονται; | Ναι, με **evidence-level visibility** & έλεγχο ανά asker | Q-10 |
| D-16 | Συμπεριφορά χωρίς evidence | «δεν ξέρω» · LLM general knowledge | **Αυστηρά «δεν ξέρω»** — ποτέ parametric knowledge (το LLM δεν ξέρει αξιόπιστα το AION 2) | Q-08 |
| D-17 | Syntax mentions & ονόματα commands | tokens/ονόματα, ελληνικά localizations | `/ask` `/faq` `/kb` `/admin` + Discord command localizations (el)· tokens όπως στο example YAML | Q-01 |
| D-19 | Embedding model | multilingual-e5-small (384d) · multilingual-e5-base (768d) · bge-m3 (1024d) · άλλο | Benchmark σε δείγμα el/en (+KR αν D-04) — μικρότερο που περνά το eval | A-03 |
| D-21 | Webhook/bot μηνύματα | αγνόηση · allowlist | **Allowlist** (π.χ. followed official announcements = tier 4) | I-01 |
| D-22 | DMs | ναι · όχι | **Όχι στο MVP** (χωρίς channel context δεν ελέγχεται visibility σωστά) | Q-01 |
| D-23 | Ορατότητα απαντήσεων slash | public · ephemeral | Public, με option `private:true` → ephemeral | Q-03 |
| D-24 | Rumors / datamines | απόρριψη · κράτηση ως `rumor` | Απόρριψη στο MVP· `claim_type=rumor` (πάντα unverified, ρητό badge) αν το θέλεις | A-04 |
| D-25 | Multi-profile στο ίδιο channel | ναι · όχι | `watch` σε πολλά profiles επιτρέπεται· `ask` → ένα profile ανά channel | C-02 |

## 2. Ασάφειες στο αρχικό spec

| ID | Ασάφεια | Ερμηνεία που υιοθετώ (αν δεν διορθωθεί) |
|----|---------|------------------------------------------|
| A-01 | «Internet knowledge» scope: live search ή αποθηκευμένη γνώση; | Αποθηκευμένη & validated (X2)· live = P2, opt-in |
| A-02 | «FAQ / verified knowledge» ως ένα scope | Δύο presets: `faq` (μόνο published FAQ) και `verified` (claims verified από οποιαδήποτε πηγή) |
| A-03 | «Sufficiently verified» για FAQ | `verified` με basis `human` ή `official_source`, ή ρητό policy ανά category |
| A-04 | Τι θεωρείται «χρήσιμη πληροφορία» σε συζητήσεις | `fact`, `tip`, `procedure` για το παιχνίδι· όχι απόψεις/αστεία/ερωτήσεις (οι ερωτήσεις κρατούνται μόνο ως σήμα ζήτησης για FAQ) |
| A-05 | «Τροφοδοτούν knowledge»: passive από όλους ή μόνο ρόλους; | Passive ανά channel policy (`min_author_tier`)· explicit capture μόνο με `kb.ingest` |
| A-06 | Ποια structured δεδομένα έχουν αξία στο AION 2 | Χρειάζεται domain input: entity types/attributes (example YAML = πρόταση) |
| A-07 | «Version relevance»: client patch, season, region; | Γενικευμένα dimensions· για AION 2: `game_version` + `region` |
| A-08 | Τι σημαίνει «χρήστης καθορίζει την πηγή» πέρα από τα scopes | Scopes + προαιρετικό `source:<key>` filter |
| A-09 | Αυτόματη ενημέρωση FAQ όταν αλλάζει η γνώση | Αλλαγή → `needs_review` + νέο draft· δημοσίευση μετά από έγκριση (εκτός αν policy auto) |
| A-10 | «Ποιοι μπορούν να αλλάζουν rules» — ποια rules | Categories, keywords, aliases, intents, prefilter weights, domain trust → `rules.manage` |

## 3. Ρίσκα

| ID | Ρίσκο | Πιθ. | Επίπτωση | Μέτρα |
|----|-------|------|----------|-------|
| R-01 | **Message Content intent** (privileged) | — | Χωρίς αυτό δεν διαβάζονται watched channels | Ενεργοποίηση στο Developer Portal· για > 100 servers απαιτείται verification — εκτός scope MVP |
| R-02 | **Prompt injection / knowledge poisoning** | Μέτρια | Λάθος γνώση με «κύρος» | Validators V1–V7, trust/community cap, independence, explicit-only σε low-trust, approval για FAQ, security suite |
| R-03 | **Διαρροή restricted γνώσης** | Χαμηλή | Σοβαρή | Evidence-level audience, query-time permission check, visibility στο cache key, property tests |
| R-04 | **Ποιότητα extraction με local 20B** | Μέτρια | Hallucinated/κακά claims | Verbatim quote check, constrained JSON, temperature 0, eval gold set, ρύθμιση prompts ανά version |
| R-05 | **Παλιά πληροφορία μετά από patches** | Υψηλή | Λάθος απαντήσεις | Applicability, half-life ανά category, `needs_review` σε νέο version, badges ηλικίας/version |
| R-06 | **Scraping blocked / ToS / γλώσσα πηγών (KR)** | Μέτρια | Λίγες web πηγές | Curated λίστα, robots/rate limits, RSS όπου υπάρχει, μετάφραση μόνο relevant chunks |
| R-07 | **Χαμένα edits/deletes σε downtime** | Μέτρια | Ορφανή γνώση | Backfill cursors + periodic recheck των evidence μηνυμάτων |
| R-08 | **GPU contention** (ingestion vs queries) | Μέτρια | Αργές απαντήσεις | Priority gate, background concurrency 1, νυχτερινά batches |
| R-09 | **Over-engineering** (generic EAV, πολλά states) | Μέτρια | Αργή παράδοση | Walking skeleton χωρίς AI πρώτα (M1)· structured μόνο όπου υπάρχει schema· features πίσω από flags |
| R-10 | **Αλλαγή embedding model** | Χαμηλή | Re-embed κόστος | `embedding_models` + backfill χωρίς downtime |
| R-11 | **Conflict detection σε ελεύθερο κείμενο** | Υψηλή | False positives/negatives | Structured όπου γίνεται· T-EQUIV μόνο στο gray zone· human review queue· ρητό «δεν ξέρω» |
| R-12 | **Latency local LLM vs Discord 3 s** | Υψηλή | Timeouts | `defer()`, typing indicator, budgets, extractive fallback |
| R-13 | **Cold start** (άδεια KB) | Βέβαιη | «Δεν ξέρω» στην αρχή | Bulk import (entities, official patch notes), explicit capture campaign, default min_state=unverified |
| R-14 | **Greeklish / mixed-language queries** | Υψηλή | Αστοχία FTS | Greeklish aliases, multilingual embeddings, normalization tests |
| R-15 | **Bus factor / ops** | Μέτρια | Δύσκολη συντήρηση | Runbook, backups με restore test, health/metrics, απλό infra (χωρίς Redis/K8s) |
