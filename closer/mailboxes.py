"""Several sending mailboxes: who a prospect hears from, and how they sign.

Without ``channels.email.mailboxes`` there is one mailbox, the persona's, and
everything here reduces to it. With them, each prospect is given one mailbox
when their first email is drafted (StateManager.assign_sender) and keeps it.
"""

from closer.config import (cold_daily_cap, mailbox_for, mailboxes_of, primary_mailbox,
                           sender_role)

# Words that close an email above a name: dropped only together with the name.
_CLOSERS = frozenset({"best", "best,", "thanks", "thanks,", "cheers", "cheers,",
                      "regards", "regards,", "best regards", "best regards,"})
# Longest title accepted in an older 'title, company' line, so a sentence that
# happens to end in the company's name is never taken for a sign-off.
MAX_TITLE = 40


def is_multi(config) -> bool:
    """Whether channels.email.mailboxes lists mailboxes."""
    email = getattr(getattr(config, "channels", None), "email", None)
    return bool(getattr(email, "mailboxes", None))


async def sender_of(state, config, prospect_id: str):
    """The MailboxConfig this prospect hears from, assigned on first use."""
    if not is_multi(config):
        return primary_mailbox(config)
    boxes = mailboxes_of(config)
    choices = [b.email for b in boxes]
    # A prospect keeps the mailbox it was given (its thread stays with one
    # sender). Only a prospect not yet given one skips a mailbox still inside
    # its warmup ramp, while another mailbox can send cold email today: its
    # emails would otherwise wait days. With none ready, all are choices.
    if await state.get_prospect_sender(prospect_id) not in choices:
        email = config.channels.email
        ready = [b.email for b in boxes if cold_daily_cap(email, mailbox=b) > 0]
        choices = ready or choices
    address = await state.assign_sender(prospect_id, choices)
    return mailbox_for(config, address) or primary_mailbox(config)


def stored_address(config, mailbox) -> str:
    """What an outbox row records as its mailbox: blank with a single
    mailbox, so rows stay as they always were."""
    return mailbox.email if is_multi(config) else ""


def sign_off(config, mailbox) -> str:
    """The lines under a cold email: name, title, company."""
    company = (config.persona.company or "").strip()
    return "\n".join(x for x in (mailbox.name.strip(), sender_role(config, mailbox), company) if x)


def _known_lines(config) -> set[str]:
    """Any line that could be a sign-off this Closer wrote (casefolded)."""
    known = {(config.persona.name or "").strip().casefold()}
    for box in mailboxes_of(config):
        for line in sign_off(config, box).split("\n"):
            known.add(line.casefold())
    known.discard("")
    return known


def _is_sign_off_line(line: str, known: set[str], company: str) -> bool:
    """A sign-off line, or the older 'title, company' form under any title."""
    line = line.strip().casefold()
    if line in known:
        return True
    title, comma, rest = line.rpartition(", ")
    return bool(comma and company and rest == company.casefold()
                and len(title) <= MAX_TITLE and not set(title) & set(".?!"))


def with_sign_off(config, body: str, mailbox) -> str:
    """``body`` ending in this mailbox's sign-off (unchanged with a single
    mailbox: the writer signs those). The model is told not to write one, so
    one it wrote anyway (or an older one) is replaced, never doubled. A bare
    sign-off is what's stripped: only whole lines that match."""
    if not is_multi(config):
        return body
    lines = (body or "").rstrip().split("\n")
    known = _known_lines(config)
    company = (config.persona.company or "").strip()
    removed = False
    while lines and _is_sign_off_line(lines[-1], known, company):
        lines.pop()
        removed = True
        while lines and not lines[-1].strip():
            lines.pop()
    if removed and lines and lines[-1].strip().casefold() in _CLOSERS:
        lines.pop()
    text = "\n".join(lines).rstrip()
    closing = sign_off(config, mailbox)
    return f"{text}\n\n{closing}" if text and closing else (text or closing)
