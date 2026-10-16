"""The word lists of wipe mode: legal forms, company, title, role, label and section words, the lists of rules/
(first names, source platforms, standards, the German and Russian known words) and the checks that read them
(`_known_word`, `_person_known`, `_generic_word` ...). One family of the wipe rules (F1 step 8); the package text is
awb/intake/wipe/__init__.py.
"""
from __future__ import annotations

import re

from awb import codes
from awb.intake.wipe import _list
from awb.intake.wipe.basic import _FUNCTION_WORDS, _OPENERS, _STOP_SINGLES, _TOOL_WORDS
from awb.matcher import fold_text


# --------------------------------------------------------------------------- word lists of wipe mode

_LEGAL_WORDS = frozenset(w.casefold() for w in (
    "GmbH", "gGmbH", "mbH", "AG", "KG", "KGaA", "SE", "Ltd", "Limited", "Inc", "LLC", "LLP", "Corp", "Co", "eV",
    "eG", "OHG", "GbR", "UG", "plc", "SAS", "SARL", "BV", "NV", "ApS", "Oy", "Kanzlei", "PartG", "mbB", "Sp",
    "ООО", "ОАО", "ЗАО", "ПАО", "АО", "ИП", "НКО", "ГУП", "ФГУП", "ТОО",
))
_GENERIC_COMPANY_WORDS = frozenset(w.casefold() for w in (
    "Beratung", "Consulting", "Consultants", "Services", "Service", "Systems", "Systeme", "Solutions", "Software",
    "Logistik", "Logistics", "Technik", "Technologies", "Technology", "Group", "Gruppe", "Holding", "Partner",
    "Partners", "International", "Deutschland", "Germany", "Europe", "Europa", "Global", "Bank", "Versicherung",
    "Industrie", "Industries", "Industrial", "Engineering", "Networks", "Network", "Digital", "Data", "Cloud",
    "Media", "Medien", "Energie", "Energy", "Handel", "Trading", "Transport", "Spedition", "Immobilien", "Verlag",
    "Werke", "Werk", "Labs", "Lab", "Studio", "Agentur", "Agency", "Institut", "Institute", "Stiftung",
    "Foundation", "Verein", "Verband", "Association", "Company", "Unternehmen", "Firma", "Kanzlei", "Praxis",
    "Klinik", "Clinic", "Hospital", "Krankenhaus", "Universität", "University", "Hochschule", "Schule", "School",
    "Stadt", "Stadtwerke", "Gemeinde", "Landkreis", "Kreis", "Bundesamt", "Ministerium", "Ministry", "Amt",
    "Behörde", "Office", "Center", "Centre", "Zentrum", "Management", "Capital", "Invest", "Investment",
    "Automotive", "Pharma", "Chemie", "Chemicals", "Textil", "Food", "Retail", "Express", "Mobile", "Mobil",
    "Online", "Design", "Marketing", "Sales", "Vertrieb", "Entwicklung", "Development", "Research", "Forschung",
    "Innovation", "Innovations", "Communications", "Kommunikation", "Telecom", "Telekom", "Informatik", "Infra",
    "Infrastructure", "Infrastruktur", "Security", "Sicherheit", "Consult", "Projekt", "Project", "Projects",
    "Team", "Teams", "Support", "Operations", "Betrieb", "Portal", "Shop", "Store", "Market", "Markt", "Haus",
    "House", "Home", "Nord", "Süd", "West", "Ost", "North", "South", "East", "Mitte", "Central", "Neue", "Neuer",
    "Deutsche", "Deutscher", "Deutsches", "Erste", "Erster", "Allgemeine", "Allgemeiner", "Vereinigte",
    "Internationale", "Europäische", "European", "German", "Swiss", "Schweizer", "Austrian", "Österreichische",
))
_ROLE_WORDS = frozenset(w.casefold() for w in (
    "Senior", "Junior", "Lead", "Head", "Chief", "Manager", "Director", "Consultant", "Architect", "Engineer",
    "Developer", "Administrator", "Admin", "Analyst", "Specialist", "Expert", "Officer", "President", "Vice",
    "Executive", "Partner", "Associate", "Assistant", "Coordinator", "Owner", "Sales", "Account", "Key",
    "Projektleiter", "Projektleiterin", "Geschäftsführer", "Geschäftsführerin", "Leiter", "Leiterin", "Berater",
    "Beraterin", "Architekt", "Architektin", "Entwickler", "Entwicklerin", "Mitarbeiter", "Mitarbeiterin",
    "Vorstand", "Prokurist", "Abteilung", "Bereich", "Team", "CEO", "CTO", "CIO", "CFO", "COO", "CISO", "CSO",
    "Solutions", "Solution", "Cloud", "Security", "Product", "Produkt", "Service", "Services", "Customer",
    "Kunde", "Kunden", "Technical", "Technischer", "Technische", "Principal", "Staff", "Member", "Mitglied",
    "Presales", "Pre-Sales", "Delivery", "Operations", "Infrastructure", "Platform", "Plattform", "Data",
    "Business", "Development", "Enterprise", "Global", "Regional", "Digital", "Program", "Programme", "Project",
    "Projekt", "Portfolio", "Strategy", "Strategie", "Transformation", "Migration", "Governance", "Compliance",
    "Risk", "Quality", "Qualität", "Testing", "Test", "Support", "Trainer", "Dozent", "Referent", "Coach",
    "Mentor", "Intern", "Student", "Werkstudent", "Praktikant", "Freelancer", "Contractor", "Extern", "External",
    "Internal", "Intern", "Guest", "Gast", "Moderator", "Moderatorin", "Host", "Speaker", "Sprecher",
    "Interviewer", "Teilnehmer", "Teilnehmerin", "Participant", "Attendee", "Author", "Autor", "Editor",
    "Desk", "Hotline", "Helpdesk", "Servicedesk", "Service-Desk", "Center", "Centre", "Zentrale", "Leitstelle",
    "Operations", "Betrieb", "Team", "Office", "Backoffice", "Frontoffice",
    "руководитель", "руководителя", "директор", "директора", "начальник", "начальника", "заместитель", "заместителя",
    "менеджер", "менеджера", "инженер", "инженера", "архитектор", "архитектора", "администратор", "администратора",
    "специалист", "специалиста", "консультант", "консультанта", "аналитик", "аналитика", "разработчик", "разработчика",
    "ведущий", "старший", "младший", "главный", "генеральный", "технический", "коммерческий", "финансовый",
    "бухгалтер", "бухгалтера", "отдел", "отдела", "департамент", "департамента", "служба", "службы", "управление",
    "управления", "проекта", "проектов", "продаж", "разработки", "эксплуатации", "поддержки", "безопасности",
    "информационной", "информационных", "технологий", "инфраструктуры", "сопровождения", "внедрения",
))
_GENERIC_ADDRESSEES = frozenset(w.casefold() for w in (
    "zusammen", "alle", "all", "everyone", "everybody", "team", "folks", "guys", "both", "beide", "Damen", "Herren",
    "Sir", "Madam", "Sirs", "friends", "Freunde", "Leute", "miteinander", "colleagues", "Kollegen", "Kolleginnen",
    "Kollegin", "Kollege", "Mitarbeiter", "Mitarbeiterinnen", "Kunde", "Kunden", "Kundin", "Partner", "Nutzer",
    "User", "Users", "Mitglied", "Mitglieder", "Teilnehmer", "Teilnehmende", "Gäste", "Guests", "there", "world",
    "Welt", "again", "nochmal", "Herr", "Herrn", "Frau", "Mr", "Mrs", "Ms", "Dr", "Prof", "Dear", "liebe", "lieber",
    "коллеги", "друзья", "все", "всем", "команда", "господа", "уважаемые",
))
_TITLE_WORDS = frozenset(w.casefold() for w in (
    "Herr", "Herrn", "Frau", "Hr", "Fr", "Mr", "Mrs", "Ms", "Mx", "Dr", "Prof", "Dipl", "Ing", "Mag", "Sir", "Lord",
    "Lady", "Dame", "г-н", "г-жа", "господин", "госпожа", "тов", "товарищ", "уважаемый", "уважаемая", "Madame",
    "Monsieur", "Mme", "M", "Signor", "Signora", "Don", "Doña",
))
_PARTICLE_WORDS = frozenset(("von", "van", "de", "zu", "vom", "zur", "der", "den", "dem", "la", "le", "du", "del",
                             "della", "di", "da", "dos", "das", "el", "al", "bin", "ibn", "ben", "af", "av", "ter"))
