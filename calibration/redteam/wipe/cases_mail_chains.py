"""Red team of wipe mode, dimension mail_chains: mail chains as txt, md, eml and mbox.

Headers in English, German, Russian and Outlook style, quoted replies with attribution lines, Outlook
-----Ursprüngliche Nachricht----- blocks, forwarded mails, salutations, sign-offs, signature blocks, legal
footers, calendar invites; losses of product, service and vendor names in bodies and signatures.

Every name is from tests/fixtures.py or from INVENTED below. Hosts end in .example. Nothing here is real.
"""
import base64
import sys
from email.message import EmailMessage
from email import policy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tests import fixtures as fx  # noqa: E402

# the fixture forms
CUSTOMER, CUSTOMER_SHORT, CUSTOMER_ACRONYM, CUSTOMER_EN = fx.CUSTOMER_FORMS
PERSON, SURNAME = fx.PERSON_FORMS            # a registered person (becomes a PERS code, not a token)
FIRST = fx.PLANTED_PERSON.split()[0]         # Xqarv
LAST = fx.PLANTED_PERSON.split()[1]          # Pomblet
PLANTED_PERSON = fx.PLANTED_PERSON           # Xqarv Pomblet, unregistered
PLANTED_COMPANY = fx.PLANTED_CANDIDATE       # Nrgtz Beratung GmbH, unregistered
BRAND = PLANTED_COMPANY.split()[0]           # Nrgtz
CONTROL = fx.CONTROL_UNREGISTERED            # Musterhausen, unregistered, a place word

# every invented name of this module, so that the owner can see nothing real slipped in
INVENTED = (
    "Anna Zrbl",                 # an invented person, four-letter surname
    "Jan Qwertzuio",             # an invented person, first name that is also a month abbreviation
    "Max Qwertzuio",             # an invented person, first name that is a word of the public vocabulary
    "Lena Qwertzung",            # an invented person, surname with a German noun suffix
    "Ксения Зрблова",            # an invented person, Cyrillic
    "Зрблова Ксения Ивановна",   # the same person with a patronymic
    "Иван Зрблов",               # an invented person, Cyrillic
    "Plmok Systemhaus GmbH",     # an invented company
    "Vrxtstraße 12",             # an invented street
    "Vrxt-Straße 12",            # the same street, hyphen and capital
    "Vrxt Straße 12",            # the same street, two words
    "nrgtz-beratung.example",    # the mail host of the planted company
    "plmok.example",             # the mail host of the invented company
    "t-systems.example",         # an invented host with the vendor's label (not a real domain)
    "proxmox.example",           # an invented host with a vendor's label (not a real domain)
    "+49 555 1234567",           # an invented phone number
)

MARK = "stage two of the invented plan"

P2_FIRST, P2_LAST = "Anna", "Zrbl"
P2 = "%s %s" % (P2_FIRST, P2_LAST)
P3_FIRST, P3_LAST = "Jan", "Qwertzuio"
P4_FIRST, P4_LAST = "Max", "Qwertzuio"
P5_FIRST, P5_LAST = "Lena", "Qwertzung"
P5 = "%s %s" % (P5_FIRST, P5_LAST)
RU_FIRST, RU_LAST, RU_PATRONYMIC = "Ксения", "Зрблова", "Ивановна"
RU = "%s %s" % (RU_FIRST, RU_LAST)
RU2_FIRST, RU2_LAST = "Иван", "Зрблов"
COMPANY2 = "Plmok Systemhaus GmbH"
STREET = "Vrxtstraße 12"
STREET_HYPHEN = "Vrxt-Straße 12"
STREET_SPACED = "Vrxt Straße 12"
STREET_WORD = "Vrxt"
HOST1 = "nrgtz-beratung.example"
HOST2 = "plmok.example"
MAIL1 = "x.pomblet@" + HOST1          # the planted person's address
MAIL2 = "a.zrbl@" + HOST2             # the invented person's address
MAIL5 = "l.qwertzung@" + HOST2
PHONE = "+49 555 1234567"


def txt(name, text):
    """A plain file with `text`; the marker line proves the carrier was read."""
    def build(inbox):
        p = inbox / name
        p.write_text(text + "\n" + MARK + "\n", encoding="utf-8")
        return p
    return build


def raw(name, data: bytes):
    def build(inbox):
        p = inbox / name
        p.write_bytes(data)
        return p
    return build


