"""Platform-neutral rendering of answers and notices, localized (en / el / Greeklish).

Greeklish strings are derived from the Greek ones by deterministic transliteration.
Output is plain data; the Discord adapter turns it into embeds/views.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from guru.core.text import to_greeklish

COLOR = {"verified": 0x2ECC71, "corroborated": 0x3498DB, "unverified": 0xF1C40F, "disputed": 0xE67E22,
         "none": 0x95A5A6, "error": 0xE74C3C}  # fmt: skip
BADGE = {"verified": "✅", "corroborated": "☑️", "unverified": "⚠️", "disputed": "⚔️"}

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "state.verified": "Verified",
        "state.corroborated": "Corroborated",
        "state.unverified": "Not verified",
        "state.disputed": "Conflicting information",
        "basis.human": "confirmed by the team",
        "basis.official_source": "official source",
        "basis.corroboration": "multiple independent sources",
        "field.status": "Status",
        "field.sources": "Sources",
        "field.applies": "Applies to",
        "src.discord": "Discord message",
        "src.manual": "Entry by the team",
        "src.import": "Import",
        "src.web": "Web",
        "answer.related": "Possibly relevant records:",
        "answer.conflict": "Conflicting information — I can't confirm which one is correct:",
        "answer.mixed": "Partly verified",
        "answer.no_answer": "I don't have documented knowledge about this yet.",
        "answer.off_topic": "This doesn't look related to {profile}. If it is, add a category, e.g. `#items`.",
        "answer.empty": "Ask me something about {profile}, e.g. `@bot where does the X boss spawn?`",
        "answer.faq_unavailable": "The FAQ is not available yet.",
        "answer.needs_review": "🕒 Possibly outdated — under review.",
        "answer.unverified_note": "Not verified yet — treat with caution.",
        "limit.minute": "Slow down a bit — try again in {seconds}s.",
        "limit.hour": "You reached your hourly question limit. Try again in {minutes} min.",
        "limit.day": "You reached your daily question limit. Try again tomorrow.",
        "denied.notice": "You don't have access to the AI assistant.",
        "perm.denied": "You need the `{capability}` permission for this.",
        "notfound": "Not found.",
        "footer.record": "Record",
        "feedback.thanks": "Thanks for the feedback!",
        "teach.ok": "📝 Noted — it will be processed and reviewed.",
        "capture.ok": "📝 Added for processing (K-records appear after extraction).",
        "review.keep_title": "🧾 Keep this information?",
        "review.rate_title": "🗳️ Rate this answer",
        "review.question": "Question",
        "review.answer": "Answer",
        "review.decided": "Decided: {decision} by {who}",
    },
    "el": {
        "state.verified": "Επιβεβαιωμένο",
        "state.corroborated": "Διασταυρωμένο",
        "state.unverified": "Μη επιβεβαιωμένο",
        "state.disputed": "Αντικρουόμενες πληροφορίες",
        "basis.human": "επιβεβαίωση από την ομάδα",
        "basis.official_source": "επίσημη πηγή",
        "basis.corroboration": "πολλές ανεξάρτητες πηγές",
        "field.status": "Κατάσταση",
        "field.sources": "Πηγές",
        "field.applies": "Ισχύει για",
        "src.discord": "Μήνυμα Discord",
        "src.manual": "Καταχώρηση από την ομάδα",
        "src.import": "Εισαγωγή",
        "src.web": "Web",
        "answer.related": "Πιθανώς σχετικές καταχωρήσεις:",
        "answer.conflict": "Αντικρουόμενες πληροφορίες — δεν μπορώ να επιβεβαιώσω ποια ισχύει:",
        "answer.mixed": "Εν μέρει επιβεβαιωμένο",
        "answer.no_answer": "Δεν έχω ακόμα τεκμηριωμένη γνώση γι' αυτό.",
        "answer.off_topic": "Δεν φαίνεται να αφορά το {profile}. Αν αφορά, πρόσθεσε κατηγορία, π.χ. `#items`.",
        "answer.empty": "Ρώτα με κάτι για το {profile}, π.χ. `@bot πού βγαίνει ο boss X;`",
        "answer.faq_unavailable": "Το FAQ δεν είναι ακόμα διαθέσιμο.",
        "answer.needs_review": "🕒 Ίσως παλιό — υπό επανέλεγχο.",
        "answer.unverified_note": "Δεν έχει επιβεβαιωθεί ακόμα — με επιφύλαξη.",
        "limit.minute": "Λίγο πιο αργά — ξαναδοκίμασε σε {seconds}s.",
        "limit.hour": "Έφτασες το ωριαίο όριο ερωτήσεων. Ξαναδοκίμασε σε {minutes} λεπτά.",
        "limit.day": "Έφτασες το ημερήσιο όριο ερωτήσεων. Ξαναδοκίμασε αύριο.",
        "denied.notice": "Δεν έχεις πρόσβαση στον AI βοηθό.",
        "perm.denied": "Χρειάζεσαι το δικαίωμα `{capability}` γι' αυτό.",
        "notfound": "Δεν βρέθηκε.",
        "footer.record": "Καταχώρηση",
        "feedback.thanks": "Ευχαριστώ για το feedback!",
        "teach.ok": "📝 Σημειώθηκε — θα επεξεργαστεί και θα ελεγχθεί.",
        "capture.ok": "📝 Προστέθηκε για επεξεργασία.",
        "review.keep_title": "🧾 Κρατάμε αυτή την πληροφορία;",
        "review.rate_title": "🗳️ Βαθμολόγησε την απάντηση",
        "review.question": "Ερώτηση",
        "review.answer": "Απάντηση",
        "review.decided": "Απόφαση: {decision} από {who}",
    },
}


def tr(key: str, style: str = "en", **kw: Any) -> str:
    lang = "el" if style in ("el", "greeklish") else "en"
    text = STRINGS[lang].get(key) or STRINGS["en"][key]
    text = text.format(**kw) if kw else text
    return to_greeklish(text) if style == "greeklish" else text


@dataclass
class EmbedField:
    name: str
    value: str
    inline: bool = False


@dataclass
class EmbedData:
    description: str
    color: int
    title: str | None = None
    fields: list[EmbedField] = field(default_factory=list)
    footer: str | None = None


@dataclass
class ButtonData:
    custom_id: str
    label: str
    emoji: str | None = None
    style: str = "secondary"  # primary | secondary | success | danger


@dataclass
class MessagePayload:
    content: str | None = None
    embed: EmbedData | None = None
    buttons: list[ButtonData] = field(default_factory=list)
    ephemeral: bool = False
    delete_after: float | None = None


# ---------------------------------------------------------------- limits & discord constraints

DESC_LIMIT = 4096
FIELD_LIMIT = 1024


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _date(d: datetime | None) -> str:
    return d.strftime("%Y-%m-%d") if d else "—"


def status_line(verification: str, basis: str | None, style: str, groups: int = 0) -> str:
    text = f"{BADGE.get(verification, '')} {tr('state.' + verification, style)}"
    if basis:
        text += f" · {tr('basis.' + basis, style)}"
    elif verification in ("unverified", "corroborated") and groups:
        text += f" · {groups}×"
    return text


def source_line(idx: int, src: Any, style: str) -> str:
    kind = {"discord_message": "src.discord", "manual": "src.manual", "import": "src.import"}.get(src.kind, "src.web")
    label = src.title or tr(kind, style)
    link = src.link
    text = f"[{idx}] {clip(label, 60)} · {_date(src.evidence_at)}"
    return f"{text} — <{link}>" if link else text


FEEDBACK_BUTTONS = [
    ButtonData("guru:fb:up", "", "👍"),
    ButtonData("guru:fb:down", "", "👎"),
]


def render_answer(answer: Any, profile_name: str) -> MessagePayload:
    """`answer` is a guru.services.query_service.Answer (duck-typed to keep core free of services)."""
    style = answer.style
    if answer.mode in ("empty", "faq_unavailable"):
        key = "answer.empty" if answer.mode == "empty" else "answer.faq_unavailable"
        return MessagePayload(embed=EmbedData(tr(key, style, profile=profile_name), COLOR["none"]))
    if answer.mode == "no_answer" or not answer.items:
        text = tr("answer.no_answer", style)
        if answer.off_topic:
            text += "\n" + tr("answer.off_topic", style, profile=profile_name)
        return MessagePayload(embed=EmbedData(text, COLOR["none"]))

    if answer.mode == "extractive":
        item = answer.items[0]
        desc = item.statement
        if item.needs_review:
            desc += "\n\n" + tr("answer.needs_review", style)
        elif item.verification == "unverified":
            desc += "\n\n_" + tr("answer.unverified_note", style) + "_"
        fields = [
            EmbedField(
                tr("field.status", style), status_line(item.verification, item.basis, style, item.groups), inline=False
            )
        ]
        if item.sources:
            lines = [source_line(i + 1, s, style) for i, s in enumerate(item.sources)]
            fields.append(EmbedField(tr("field.sources", style), clip("\n".join(lines), FIELD_LIMIT)))
        footer = f"{tr('footer.record', style)} K-{item.claim_id}" + (
            f" · {item.category_key}" if item.category_key else ""
        )
        return MessagePayload(
            embed=EmbedData(
                clip(desc, DESC_LIMIT), COLOR.get(item.verification, COLOR["none"]), fields=fields, footer=footer
            ),
            buttons=list(FEEDBACK_BUTTONS),
        )

    if answer.mode == "conflict":
        lines = [tr("answer.conflict", style)]
        for item in answer.items:
            src = item.sources[0] if item.sources else None
            where = f" — {source_line(1, src, style)[4:]}" if src else ""
            lines.append(f"• {BADGE.get(item.verification, '')} {clip(item.statement, 300)} `K-{item.claim_id}`{where}")
        return MessagePayload(
            embed=EmbedData(clip("\n".join(lines), DESC_LIMIT), COLOR["disputed"]), buttons=list(FEEDBACK_BUTTONS)
        )

    if answer.mode == "llm":
        state = answer.overall_state
        status = status_line(state, None, style)
        if state == "corroborated" and any(i.verification == "unverified" for i in answer.items):
            status = f"{BADGE['corroborated']} {tr('answer.mixed', style)}"
        desc = answer.text or ""
        if state == "unverified":
            desc += "\n\n_" + tr("answer.unverified_note", style) + "_"
        sources = [s for i in answer.items for s in i.sources][:4]
        fields = [EmbedField(tr("field.status", style), status)]
        if sources:
            lines = [source_line(n + 1, s, style) for n, s in enumerate(sources)]
            fields.append(EmbedField(tr("field.sources", style), clip("\n".join(lines), FIELD_LIMIT)))
        footer = " · ".join(f"K-{i.claim_id}" for i in answer.items) + " · AI"
        return MessagePayload(
            embed=EmbedData(clip(desc, DESC_LIMIT), COLOR.get(state, COLOR["none"]), fields=fields, footer=footer),
            buttons=list(FEEDBACK_BUTTONS),
        )

    # list mode: several comparable records, each with its own badge and id
    lines = [tr("answer.related", style)]
    for item in answer.items:
        src = item.sources[0] if item.sources else None
        link = f" — <{src.link}>" if src and src.link else ""
        lines.append(f"• {BADGE.get(item.verification, '')} {clip(item.statement, 300)} `K-{item.claim_id}`{link}")
    worst = "disputed" if any(i.verification == "disputed" for i in answer.items) else answer.items[0].verification
    return MessagePayload(
        embed=EmbedData(clip("\n".join(lines), DESC_LIMIT), COLOR.get(worst, COLOR["none"])),
        buttons=list(FEEDBACK_BUTTONS),
    )


def render_limited(window: str | None, retry_after_s: float | None, style: str, delete_after: float) -> MessagePayload:
    secs = int(retry_after_s or 0)
    key = {"minute": "limit.minute", "hour": "limit.hour"}.get(window or "", "limit.day")
    return MessagePayload(
        content=tr(key, style, seconds=max(secs, 1), minutes=max(secs // 60, 1)), delete_after=delete_after
    )


def render_claim(data: dict[str, Any], style: str) -> MessagePayload:
    c = data["claim"]
    fields = [
        EmbedField(tr("field.status", style), status_line(c["verification"], c["verification_basis"], style)),
        EmbedField("Lifecycle", f"{c['lifecycle']}" + (f" ({c['lifecycle_reason']})" if c["lifecycle_reason"] else "")),
    ]
    if c.get("category_key"):
        fields.append(EmbedField("Category", c["category_key"], inline=True))
    if c.get("applicability"):
        fields.append(EmbedField(tr("field.applies", style), str(c["applicability"]), inline=True))
    lines = []
    for e in data["evidence"][:10]:
        where = (
            f"<https://discord.com/channels/{e['guild_id']}/{e['channel_id']}/{e['message_id']}>"
            if e["message_id"]
            else (f"<{e['url']}>" if e["url"] else e["kind"])
        )
        who = f"<@{e['author_id']}>" if e["author_id"] else ""
        state = "" if e["active"] else f" ~~inactive: {e['deactivated_reason']}~~"
        lines.append(
            f"{'➕' if e['stance'] == 'supports' else '➖'} T{e['trust_tier']} {e['origin']} "
            f"{_date(e['evidence_at'])} {who} {where}{state}"
        )
    if lines:
        fields.append(EmbedField(tr("field.sources", style), clip("\n".join(lines), FIELD_LIMIT)))
    return MessagePayload(
        embed=EmbedData(
            clip(c["statement"], DESC_LIMIT),
            COLOR.get(c["verification"], COLOR["none"]),
            title=f"K-{c['id']}",
            fields=fields,
            footer=f"rev {c['rev']} · created {_date(c['created_at'])}",
        ),
        ephemeral=True,
    )


def render_review(task: dict[str, Any], style: str = "el") -> MessagePayload:
    """Moderator review post (D-27). Buttons carry the task id (persistent across restarts)."""
    p = task["payload"] or {}
    tid = task["id"]
    if task["kind"] == "claim_keep":
        fields = [
            EmbedField("Category", str(p.get("category") or "—"), inline=True),
            EmbedField(
                tr("field.status", style), status_line(p.get("verification", "unverified"), None, style), inline=True
            ),
        ]
        lines = []
        for q in p.get("quotes", []):
            link = (
                f"https://discord.com/channels/{q['guild_id']}/{q['channel_id']}/{q['message_id']}"
                if q.get("message_id")
                else None
            )
            who = f"<@{q['author_id']}>" if q.get("author_id") else ""
            lines.append(
                f"> {clip(q.get('quote', ''), 200)}\n{who} T{q.get('tier', '?')}" + (f" — <{link}>" if link else "")
            )
        if lines:
            fields.append(EmbedField(tr("field.sources", style), clip("\n".join(lines), FIELD_LIMIT)))
        return MessagePayload(
            embed=EmbedData(
                clip(str(p.get("statement", "")), DESC_LIMIT),
                COLOR["unverified"],
                title=tr("review.keep_title", style),
                fields=fields,
                footer=f"K-{task['target_id']} · task {tid}",
            ),
            buttons=[
                ButtonData(f"guru:rv:{tid}:keep", "Keep", "✅", "success"),
                ButtonData(f"guru:rv:{tid}:reject", "Reject", "❌", "danger"),
                ButtonData(f"guru:rv:{tid}:edit", "Edit", "✏️", "secondary"),
            ],
        )
    items = p.get("items", [])
    answer = (
        "\n".join(
            f"• {BADGE.get(i.get('verification', ''), '')} {clip(i.get('statement', ''), 300)} `K-{i.get('claim_id')}`"
            for i in items
        )
        or "—"
    )
    bot = p.get("bot_message") or {}
    link = (
        f"https://discord.com/channels/{bot['guild_id']}/{bot['channel_id']}/{bot['message_id']}"
        if bot.get("message_id")
        else None
    )
    fields = [
        EmbedField(tr("review.question", style), clip(str(p.get("question", "")), FIELD_LIMIT)),
        EmbedField(tr("review.answer", style), clip(answer, FIELD_LIMIT)),
    ]
    return MessagePayload(
        embed=EmbedData(
            f"<{link}>" if link else "",
            COLOR["none"],
            title=tr("review.rate_title", style),
            fields=fields,
            footer=f"{p.get('answered_by', '')} · task {tid}",
        ),
        buttons=[
            ButtonData(f"guru:rv:{tid}:good", "Good", "👍", "success"),
            ButtonData(f"guru:rv:{tid}:bad", "Bad", "👎", "danger"),
        ],
    )