_MONTHS_DAYS = frozenset(w.casefold() for w in (
    "Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November",
    "Dezember", "January", "February", "March", "May", "June", "July", "October", "December", "Montag", "Dienstag",
    "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag", "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday", "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug", "Sep", "Sept", "Oct", "Nov",
    "Dec", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun", "Mo", "Di", "Mi", "Do", "Fr", "Sa", "So",
    "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября",
    "декабря", "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
))
_NEUTRAL_LABELS = frozenset(w.casefold() for w in (
    "note", "notes", "hinweis", "hinweise", "betreff", "subject", "date", "datum", "time", "zeit", "uhrzeit",
    "summary", "zusammenfassung", "action", "actions", "action item", "action items", "todo", "to do", "todos",
    "decision", "decisions", "entscheidung", "entscheidungen", "question", "questions", "frage", "fragen",
    "answer", "antwort", "q", "a", "f", "moderator", "interviewer", "agenda", "topic", "topics", "thema", "themen",
    "result", "results", "ergebnis", "ergebnisse", "status", "next", "next steps", "nächste schritte", "source",
    "quelle", "warning", "warn", "error", "errors", "fehler", "info", "debug", "trace", "fatal", "critical",
    "example", "examples", "beispiel", "output", "input", "step", "steps", "schritt", "option", "options",
    "pro", "contra", "cons", "pros", "risk", "risks", "risiko", "risiken", "due", "deadline", "location", "ort",
    "attendees", "anwesend", "abwesend", "absent", "present", "re", "aw", "fw", "fwd", "wg", "cc", "to", "from",
    "von", "an", "kunde", "customer", "firma", "company", "note to self", "important", "wichtig", "tip", "tipp",
    "hint", "caution", "attention", "achtung", "usage", "syntax", "returns", "return", "raises", "args",
    "arguments", "parameters", "parameter", "description", "beschreibung", "title", "titel", "name", "id",
    "type", "typ", "value", "wert", "default", "see", "siehe", "references", "reference", "link", "links", "url",
    "path", "pfad", "file", "datei", "version", "stand", "author", "autor", "owner", "reviewer", "approver",
    "total", "summe", "gesamt", "sum", "note", "remark", "remarks", "bemerkung", "bemerkungen", "comment",
    "comments", "kommentar", "kommentare", "thanks", "danke", "ps", "pps", "nb", "key", "keys", "host", "hosts",
    "server", "client", "user", "password", "passwort", "login", "region", "zone", "project", "projekt", "image",
    "flavor", "flavour", "size", "price", "preis", "cost", "kosten", "unit", "quantity", "menge", "amount",
    "label", "labels", "tag", "tags", "scope", "goal", "ziel", "ziele", "context", "kontext", "background",
    "hintergrund", "problem", "solution", "lösung", "approach", "ansatz", "assumption", "assumptions", "annahme",
    "annahmen", "constraint", "constraints", "requirement", "requirements", "anforderung", "anforderungen",
    "speaker", "sprecher", "participant", "participants", "teilnehmer", "me", "ich", "you", "du", "sie", "system",
    "assistant", "bot", "ai", "chatgpt", "claude", "gpt", "model", "human", "user", "operator", "support", "sales",
    "presenter", "transcript", "recording", "chat", "message", "nachricht", "sender", "recipient", "empfänger",
    "absender", "phone", "telefon", "tel", "fax", "mail", "e-mail", "email", "mobile", "mobil", "web", "www",
    "http", "https", "ftp", "ssh", "smtp", "imap", "dns", "ip", "vpn", "api", "cli", "ui", "gui", "sdk", "os",
    "todo", "fixme", "xxx", "bug", "feature", "task", "aufgabe", "aufgaben", "milestone", "meilenstein", "phase",
    "sprint", "release", "build", "deploy", "deployment", "test", "tests", "prod", "dev", "stage", "staging",
    "примечание", "тема", "дата", "время", "вопрос", "ответ", "итог", "итоги", "решение", "решения", "статус",
    "задача", "задачи", "участники", "автор", "источник", "пример", "внимание", "важно", "спасибо", "ошибка",
    "file id", "part id", "kind", "state", "notes", "totals", "states", "outputs", "files", "customer", "date",
    "interviewee", "interviewed", "befragter", "befragte", "interviewpartner", "interviewpartnerin", "respondent",
    "fazit", "vorschlag", "vorschläge", "vereinbarung", "vereinbarungen", "beschluss", "beschlüsse", "aufgabe",
    "aufgaben", "nächster schritt", "offen", "erledigt", "klärung", "anmerkung", "anmerkungen", "bemerkung",
    "zusammenfassung", "protokoll", "begrüßung", "einleitung", "abschluss", "diskussion", "präsentation", "demo",
    "pause", "ende", "start", "top", "tagesordnungspunkt", "punkt", "notiz", "memo", "update", "feedback",
    "rückmeldung", "bedarf", "lösung", "termin", "budget", "angebot", "vertrag", "conclusion", "proposal",
    "agreement", "resolution", "clarification", "remark", "presentation", "break", "item", "need", "date",
    "offer", "contract", "hinweis", "wichtig", "achtung", "frage", "antwort", "vorname", "nachname", "e-mail",
    "telefon", "mobil", "rolle", "role", "funktion", "function", "firma", "abteilung", "department", "standort",
    "antwort", "ergebnis", "ergebnisse", "nächste schritte", "next steps", "open items", "offene punkte", "todo",
    "action items", "entscheidungen", "decisions", "teilnehmer", "attendees", "anwesend", "entschuldigt",
    "abwesend", "verteiler", "distribution", "anlagen", "attachments", "anhang", "attachment", "betreff", "subject",
    "von", "an", "cc", "bcc", "datum", "uhrzeit", "ort", "location", "dauer", "duration", "agenda", "ziel", "goal",
    "ziele", "goals", "status", "fortschritt", "progress", "risiken", "risks", "abhängigkeiten", "dependencies",
    "annahmen", "assumptions", "kosten", "costs", "nutzen", "benefit", "benefits", "aufwand", "effort", "zeitplan",
    "timeline", "meilensteine", "milestones", "verantwortlich", "responsible", "owner", "zuständig",
    "описание", "результат", "ожидаемый результат", "фактический результат", "шаги", "шаг", "предусловия",
    "предусловие", "постусловия", "статус", "комментарий", "комментарии", "действие", "действия", "проверка",
    "проверки", "условие", "условия", "данные", "вход", "выход", "ожидание", "причина", "следствие", "замечание",
    "замечания", "риск", "риски", "срок", "сроки", "приоритет", "категория", "тип", "версия", "окружение", "среда",
    "сценарий", "сценарии", "кейс", "тест", "тесты", "этап", "этапы", "цель", "цели", "задача", "задачи", "вывод",
    "выводы", "итого", "всего", "сумма", "количество", "стоимость", "цена", "название", "наименование", "номер",
    "адрес", "телефон", "почта", "email", "сайт", "контакты", "реквизиты", "подпись", "печать", "м.п.", "место",
))