def message(headers, body, html=False, attachments=()):
    """A mail built with the standard library: `headers` is a list of (name, value), `attachments` a list of
    (filename, bytes, maintype, subtype)."""
    msg = EmailMessage(policy=policy.default)
    for k, v in headers:
        msg[k] = v
    msg.set_content(body, subtype="html" if html else "plain")
    for filename, data, maintype, subtype in attachments:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg.as_bytes()


def eml(name, headers, body, html=False, attachments=()):
    return raw(name, message(headers, body, html=html, attachments=attachments))


def mbox(name, messages):
    """A Unix mailbox: (envelope sender, mail bytes) per message."""
    out = b""
    for sender, data in messages:
        out += ("From %s Mon Oct  5 10:03:00 2026\n" % sender).encode("ascii")
        out += data.replace(b"\r\n", b"\n").rstrip(b"\n") + b"\n\n"
    return raw(name, out)


def ics(description_lines, organizer_cn, attendee_cn, location, summary, organizer_mail=MAIL1):
    """A calendar invite; `description_lines` are the physical lines of the DESCRIPTION property (the first
    with the property name, the others folded with a leading space)."""
    lines = [
        "BEGIN:VCALENDAR", "METHOD:REQUEST", "PRODID:-//Invented//Calendar//EN", "VERSION:2.0", "BEGIN:VEVENT",
        "ORGANIZER;CN=%s:mailto:%s" % (organizer_cn, organizer_mail),
        "ATTENDEE;CN=\"%s\";ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:%s" % (attendee_cn, MAIL2),
        "SUMMARY:%s" % summary,
        "LOCATION:%s" % location,
    ] + list(description_lines) + [
        "DTSTART:20261005T100000Z", "DTEND:20261005T110000Z", "UID:20261005-kickoff@" + HOST2,
        "END:VEVENT", "END:VCALENDAR",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def case(cid, carrier, values, keep, build, note="", code=None, expect=None):
    c = {"id": cid, "carrier": carrier, "values": list(values), "keep": list(keep), "build": build, "note": note}
    if code is not None:
        c["code"] = code
    if expect:
        c["expect"] = dict(expect)
    return c


OUTLOOK_SENT = "Gesendet: Montag, 5. Oktober 2026 10:03"

CASES = [
    # ------------------------------------------------------------------ headers
    case("hdr-outlook-de-inverted", "txt, Outlook German header block, Last, First display names without addresses",
         ["%s, %s" % (LAST, FIRST), LAST, FIRST, "%s, %s" % (P2_LAST, P2_FIRST), P2_LAST, P2_FIRST,
          "%s, %s" % (P3_LAST, P3_FIRST), P3_LAST, P3_FIRST],
         ["Elastic Cloud Server"],
         txt("thread.txt",
             "Von: %s, %s <%s>\n%s\nAn: %s, %s; %s, %s\nBetreff: AW: Angebot Elastic Cloud Server\n\n"
             "Hallo zusammen,\n\nanbei das Angebot. Der Elastic Cloud Server steht.\n"
             % (LAST, FIRST, MAIL1, OUTLOOK_SENT, P2_LAST, P2_FIRST, P3_LAST, P3_FIRST)),
         expect={"%s, %s" % (LAST, FIRST): "person", "%s, %s" % (P2_LAST, P2_FIRST): "person",
                 "%s, %s" % (P3_LAST, P3_FIRST): "person"}),
    case("hdr-wrapped-list", "txt, an Outlook An: list wrapped onto a second line inside a name",
         [P2_LAST, P2_FIRST, P3_LAST, P3_FIRST, FIRST, LAST],
         ["Zeitplan"],
         txt("thread.txt",
             "Von: %s, %s <%s>\n%s\nAn: %s, %s; %s\n%s\nBetreff: AW: Zeitplan\n\n"
             "Hallo zusammen,\n\nder Zeitplan steht.\n"
             % (P2_LAST, P2_FIRST, MAIL2, OUTLOOK_SENT, P3_LAST, P3_FIRST, FIRST, LAST)),
         note="the surname lands alone at the start of the continuation line"),
    case("hdr-suffix-surname-pair", "txt, Outlook Von: with Last, First where the surname carries a German noun suffix",
         ["%s, %s" % (P5_LAST, P5_FIRST), P5_LAST, P5_FIRST],
         ["Zeitplan"],
         txt("thread.txt",
             "Von: %s, %s <%s>\n%s\nAn: %s\nBetreff: AW: Zeitplan\n\nHallo zusammen,\n\nder Zeitplan steht.\n"
             % (P5_LAST, P5_FIRST, MAIL5, OUTLOOK_SENT, MAIL2)),
         expect={"%s, %s" % (P5_LAST, P5_FIRST): "person"}),
    case("hdr-caps-surname-pair", "txt, Outlook Von: with the surname in capitals (ZRBL, Anna)",
         ["%s, %s" % (P2_LAST.upper(), P2_FIRST), P2_LAST, P2_FIRST],
         ["Zeitplan"],
         txt("thread.txt",
             "Von: %s, %s <%s>\n%s\nAn: %s\nBetreff: AW: Zeitplan\n\nHallo zusammen,\n\nder Zeitplan steht.\n"
             % (P2_LAST.upper(), P2_FIRST, MAIL2, OUTLOOK_SENT, MAIL5)),
         expect={"%s, %s" % (P2_LAST.upper(), P2_FIRST): "person"}),
    case("hdr-on-behalf", "txt, Outlook Von: with im Auftrag von, a plain noun inside the header value",
         [P5, P5_FIRST, P5_LAST, PLANTED_PERSON, FIRST, LAST, P2_FIRST],
         ["Auftrag", "Load Balancer"],
         txt("thread.txt",
             "Von: %s im Auftrag von %s\n%s\nAn: %s, %s\nBetreff: Vertretung\n\n"
             "Hallo %s,\n\nder Auftrag für den Load Balancer ist erteilt.\n\nViele Grüße\n%s\n"
             % (P5, PLANTED_PERSON, OUTLOOK_SENT, P2_LAST, P2_FIRST, P2_FIRST, P5_FIRST)),
         expect={PLANTED_PERSON: "person"}),
    case("hdr-sender-product", "eml, a product name as the From display name of a notification",
         [P2, P2_FIRST, P2_LAST],
         ["Azure DevOps", "Terraform"],
         eml("build.eml",
             [("From", "Azure DevOps <azuredevops@%s>" % HOST2), ("To", "%s <%s>" % (P2, MAIL2)),
              ("Subject", "[Build] pipeline tcp-landing-zone succeeded"), ("Date", "Mon, 5 Oct 2026 10:03:00 +0200")],
             "Build 20261005.1 of pipeline tcp-landing-zone succeeded.\nTerraform plan: no changes.\n")),
    case("hdr-ru-outlook", "txt, Russian Outlook Исходное сообщение block, surname first with patronymic",
         [RU_LAST, RU_FIRST, RU_PATRONYMIC, RU2_LAST, RU2_FIRST],
         ["Relational Database Service", "T Cloud Public"],
         txt("thread.txt",
             "-----Исходное сообщение-----\nОт: %s %s %s <k.zrblova@%s>\nОтправлено: 5 октября 2026 г. 10:03\n"
             "Кому: %s %s <i.zrblov@%s>\nТема: RE: Миграция в T Cloud Public\n\n"
             "%s, добрый день!\n\nСогласовано, ждём %sа на звонке. Relational Database Service подходит.\n\n"
             "С уважением,\n%s\n"
             % (RU_LAST, RU_FIRST, RU_PATRONYMIC, HOST1, RU2_LAST, RU2_FIRST, HOST2, RU2_FIRST, RU2_LAST, RU_FIRST))),

    # ------------------------------------------------------------------ quoted replies
    case("reply-ru-gmail-noverb", "txt, Russian Gmail attribution line without a verb (date, name <address>:)",
         [RU, RU_FIRST, RU_LAST],
         ["Elastic Cloud Server"],
         txt("reply.txt",
             "%s, спасибо за уточнение, тогда берём Elastic Cloud Server.\n\n"
             "пн, 5 окт. 2026 г. в 10:03, %s <k.zrblova@%s>:\n\n> Добрый день!\n> Предлагаю два варианта.\n"
             % (RU_FIRST, RU, HOST1)),
         expect={RU: "person"}),
    case("reply-ru-pishet", "txt, Russian Thunderbird attribution line with пишет",
         [RU, RU_FIRST, RU_LAST],
         ["Cloud Backup and Recovery"],
         txt("reply.txt",
             "Добрый день!\n\nКак и говорила %s, подходит Cloud Backup and Recovery.\n\n"
             "05.10.2026 10:03, %s пишет:\n> Коллеги, предлагаю два варианта.\n" % (RU_FIRST, RU)),
         expect={RU: "person"}),
    case("reply-en-gmail", "txt, English Gmail attribution line, quoted salutation and sign-off with > prefix",
         [PLANTED_PERSON, FIRST, LAST, P2_FIRST],
         ["Elastic Load Balancer"],
         txt("reply.txt",
             "Thanks, that works for us.\n\nOn Mon, Oct 5, 2026 at 10:03 AM %s <%s> wrote:\n"
             "> Hi %s,\n>\n> the Elastic Load Balancer is ready.\n>\n> Best regards,\n> %s\n"
             % (PLANTED_PERSON, MAIL1, P2_FIRST, FIRST)),
         expect={PLANTED_PERSON: "person"}),
    case("reply-de-gmail-namefirst", "txt, German Gmail attribution with the name first (X <mail> schrieb am Mo., ...)",
         [PLANTED_PERSON, FIRST, LAST, P2_FIRST],
         ["Enterprise Router"],
         txt("reply.txt",
             "Danke %s, dann passt es.\n\n%s <%s> schrieb am Mo., 5. Okt. 2026, 10:03:\n\n"
             "> Hallo %s,\n> der Enterprise Router ist bestellt.\n" % (FIRST, PLANTED_PERSON, MAIL1, P2_FIRST)),
         expect={PLANTED_PERSON: "person"}),
    case("reply-no-date", "txt, attribution line without a date (X wrote:)",
         [PLANTED_PERSON, FIRST, LAST, P2_FIRST],
         ["s3.large.2"],
         txt("reply.txt",
             "%s, thanks, we take the s3.large.2 flavor then.\n\n%s wrote:\n> Hi %s,\n> please confirm the flavor.\n"
             % (FIRST, PLANTED_PERSON, P2_FIRST)),
         expect={PLANTED_PERSON: "person"}),
    case("reply-en-wrapped", "txt, Apple Mail attribution wrapped so that wrote: stands on the next line",
         [PLANTED_PERSON, FIRST, LAST, P2_FIRST],
         ["Direct Connect"],
         txt("reply.txt",
             "%s, agreed, Direct Connect it is.\n\nOn 5 Oct 2026, at 10:03, %s <%s>\nwrote:\n\n"
             "> Hi %s,\n> which uplink do you prefer?\n" % (FIRST, PLANTED_PERSON, MAIL1, P2_FIRST)),
         expect={PLANTED_PERSON: "person"}),

    # ------------------------------------------------------------------ salutations
    case("salut-double", "txt, two salutations on one line (Hallo X, hallo Y,)",
         [P2_FIRST, FIRST],
         ["Load Balancer"],
         txt("mail.txt", "Hallo %s, hallo %s,\n\nder Termin steht, der Load Balancer auch.\n" % (P2_FIRST, FIRST)),
         expect={P2_FIRST: "person"}),
    case("salut-inline-text", "txt, salutation followed by text on the same line (Hallo X, kurze Rückfrage ...)",
         [P2_FIRST],
         ["Load Balancer"],
         txt("mail.txt", "Hallo %s, kurze Rückfrage zum Load Balancer: passt Dienstag?\n" % P2_FIRST)),
    case("salut-comma-ru", "txt, Russian greeting with a comma before first name and patronymic (Здравствуйте, X Y!)",
         [RU_FIRST, RU_PATRONYMIC],
         ["Cloud Eye"],
         txt("mail.txt", "Здравствуйте, %s %s!\n\nСпасибо, Cloud Eye подходит.\n" % (RU_FIRST, RU_PATRONYMIC))),
    case("salut-name-first-ru", "txt, Russian greeting with the name first (X, добрый день!)",
         [RU_FIRST],
         ["Cloud Trace Service"],
         txt("mail.txt", "%s, добрый день!\n\nПодтверждаю, Cloud Trace Service включён.\n" % RU_FIRST)),
    case("salut-jan", "txt, a first name that is also a month abbreviation after Hallo",
         [P3_FIRST, P2_FIRST],
         ["NAT Gateway"],
         txt("mail.txt", "Hallo %s,\n\nder NAT Gateway ist eingerichtet.\n\nViele Grüße\n%s\n" % (P3_FIRST, P2_FIRST)),
         expect={P3_FIRST: "person", P2_FIRST: "person"}),

    # ------------------------------------------------------------------ sign-offs
    case("signoff-same-line", "txt, closing and signer on one line (LG X)",
         [FIRST],
         ["Enterprise Router"],
         txt("mail.txt", "Hallo zusammen,\n\nder Enterprise Router läuft.\n\nLG %s\n" % FIRST)),
    case("signoff-trailing-place", "txt, closing with a place after it (Viele Grüße aus X), signer below",
         [FIRST, CONTROL],
         ["Cloud Container Engine"],
         txt("mail.txt", "Hallo zusammen,\n\ndie Cloud Container Engine ist bestellt.\n\nViele Grüße aus %s\n%s\n"
             % (CONTROL, FIRST))),
    case("signoff-thanks-and", "txt, closing preceded by thanks (Vielen Dank und viele Grüße), signer below",
         [FIRST],
         ["Cloud Eye"],
         txt("mail.txt", "Hallo zusammen,\n\nCloud Eye zeigt alles grün.\n\nVielen Dank und viele Grüße\n%s\n" % FIRST)),
    case("signoff-mfg-singular", "txt, Mit freundlichem Gruß (declined adjective, singular), signer below",
         [FIRST],
         ["Elastic IP"],
         txt("mail.txt", "Hallo zusammen,\n\ndie Elastic IP ist zugewiesen.\n\nMit freundlichem Gruß\n%s\n" % FIRST)),
    case("signoff-bilingual", "txt, bilingual closing on one line (Mit freundlichen Grüßen / Best regards)",
         [PLANTED_PERSON, FIRST, LAST, BRAND],
         ["Direct Connect", "Senior Consultant"],
         txt("mail.txt",
             "Hallo zusammen,\n\nder Direct Connect ist bestellt, %s meldet sich wegen der Termine.\n\n"
             "Mit freundlichen Grüßen / Best regards\n%s\nSenior Consultant\n%s\n"
             % (FIRST, PLANTED_PERSON, PLANTED_COMPANY)),
         expect={PLANTED_PERSON: "person"}),
    case("signoff-ru-same-line", "txt, Russian closing and signer on one line (С уважением, X)",
         [RU_FIRST],
         ["Object Storage Service"],
         txt("mail.txt", "Добрый день!\n\nObject Storage Service подключён.\n\nС уважением, %s\n" % RU_FIRST)),
    case("signoff-company-first", "txt, the company line right after the closing, the signer below it",
         [PLANTED_PERSON, FIRST, LAST, PLANTED_COMPANY],
         ["Senior Consultant", "Cloud Firewall"],
         txt("mail.txt",
             "Hallo zusammen,\n\n%s meldet sich morgen wegen der Cloud Firewall.\n\n"
             "Mit freundlichen Grüßen\n\n%s\n\n%s\nSenior Consultant\n" % (LAST, PLANTED_COMPANY, PLANTED_PERSON)),
         expect={PLANTED_PERSON: "person", PLANTED_COMPANY: "company"}),
    case("signoff-no-closing", "txt, a first name alone on its own line at the end, no closing phrase",
         [P2_FIRST, FIRST],
         ["Cloud Firewall"],
         txt("mail.txt", "Hallo %s,\n\nmelde dich gern, die Cloud Firewall ist aktiv.\n\n%s\n%s\n"
             % (P2_FIRST, FIRST, PLANTED_COMPANY))),
    case("sig-caps-surname-bare", "txt, surname in capitals in the signature, the surname bare in the body",
         ["%s %s" % (P2_FIRST, P2_LAST.upper()), P2_FIRST, P2_LAST],
         ["Object Storage Service"],
         txt("mail.txt",
             "Hallo zusammen,\n\n%s kümmert sich um den Zugang zum Object Storage Service.\n\n"
             "Mit freundlichen Grüßen\n%s %s\n%s\n" % (P2_LAST, P2_FIRST, P2_LAST.upper(), PLANTED_COMPANY)),
         expect={"%s %s" % (P2_FIRST, P2_LAST.upper()): "person"}),

    # ------------------------------------------------------------------ signatures, footers, products
    case("sig-tsystems-gmbh", "txt, a signature block of the vendor with its legal entity, street, town, phone, mail",
         [PLANTED_PERSON, FIRST, LAST, P2_FIRST, STREET, CONTROL],
         ["T-Systems International GmbH", "Senior Consultant Cloud", "Direct Connect"],
         txt("mail.txt",
             "Hallo %s,\n\ndie Freigabe für den Direct Connect liegt vor.\n\nMit freundlichen Grüßen\n%s\n"
             "Senior Consultant Cloud\nT-Systems International GmbH\n%s, 12345 %s\nTel. %s\nx.pomblet@t-systems.example\n"
             % (P2_FIRST, PLANTED_PERSON, STREET, CONTROL, PHONE)),
         expect={PLANTED_PERSON: "person", STREET: "place"}),
    case("sig-role-compound", "txt, a German compound role word in the signature (Teamleiter Cloud Operations)",
         [PLANTED_PERSON, FIRST, LAST, BRAND],
         ["Teamleiter Cloud Operations", "Cloud Eye"],
         txt("mail.txt",
             "Hallo zusammen,\n\nCloud Eye ist eingerichtet.\n\nViele Grüße\n%s\nTeamleiter Cloud Operations\n%s\n"
             % (PLANTED_PERSON, PLANTED_COMPANY)),
         expect={PLANTED_PERSON: "person"}),
    case("footer-legal-de", "txt, a German legal footer (Sitz der Gesellschaft, Registergericht, Geschäftsführer)",
         [CONTROL, PLANTED_PERSON, FIRST, LAST, BRAND],
         ["Sitz der Gesellschaft", "Registergericht", "Direct Connect"],
         txt("mail.txt",
             "Hallo zusammen,\n\nder Direct Connect ist aktiv.\n\nViele Grüße\n%s\n\n"
             "%s | Sitz der Gesellschaft: %s | Registergericht: Amtsgericht %s, HRB 12345 | "
             "Geschäftsführer: %s | USt-IdNr.: DE123456789\n"
             % (FIRST, PLANTED_COMPANY, CONTROL, CONTROL, PLANTED_PERSON)),
         expect={PLANTED_PERSON: "person"}),
    case("footer-two-managers", "txt, two managing directors in a legal footer (Geschäftsführer: X Y, A B)",
         [PLANTED_PERSON, P2, FIRST, LAST, P2_FIRST, P2_LAST],
         ["Geschäftsführer", "Cloud Eye"],
         txt("mail.txt",
             "Hallo zusammen,\n\nCloud Eye ist eingerichtet.\n\nViele Grüße\n%s\n\n"
             "%s | Geschäftsführer: %s, %s | HRB 12345\n" % (P5_FIRST, PLANTED_COMPANY, PLANTED_PERSON, P2)),
         expect={PLANTED_PERSON: "person", P2: "person"}),
    case("address-one-line-comma", "txt, a one-line address in a footer (company, street number, postcode town)",
         [STREET, CONTROL],
         ["Cloud Eye"],
         txt("mail.txt",
             "Hallo zusammen,\n\nCloud Eye ist eingerichtet.\n\nViele Grüße\n%s\n\n%s, %s, 12345 %s\n"
             % (P5_FIRST, PLANTED_COMPANY, STREET, CONTROL)),
         expect={STREET: "place"}),
    case("street-hyphen-capital", "txt, a street written with a hyphen and a capital Straße in the signature",
         [STREET_HYPHEN, STREET_WORD, CONTROL, FIRST],
         ["Direct Connect"],
         txt("mail.txt",
             "Hallo zusammen,\n\nder Direct Connect ist aktiv.\n\nViele Grüße\n%s\n%s\n%s\n12345 %s\n"
             % (FIRST, PLANTED_COMPANY, STREET_HYPHEN, CONTROL))),
    case("attachment-name-brand", "eml, the company brand inside the file name of an attachment",
         [BRAND],
         ["Angebot"],
         eml("offer.eml",
             [("From", "info@%s" % HOST2), ("To", MAIL2), ("Subject", "Angebot"),
              ("Date", "Mon, 5 Oct 2026 10:03:00 +0200")],
             "Hallo zusammen,\n\nanbei das Angebot.\n",
             attachments=[("Angebot_%s_Beratung_2026-10.txt" % BRAND, b"Angebot im Portal hinterlegt.\n", "text", "plain")])),
    case("ics-attachment", "eml, a calendar invite as attachment: CN parameters, escaped DESCRIPTION with salutation and sign-off",
         [PLANTED_PERSON, FIRST, LAST, "%s, %s" % (P2_LAST, P2_FIRST), P2_LAST, P2_FIRST, CONTROL, BRAND],
         ["Cloud Container Engine"],
         eml("invite.eml",
             [("From", "noreply@%s" % HOST2), ("To", MAIL2), ("Subject", "Einladung: Kickoff"),
              ("Date", "Mon, 5 Oct 2026 10:03:00 +0200")],
             "Einladung anbei.\n",
             attachments=[("invite.ics",
                           ics(["DESCRIPTION:Hallo %s\\,\\nhier die Einladung zum Kickoff.\\n\\nViele Grüße\\n%s"
                                % (P2_FIRST, FIRST)],
                               PLANTED_PERSON, "%s, %s" % (P2_LAST, P2_FIRST), "%s\\, 12345 %s" % (STREET, CONTROL),
                               "Kickoff %s / Cloud Container Engine" % PLANTED_COMPANY),
                           "text", "calendar")]),
         expect={PLANTED_PERSON: "person", "%s, %s" % (P2_LAST, P2_FIRST): "person"}),
    case("ics-folded-name", "eml, a calendar invite whose DESCRIPTION is folded (RFC 5545) inside a name",
         [LAST, FIRST],
         ["Cloud Container Engine"],
         eml("invite.eml",
             [("From", "noreply@%s" % HOST2), ("To", MAIL2), ("Subject", "Einladung: Kickoff"),
              ("Date", "Mon, 5 Oct 2026 10:03:00 +0200")],
             "Einladung anbei.\n",
             attachments=[("invite.ics",
                           ics(["DESCRIPTION:Bitte den Termin zur Cloud Container Engine mit %s" % FIRST[:3],
                                " %s %s bestätigen." % (FIRST[3:], LAST)],
                               "Kickoff", P2_FIRST + " " + P2_LAST, "12345 " + CONTROL, "Kickoff",
                               organizer_mail="xp@" + HOST1),
                           "text", "calendar")])),
    case("html-outlook-forward", "eml html, an Outlook forward: bold Von/Gesendet/An/Betreff block, salutations, signatures",
         [PLANTED_PERSON, FIRST, LAST, P5, P5_FIRST, P5_LAST, P2, P2_FIRST, P2_LAST, BRAND],
         ["Load Balancer"],
         eml("forward.eml",
             [("From", "%s <%s>" % (P5, MAIL5)), ("To", "%s <%s>" % (P2, MAIL2)), ("Subject", "WG: Bestellung Load Balancer"),
              ("Date", "Mon, 5 Oct 2026 10:03:00 +0200")],
             "<html><body>\n<p>Hallo %s,</p>\n<p>zur Info, siehe unten. Der Load Balancer ist bestellt.</p>\n"
             "<p>Viele Grüße<br>%s</p>\n"
             "<div style=\"border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0cm 0cm 0cm\">\n"
             "<p><b>Von:</b> %s &lt;%s&gt;<br>\n<b>Gesendet:</b> Montag, 5. Oktober 2026 10:03<br>\n"
             "<b>An:</b> %s, %s &lt;%s&gt;<br>\n<b>Betreff:</b> Bestellung Load Balancer</p>\n</div>\n"
             "<p>Hallo %s,</p>\n<p>die Bestellung ist raus.</p>\n<p>Mit freundlichen Grüßen<br>%s<br>%s</p>\n</body></html>\n"
             % (P2_FIRST, P5_FIRST, PLANTED_PERSON, MAIL1, P5_LAST, P5_FIRST, MAIL5, P5_FIRST, PLANTED_PERSON, PLANTED_COMPANY),
             html=True),
         expect={PLANTED_PERSON: "person"}),
    case("mbox-thread", "mbox, a thread of three mails, the surnames bare in the third",
         [PLANTED_PERSON, FIRST, LAST, P2, P2_FIRST, P2_LAST, BRAND],
         ["Elastic Cloud Server"],
         mbox("thread.mbox", [
             (MAIL1, message([("From", "%s <%s>" % (PLANTED_PERSON, MAIL1)), ("To", "%s <%s>" % (P2, MAIL2)),
                              ("Subject", "Bestellung"), ("Date", "Mon, 5 Oct 2026 10:03:00 +0200")],
                             "Hallo %s,\n\nder Elastic Cloud Server ist bestellt.\n\nViele Grüße\n%s\n%s\n"
                             % (P2_FIRST, PLANTED_PERSON, PLANTED_COMPANY))),
             (MAIL2, message([("From", "%s <%s>" % (P2, MAIL2)), ("To", "%s <%s>" % (PLANTED_PERSON, MAIL1)),
                              ("Subject", "AW: Bestellung"), ("Date", "Mon, 5 Oct 2026 11:03:00 +0200")],
                             "Hallo %s,\n\ndanke.\n\nViele Grüße\n%s\n\nAm 05.10.2026 um 10:03 schrieb %s:\n> der Elastic Cloud Server ist bestellt.\n"
                             % (FIRST, P2_FIRST, PLANTED_PERSON))),
             (MAIL1, message([("From", "%s <%s>" % (PLANTED_PERSON, MAIL1)), ("To", "%s <%s>" % (P2, MAIL2)),
                              ("Subject", "AW: AW: Bestellung"), ("Date", "Mon, 5 Oct 2026 12:03:00 +0200")],
                             "%s hat zugestimmt, %s meldet den Termin.\n" % (P2_LAST, LAST))),
         ])),
    case("subject-lone-surname", "eml, a bare surname as the Subject after a reply prefix, the sender a role mailbox",
         [LAST],
         ["Tel."],
         eml("callback.eml",
             [("From", "info@%s" % HOST1), ("To", MAIL2), ("Subject", "AW: %s" % LAST),
              ("Date", "Mon, 5 Oct 2026 10:03:00 +0200")],
             "Bitte zurückrufen, Nummer siehe unten.\nTel. %s\n" % PHONE)),
    case("vendor-domain-label", "eml, a vendor's label as the mail host of the sender, the vendor name alone in the body",
         [P2, P2_FIRST, P2_LAST],
         ["Proxmox"],
         eml("renewal.eml",
             [("From", "Sales <sales@proxmox.example>"), ("To", "%s <%s>" % (P2, MAIL2)), ("Subject", "Renewal"),
              ("Date", "Mon, 5 Oct 2026 10:03:00 +0200")],
             "Hello %s,\n\nyour Proxmox subscription renews in October. Proxmox support stays included.\n\n"
             "Best regards\nSales Team\n" % P2_FIRST)),
    case("product-unknown-word", "txt, a product name of two words where the second is outside every list (Juniper Mist)",
         [PLANTED_PERSON, FIRST, LAST, P2_FIRST],
         ["Juniper Mist", "Enterprise Switch"],
         txt("mail.txt",
             "Hallo %s,\n\nwir lösen Juniper Mist am Standort ab, der Enterprise Switch übernimmt.\n\nViele Grüße\n%s\n"
             % (P2_FIRST, PLANTED_PERSON)),
         expect={PLANTED_PERSON: "person"}),
    case("particle-von-vendor", "txt, a capitalised noun before von and the vendor after it (die Kollegen von T-Systems)",
         [P2_FIRST, FIRST],
         ["T-Systems", "Kollegen", "Direct Connect"],
         txt("mail.txt",
             "Hallo %s,\n\ndie Kollegen von T-Systems melden sich wegen des Direct Connect.\n\nViele Grüße\n%s\n"
             % (P2_FIRST, FIRST))),
    case("two-names-comma", "txt, exactly two full names joined by a comma in prose",
         [PLANTED_PERSON, P2, FIRST, LAST, P2_FIRST, P2_LAST, P5_FIRST],
         ["Enterprise Router"],
         txt("mail.txt",
             "Hallo zusammen,\n\nim Termin waren %s, %s und ich. Der Enterprise Router wurde freigegeben.\n\n"
             "Viele Grüße\n%s\n" % (PLANTED_PERSON, P2, P5_FIRST)),
         expect={PLANTED_PERSON: "person", P2: "person"}),
    case("learned-vocab-firstname", "txt, a first name that is a word of the public vocabulary, bare in the body and in the sign-off",
         ["%s %s" % (P4_FIRST, P4_LAST), P4_FIRST, P4_LAST, P2_FIRST],
         ["Elastic Load Balancer"],
         txt("mail.txt",
             "Hallo %s,\n\nwie mit %s besprochen, nehmen wir den Elastic Load Balancer.\n\nViele Grüße\n%s %s\n"
             % (P2_FIRST, P4_FIRST, P4_FIRST, P4_LAST)),
         expect={"%s %s" % (P4_FIRST, P4_LAST): "person"}),
    case("learned-suffix-surname", "txt, a surname with a German noun suffix, bare in the body after the sign-off learned it",
         [P5, P5_FIRST, P5_LAST, P2_FIRST],
         ["Elastic Volume Service"],
         txt("mail.txt",
             "Hallo %s,\n\nwie mit %s besprochen, bleibt der Elastic Volume Service.\n\nMit freundlichen Grüßen\n%s\n"
             % (P2_FIRST, P5_LAST, P5)),
         expect={P5: "person"}),
]