_GERMAN_KNOWN = _list("known-words-de.txt")
"""German words of tender, minutes and architecture texts (rules/known-words-de.txt)."""

_COMPOUND_HEADS = (
    "verzeichnis", "blatt", "nachweis", "frist", "stelle", "bereich", "wesen", "referat", "amt", "ordnung", "satzung",
    "gesetz", "schein", "buch", "karte", "nummer", "kennung", "antrag", "bescheid", "erklärung", "preis",
    "erfassung", "verwaltung", "abrechnung", "steuerung", "regelung", "versorgung", "entsorgung", "beschaffung",
    "haltung", "wartung", "pflege", "betreuung", "beratung", "schulung", "bildung", "forschung", "lehre",
    "zentrum", "zentrale", "dienst", "werk", "werke", "betrieb", "betriebe", "leitung", "leiter", "leiterin",
    "architektur", "vergleich", "konzept", "modell", "plan", "planung", "bericht", "analyse", "übersicht",
    "strategie", "migration", "betrieb", "management", "verwaltung", "sicherheit", "lösung", "struktur", "prozess",
    "system", "systeme", "plattform", "umgebung", "landschaft", "anbindung", "verbindung", "schnittstelle",
    "schätzung", "rechnung", "kalkulation", "kosten", "preise", "liste", "tabelle", "matrix", "katalog",
    "handbuch", "dokument", "beschreibung", "anforderung", "anforderungen", "bedingungen", "vereinbarung",
    "vertrag", "angebot", "ausschreibung", "phase", "phasen", "schritt", "schritte", "plattformen", "dienste",
    "dienst", "service", "services", "netz", "netze", "netzwerk", "speicher", "server", "datenbank", "datenbanken",
    "cluster", "zone", "zonen", "region", "regionen", "standort", "standorte", "zugang", "zugriff", "verfahren",
    "richtlinie", "richtlinien", "vorgabe", "vorgaben", "kriterien", "kennzahlen", "ziele", "ziel", "bild",
    "situation", "zustand", "stand", "status", "ablauf", "abläufe", "workflow", "workflows", "test", "tests",
    "abnahme", "übergabe", "einführung", "umstellung", "umzug", "ablösung", "erneuerung", "modernisierung",
    "konsolidierung", "virtualisierung", "automatisierung", "standardisierung", "optimierung", "skalierung",
    "sicherung", "wiederherstellung", "notfall", "wartung", "support", "schulung", "dokumentation", "team",
    "teams", "leitung", "leiter", "leiterin", "verantwortung", "rolle", "rollen", "gremium", "gremien",
    "projekt", "projekte", "szenario", "fächer", "sätze", "zeiten", "fenster", "regel", "regeln", "eintrag", "einträge",
)
"""German compound nouns end in a known head: Zielarchitektur, Kostenvergleich, Betriebsmodell, Sollkonzept."""


def _compound_known(low: str) -> bool:
    return len(low) >= 7 and any(low.endswith(h) and len(low) >= len(h) + 3 for h in _COMPOUND_HEADS)

_SOURCE_PLATFORM_WORDS = _list("source-platforms.txt")
"""The vendors, products and services of the source platforms and of a typical estate (rules/source-platforms.txt)."""
_STANDARD_WORDS = _list("standards.txt")
_FIRST_NAMES = _list("first-names.txt")
"""Common first names of the languages of this host's material; a first name is a name even when the public corpus
carries the word (Peter, Michael, Jan, Max, Will, Mark) (rules/first-names.txt)."""
_RUSSIAN_KNOWN = _list("known-words-ru.txt")

_GERMAN_NOUN_SUFFIXES = ("ung", "ungen", "heit", "heiten", "keit", "keiten", "schaft", "schaften", "tion",
                         "tionen", "ität", "itäten", "ierung", "ierungen")
_GERMAN_SUFFIX_MIN = 8
_GERMAN_FUNCTION = _list("sentence-openers.txt", section="german-function")
"""German prepositions, conjunctions and adverbs that open a sentence with a capital: never a name, trimmed off a run."""
_EXTRA_KNOWN = _list("allowed-terms.txt", section="intake-seed", single=True)
"""Words the public corpus lacks that typical architecture, contract and agile texts carry (rules/allowed-terms.txt)."""
_ORG_WORDS = frozenset(("team", "teams", "abteilung", "gruppe", "leitung", "büro", "board", "gremium", "ausschuss",
                        "referat", "bereich", "support", "service", "desk", "office", "committee", "council",
                        "department", "unit", "division", "management", "vorstand", "geschäftsführung", "betriebsrat",
                        "projektteam", "projektleitung", "architekturteam", "netzwerkteam", "einkauf", "vertrieb",
                        "buchhaltung", "it", "edv", "rz", "hotline", "helpdesk", "servicedesk"))
_ORG_TAILS = ("team", "abteilung", "gruppe", "leitung", "büro", "board", "gremium", "ausschuss", "referat", "bereich",
              "support", "service", "desk", "office", "kreis", "runde", "stelle")
_SECTION_WORDS = frozenset(("teil", "kapitel", "abschnitt", "anhang", "anlage", "annex", "appendix", "section", "part",
                            "schedule", "exhibit", "punkt", "phase", "variante", "option", "szenario", "teilprojekt",
                            "los", "stufe", "welle", "block", "modul", "gruppe", "klasse", "typ", "plan", "paket",
                            "arbeitspaket", "meilenstein", "schritt", "stage", "step", "wave", "tier", "level",
                            "chapter", "attachment", "figure", "table", "tabelle", "abbildung", "bild", "format",
                            "version", "release", "zone", "region", "cluster", "site", "standort", "halle", "gebäude",
                            "raum", "rack", "reihe", "spalte", "zeile", "position", "pos", "nr", "no", "item"))
_GENERIC_LOCALS = frozenset(("support", "info", "sales", "docs", "doc", "help", "noreply", "no-reply", "service", "kontakt",
                             "contact", "office", "team", "helpdesk", "admin", "hostmaster", "postmaster", "webmaster",
                             "abuse", "security", "billing", "invoice", "rechnung", "bewerbung", "jobs", "karriere",
                             "career", "press", "presse", "marketing", "newsletter", "news", "mail", "mailer",
                             "notifications", "notification", "alerts", "alert", "bounce", "do-not-reply", "donotreply",
                             "vertrieb", "einkauf", "buchhaltung", "zentrale", "empfang", "reception", "hr", "it", "edv",
                             "rz", "noc", "soc", "cert", "ops", "devops", "orders", "order", "shop", "store", "feedback",
                             "privacy", "datenschutz", "legal", "recht", "compliance", "partner", "partners", "vendors"))
_GERMAN_ADJ = frozenset("""klein groß gross lang kurz jung alt weiß weiss schwarz braun roth rot grün gruen neu frisch kühn
fromm stark schnell ernst wild voll reich arm fein hart weich hell dunkel früh spät breit schmal hoch tief schön klug lieb
treu mutig sauer süß bitter scharf mild still laut leise froh lustig fest lose nah fern nett rein fertig gut böse klar
eng weit dick dünn leicht schwer warm kalt heiß nass trocken grob glatt rau bunt grau blau gelb rosa lila""".split())
"""German adjectives that are also surnames (Klein, Lang, Groß, Weiß): known words, so that a surname learned from
a title is matched capitalised only and the adjective in the same text stays."""
_PRODUCT_LABEL_RE = re.compile(
    r"(?im)^[ \t>*_-]*(?:tools?|werkzeuge?|produkte?|products?|software|komponenten?|components?|hersteller|vendor|"
    r"manufacturer|lösung(?:en)?|solutions?|technologien?|technolog(?:y|ies)|plattform(?:en)?|platforms?|stack|"
    r"module?|lizenz(?:en)?|licen[cs]es?|versionen?|versions?|dienste|systeme?|anwendung(?:en)?|applications?|"
    r"backup-?tool|monitoring|ticketsystem|betriebssysteme?|datenbank(?:en)?|middleware|hypervisor|firewall|"
    r"loadbalancer|virtualisierung)[ \t]*:[ \t]*(?P<v>[^\n]{1,160}?)[ \t]*$")
"""A key-value line whose label announces a product or a vendor: its value is never a name candidate."""
_PARTICIPLE_RE = re.compile(r"(?:ge|be|ver|er|ent|zer|ein|aus|auf|ab|an|vor|zu|um|über|unter|durch|nach|mit|weg|her|hin|fort|zusammen)"
                            r"[a-zäöüß]{2,}(?:ierte|ierten|iertes|ierter|ene|enen|enes|ener|ete|eten|etes|eter|te|ten|tes|ter)")
"""Eingesetzte, Betroffene, Verwendete, Migrierte: a German participle with a verb prefix and an adjective ending."""
_LETTER_LINE_RE = re.compile(r"^[ \t>*_-]*[A-ZÄÖÜА-ЯЁ]\.[ \t]+\S")


def _org_word(w: str) -> bool:
    low = w.casefold().strip(".,;:!?()[]\"'")
    return low in _ORG_WORDS or (len(low) >= 7 and low.endswith(_ORG_TAILS))


def _speech_shaped(v: str) -> bool:
    """The value after a speaker label speaks: a question or an exclamation, a sentence that ends in a full stop,
    a lower-case ordinary word in it (so passt es, Ok, danke), or six words and more. Ticketsystem der IT,
    Version 6.3, srv-bkp-01 and 20 TB are data."""
    v = v.strip()
    if not v:
        return False
    if "?" in v or "!" in v:
        return True
    ws = [w.strip(".,;:()\"'") for w in v.split()]
    ws = [w for w in ws if w]
    if len(ws) >= 2 and v.endswith((".", "…")):
        return True
    for w in ws:
        low = w.casefold()
        if (w[:1].islower() and w.isalpha() and len(w) >= 2 and low not in _FUNCTION_WORDS
                and low not in _PARTICLE_WORDS and low not in _UNIT_WORDS and low not in _SYSTEM_USERS
                and low not in _GERMAN_FUNCTION):
            return True
    return len(ws) >= 6


def _lettered_list(text: str, line_start: int) -> bool:
    """The line at line_start stands in a list of lettered lines (A. B. C.): a neighbouring non-blank line starts
    with another single letter and a dot."""
    before = [l for l in text[:line_start].split("\n")[-3:-1] if l.strip()]
    nl = text.find("\n", line_start)
    after = [l for l in text[nl + 1:nl + 200].split("\n")[:2] if l.strip()] if nl >= 0 else []
    own = text[line_start:nl if nl >= 0 else len(text)]
    neighbours = ([before[-1]] if before else []) + ([after[0]] if after else [])
    return any(_LETTER_LINE_RE.match(l) and l.strip()[:1] != own.strip()[:1] for l in neighbours)


def _generic_local(value: str) -> bool:
    """The local part of a mail address names a role, not a person: support@, info@, noreply@."""
    local = value.split("@")[0].casefold().strip("<>\"' ")
    parts = [p for p in re.split(r"[._+-]", local) if p]
    if local in _GENERIC_LOCALS or local in _SYSTEM_USERS:
        return True
    return bool(parts) and all(p in _GENERIC_LOCALS or p in _SYSTEM_USERS or p.isdigit()
                               or (p.isalpha() and len(p) >= 3 and p not in _FIRST_NAMES and _known_word(p)) for p in parts)


def _letters(word: str) -> int:
    return sum(1 for c in word if c.isalpha())


def _generic_word(word: str, months: bool = True) -> bool:
    """A word that is never a name in a strong shape: a function word, a stop word, a title, a generic addressee,
    a role word, a neutral label, a legal form, a number, and (unless months is False) a month or a weekday."""
    low = word.casefold().strip(".,;:!?()[]\"'")
    if not low or not any(c.isalpha() for c in low):
        return True
    if low in _FIRST_NAMES:
        return False
    return (low in _FUNCTION_WORDS or low in _STOP_SINGLES or low in _TOOL_WORDS
            or low in _TITLE_WORDS or low in _GENERIC_ADDRESSEES or low in _ROLE_WORDS or low in _NEUTRAL_LABELS
            or (months and low in _MONTHS_DAYS) or low in _LEGAL_WORDS or low in _PARTICLE_WORDS
            or low in _UNIT_WORDS or low in _GERMAN_FUNCTION or _role_shape(low))


_SQL_WORDS = frozenset(w.casefold() for w in (
    "TRUNCATE", "EXEC", "EXECUTE", "CALL", "GRANT", "REVOKE", "ANALYZE", "ANALYSE", "MERGE", "UPSERT", "VACUUM",
    "EXPLAIN", "COMMIT", "ROLLBACK", "BEGIN", "DECLARE", "SET", "SHOW", "USE", "WITH", "UNION", "EXCEPT", "INTERSECT",
    "HAVING", "LIMIT", "OFFSET", "FETCH", "OVER", "PARTITION", "WINDOW", "CASE", "WHEN", "THEN", "ELSE", "END", "CAST",
    "COALESCE", "NULLIF", "EXISTS", "BETWEEN", "LIKE", "ILIKE", "DISTINCT", "ASC", "DESC", "NULLS", "FIRST", "LAST",
    "RETURNING", "CONFLICT", "NOTHING", "CASCADE", "RESTRICT", "CONSTRAINT", "REFERENCES", "CHECK", "UNIQUE", "INDEX",
    "VIEW", "SEQUENCE", "TRIGGER", "FUNCTION", "PROCEDURE", "SCHEMA", "DATABASE", "ROLE", "OWNER", "TABLESPACE",
    "VARCHAR", "INTEGER", "BIGINT", "SMALLINT", "SERIAL", "BOOLEAN", "TIMESTAMP", "NUMERIC", "DECIMAL", "TEXT", "JSONB",
    "JSON", "UUID", "BYTEA", "CHAR", "DATE", "TIME", "INTERVAL", "ARRAY", "ENUM", "PRIMARY", "FOREIGN", "KEY", "NOT",
    "NULL", "DEFAULT", "AUTO_INCREMENT", "IDENTITY", "GENERATED", "ALWAYS", "STORED", "VIRTUAL", "COLLATE", "ENGINE",
    "CHARSET", "COMMENT", "LOCK", "UNLOCK", "TABLES", "FLUSH", "PRIVILEGES", "IDENTIFIED", "PASSWORD", "BACKUP",
    "RESTORE", "DUMP", "LOAD", "COPY", "IMPORT", "EXPORT", "OUTER", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL",
    "ORDER", "GROUP", "BY", "AS", "ON", "IN", "IS", "AND", "OR", "ALL", "ANY", "SOME", "TOP", "PERCENT", "ROWS",
    "ROW", "ONLY", "TIES", "RECURSIVE", "MATERIALIZED", "REFRESH", "CONCURRENTLY", "REINDEX", "CLUSTER", "LISTEN",
    "NOTIFY", "PREPARE", "DEALLOCATE", "SAVEPOINT", "RELEASE", "ISOLATION", "LEVEL", "READ", "WRITE", "SERIALIZABLE",
))
_CODE_KIND_WORDS = frozenset(w.casefold() for w in codes.KINDS) | frozenset(("fileid", "file", "code", "codes", "kind",
                                                                              "tcp", "hcs", "awb", "cust", "part"))
_UNIT_WORDS = frozenset(w.casefold() for w in (
    "TiB", "GiB", "MiB", "KiB", "PiB", "TB", "GB", "MB", "KB", "PB", "EB", "vCPU", "vCPUs", "GHz", "MHz", "Gbit", "Mbit",
    "Gbps", "Mbps", "Gbit/s", "Mbit/s", "IOPS", "SAS", "SATA", "SSD", "HDD", "NVMe", "RAM", "CPU", "GPU", "RPM", "ms",
    "Submillisecond", "Submillis", "EUR", "USD", "CHF", "Euro", "Cent", "Std", "Stk", "pcs", "Mio", "Mrd",
))


def _known_word(word: str, suffix: bool = True) -> bool:
    """A word the run rule may not take for a name in proposed mode: a function word, a stop word, an opener, a
    tool name, a legal form, a month, a unit, a word of the public corpus or of the German list, a word with a long
    German noun suffix (not when `suffix` is False: a surname may end in -ung), a number or a code. Ligatures and
    look-alike letters are folded first."""
    from awb.matcher import fold_text
    raw = word.casefold().strip(".,;:!?()[]\"'")
    low = fold_text(word).casefold().strip(".,;:!?()[]\"'")
    if not low:
        return True
    for w in {raw, low}:
        if (w in _FUNCTION_WORDS or w in _STOP_SINGLES or w in _TOOL_WORDS
                or w in _OPENERS or w in _LEGAL_WORDS or w in _MONTHS_DAYS or w in _VOCAB
                or w in _ROLE_WORDS or w in _SOURCE_PLATFORM_WORDS or w in _UNIT_WORDS or w in _STANDARD_WORDS
                or w in _RUSSIAN_KNOWN or (w in _SQL_WORDS and word.isupper()) or (w in _CODE_KIND_WORDS and word.isupper())):
            return True
    if not any(c.isalpha() for c in low):
        return True
    if low in _runstate._RUN_EXEMPT:
        return True   # a value under a product label (Hersteller: Kemp) is a term of this run
    if _role_shape(raw):
        return True
    if "-" in low and len(low) >= 5 and all(_letters(p) < 3 or _known_word(p, suffix=suffix) for p in low.split("-")):
        return True   # Non-Disclosure: known parts
    for ending in ("ern", "en", "er", "es", "e", "n", "s"):
        if low.endswith(ending) and len(low) - len(ending) >= 4 and low[:-len(ending)] in _VOCAB:
            return True   # Netze, Europas, Mandanten: a plural or a genitive of a known word
    if suffix and low not in _FIRST_NAMES and len(low) >= 7 and _PARTICIPLE_RE.fullmatch(low):
        return True   # Eingesetzte Werkzeuge: a participle opens the heading
    if suffix and len(low) >= _GERMAN_SUFFIX_MIN and low.endswith(_GERMAN_NOUN_SUFFIXES):
        return True
    if suffix and _compound_known(raw):
        return True
    return False


def _person_known(word: str) -> bool:
    """A word that cannot be a person's name: known, and not a first name of the list."""
    low = word.casefold().strip(".,;:!?()[]\"'")
    if low in _FIRST_NAMES:
        return False
    return _known_word(word, suffix=False)


_ROLE_SUFFIXES = ("leiter", "leiterin", "manager", "managerin", "berater", "beraterin", "architekt", "architektin",
                  "ingenieur", "ingenieurin", "entwickler", "entwicklerin", "experte", "expertin", "spezialist",
                  "spezialistin", "beauftragter", "beauftragte", "verantwortlicher", "verantwortliche",
                  "koordinator", "koordinatorin", "assistent", "assistentin", "referent", "referentin",
                  "administrator", "administratorin", "techniker", "technikerin", "sachbearbeiter",
                  "sachbearbeiterin", "vorstand", "direktor", "direktorin", "inhaber", "inhaberin", "consultant",
                  "engineer", "developer", "officer", "director", "owner", "lead", "head")


def _role_shape(low: str) -> bool:
    """A German or English compound role word (Teamleiterin, Abteilungsleiter, Projektmanager, Cloud-Architekt)."""
    low = low.replace("-", "")
    return len(low) >= 9 and any(low.endswith(x) and len(low) > len(x) + 2 for x in _ROLE_SUFFIXES)


# the names of the later families this one calls; they import this module, so these come after its own
from awb.intake.wipe.proposed import _SYSTEM_USERS  # noqa: E402
from awb.intake.wipe.runstate import _VOCAB  # noqa: E402
from awb.intake.wipe import runstate as _runstate  # noqa: E402  (bind rebinds _RUN_EXEMPT there)
