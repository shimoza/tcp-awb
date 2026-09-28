"""Derived forms: adjacent spellings of a registered name that a reader recognises but the register does not
list, and the boundary of what the matcher generates from a form. Same interface as smoke.py.

Every derived string here is built from tests/fixtures.py by a string operation, so the shapes stay tied to
the fixture register and no other name enters.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # the repository that holds this pack
from tests import fixtures as fx  # noqa: E402

FULL, SHORT, ACRONYM, ENGLISH = fx.CUSTOMER_FORMS
DOMAIN = fx.CUSTOMER_DOMAIN
FIRST_LAST, SURNAME = fx.PERSON_FORMS
FIRST = FIRST_LAST.split()[0]
ORG_FULL, ORG_SHORT = fx.ORG_FORMS
LAW_FULL, LAW_SHORT = fx.LAWFIRM_FORMS
PLACE = fx.PLACE_FORMS[0]
FILE_NUMBER = fx.FILE_NUMBER
TENDER_ID = fx.TENDER_ID

MARK = "stage two of the invented plan"


def txt(name, text):
    def build(inbox):
        p = inbox / name
        p.write_text(text, encoding="utf-8")
        return p
    return build


def case(cid, cls, carrier, values, text, name="note.txt", note=""):
    return {"id": cid, "cls": cls, "carrier": carrier, "values": list(values),
            "build": txt(name, text + "\n" + MARK + "\n"), "visible": [MARK], "note": note}


# ---- derived spellings of the fixture forms (string operations only)
LONG_UMLAUT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"})
SHORT_UMLAUT = str.maketrans({"ä": "a", "ö": "o", "ü": "u", "Ä": "A", "Ö": "O", "Ü": "U"})

SHORT_TYPO = SHORT[:-1] + "p"                                  # one letter off at the end
SHORT_SWAP = SHORT[:-2] + SHORT[-1] + SHORT[-2]                # last two letters swapped
SHORT_DOUBLED_MID = SHORT[:3] + SHORT[2] + SHORT[3:]           # a doubled inner letter
SHORT_DOUBLED_LAST = SHORT + SHORT[-1]                         # a doubled last letter
SHORT_MISSING = SHORT[:2] + SHORT[3:]                          # one inner letter missing
SURNAME_MISSING = SURNAME[:-1]                                 # last letter missing
ORG_SHORT_MISSING = ORG_SHORT[:3] + ORG_SHORT[4:]              # one inner letter missing
LAW_SHORT_SWAP = LAW_SHORT[:2] + LAW_SHORT[3] + LAW_SHORT[2] + LAW_SHORT[4:]
ACRONYM_SWAP = ACRONYM[0] + ACRONYM[2] + ACRONYM[1]
PLACE_ADJ = PLACE[:-5] + "städter"                             # the place as an adjective (umlaut)
PLACE_ADJ_AE = PLACE[:-5] + "staedter"
ORG_LONG = ORG_FULL.translate(LONG_UMLAUT)
ORG_SHORT_LONG = ORG_SHORT.translate(LONG_UMLAUT)
ORG_DROPPED = ORG_SHORT.translate(SHORT_UMLAUT)
DOMAIN_STEM = DOMAIN.rsplit(".", 1)[0]
DOMAIN_DE = DOMAIN_STEM + ".de"
DOMAIN_XYZ = DOMAIN_STEM + ".xyz"
DOMAIN_NOHYPHEN = DOMAIN.replace("-", "")
FILE_SLASH = FILE_NUMBER.replace("-", "/")
FILE_NOSEP = FILE_NUMBER.replace("-", "")
TENDER_SPACE = TENDER_ID.replace("-", " ")
TENDER_NOSEP = TENDER_ID.replace("-", "")
TENDER_PARTIAL = TENDER_ID.split("-", 1)[1]
TENDER_UNDERSCORE = TENDER_ID.replace("-", "_")
TENDER_LOWER_DOT = TENDER_ID.replace("-", ".").lower()
SLUG = FULL.lower().replace(" ", "-")
CAMEL = FULL.replace(" ", "") + "Client"
acr = ACRONYM.lower()
LOGIN_SURNAME_FIRST = "%s.%s" % (SURNAME.lower(), FIRST.lower())
LOGIN_INITIAL_GLUED = FIRST[0].lower() + SURNAME.lower()
LOGIN_INITIAL_HYPHEN = FIRST[0].lower() + "-" + SURNAME.lower()
LOGIN_SURNAME_INITIAL = SURNAME.lower() + FIRST[0].lower()
LOGIN_FIRST_INITIAL = FIRST.lower() + "." + SURNAME[0].lower()
FIRST_INITIAL = "%s %s." % (FIRST, SURNAME[0])
ACR_DOTTED = ".".join(ACRONYM) + "."
ACR_DOT_SPACE = ". ".join(ACRONYM) + "."
ACR_DOT_2SPACE = ".  ".join(ACRONYM) + "."
ACR_SPACED = " ".join(ACRONYM)
ACR_DASHED = " - ".join(ACRONYM)


CASES = [
    # ---- compounds and hyphen forms (the short form stays intact: promised)
    case("d-compound-gruppe", "name", "txt, short form + -Gruppe", [SHORT], "die %s-Gruppe hat zugesagt" % SHORT),
    case("d-compound-team", "name", "txt, short form-Logistik-Team", [SHORT], "das %s-Logistik-Team meldet" % SHORT),
    case("d-compound-place", "name", "txt, short form-place compound", [SHORT, PLACE], "Standort %s-%s" % (SHORT, PLACE)),
    # ---- other legal forms behind the short form
    case("d-legal-ag", "name", "txt, short form + Logistik AG", [SHORT], "Angebot an die %s Logistik AG" % SHORT),
    case("d-legal-cokg", "name", "txt, full form + & Co. KG", [FULL, SHORT], "Angebot an die %s & Co. KG" % FULL),
    case("d-legal-verwaltung", "name", "txt, short form + Logistik Verwaltungs GmbH", [SHORT],
         "Vertrag mit der %s Logistik Verwaltungs GmbH" % SHORT),
    case("d-english-noltd", "name", "txt, English form without Ltd", [SHORT], "offer for %s Logistics today" % SHORT),
    case("d-english-limited", "name", "txt, English form with Limited", [SHORT], "offer for %s Logistics Limited" % SHORT),
    case("d-abbrev-log", "name", "txt, second word abbreviated", [SHORT], "Angebot %s Log. und %s Logist. heute" % (SHORT, SHORT)),
    # ---- typos of the short forms: alone in a sentence, and inside a candidate shape
    case("d-typo-one-letter", "name", "txt, short form one letter off, alone", [SHORT_TYPO], "das Angebot an %s ist raus" % SHORT_TYPO),
    case("d-typo-swap", "name", "txt, short form two letters swapped, alone", [SHORT_SWAP], "das Angebot an %s ist raus" % SHORT_SWAP),
    case("d-typo-doubled-mid", "name", "txt, short form with a doubled inner letter", [SHORT_DOUBLED_MID], "das Angebot an %s ist raus" % SHORT_DOUBLED_MID),
    case("d-typo-doubled-last", "name", "txt, short form with a doubled last letter", [SHORT], "das Angebot an %s ist raus" % SHORT_DOUBLED_LAST),
    case("d-typo-missing", "name", "txt, short form with a letter missing", [SHORT_MISSING], "das Angebot an %s ist raus" % SHORT_MISSING),
    case("d-typo-in-company", "name", "txt, swapped short form before Logistik GmbH", [SHORT_SWAP], "Angebot an die %s Logistik GmbH" % SHORT_SWAP),
    case("d-typo-run", "name", "txt, swapped short form + capitalised word", [SHORT_SWAP], "Angebot an %s Logistik heute" % SHORT_SWAP),
    case("d-person-typo-alone", "name", "txt, surname with last letter missing, alone", [SURNAME_MISSING], "wie mit %s besprochen" % SURNAME_MISSING),
    case("d-person-typo-title", "name", "txt, surname typo after Herr", [SURNAME_MISSING], "Sehr geehrter Herr %s," % SURNAME_MISSING),
    case("d-org-typo-alone", "name", "txt, org short form with a letter missing", [ORG_SHORT_MISSING], "Lieferant ist %s seit 2024" % ORG_SHORT_MISSING),
    case("d-org-typo-company", "name", "txt, org typo before Präzision AG", [ORG_SHORT_MISSING], "Lieferant %s Präzision AG" % ORG_SHORT_MISSING),
    case("d-law-typo-kanzlei", "name", "txt, law firm typo after Kanzlei", [LAW_SHORT_SWAP], "vertreten durch Kanzlei %s" % LAW_SHORT_SWAP),
    case("d-law-typo-alone", "name", "txt, law firm typo alone", [LAW_SHORT_SWAP], "vertreten durch %s in dieser Sache" % LAW_SHORT_SWAP),
    case("d-acr-swap", "name", "txt, acronym letters swapped", [ACRONYM_SWAP], "Kürzel %s im Betreff" % ACRONYM_SWAP),
    # ---- plurals and adjectives
    case("d-plural", "name", "txt, short form plural", [SHORT], "beide %ss haben unterschrieben" % SHORT),
    case("d-adj-place", "name", "txt, place adjective with umlaut", [PLACE], "die %s Straße 5" % PLACE_ADJ),
    case("d-adj-place-ae", "name", "txt, place adjective written with ae, alone", [PLACE_ADJ_AE], "im %s Gewerbegebiet" % PLACE_ADJ_AE),
    case("d-adj-person", "name", "txt, surname adjective -sche", [SURNAME], "die %ssche Planung" % SURNAME),
    case("d-adj-cust", "name", "txt, short form adjective -er", [SHORT], "die %ser Niederlassung" % SHORT),
    # ---- the person
    case("d-firstname-alone", "name", "txt, first name alone in a salutation", [FIRST], "Hallo %s,\nanbei der Entwurf." % FIRST),
    case("d-firstname-initial", "name", "txt, first name + surname initial", [FIRST_INITIAL], "Rückfrage bei %s bitte" % FIRST_INITIAL),
    case("d-surname-title", "name", "txt, surname after Herrn Dr.", [SURNAME], "Sehr geehrter Herrn Dr. %s," % SURNAME),
    case("d-inverted", "name", "txt, Last, First", [SURNAME, FIRST], "Ansprechpartner: %s, %s" % (SURNAME, FIRST)),
    case("d-middle-initial", "name", "txt, First M. Last", [SURNAME, FIRST], "Termin mit %s M. %s" % (FIRST, SURNAME)),
    case("d-login-initial-glued", "name", "txt, login tsurname", [SURNAME], "user %s logged in" % LOGIN_INITIAL_GLUED),
    case("d-login-initial-hyphen", "name", "txt, login t-surname", [SURNAME], "user %s logged in" % LOGIN_INITIAL_HYPHEN),
    case("d-login-surname-initial", "name", "txt, login surnamet", [SURNAME], "user %s logged in" % LOGIN_SURNAME_INITIAL),
    case("d-login-surname-first", "name", "txt, login surname.first", [SURNAME, FIRST.lower()], "user %s logged in" % LOGIN_SURNAME_FIRST),
    case("d-login-first-initial", "name", "txt, login first.b", [LOGIN_FIRST_INITIAL], "user %s logged in" % LOGIN_FIRST_INITIAL),
    case("d-mail-orders", "mail", "txt, three mail addresses in other orders", [SURNAME, FIRST.lower()],
         "cc %s@example.org, %s@example.org, %s@example.org" % (LOGIN_SURNAME_FIRST, LOGIN_INITIAL_GLUED, LOGIN_FIRST_INITIAL)),
    # ---- the domain
    case("d-domain-tld-de", "url", "txt, domain with .de", [SHORT, DOMAIN_DE], "siehe %s heute" % DOMAIN_DE),
    case("d-domain-tld-xyz", "url", "txt, domain with an unlisted TLD", [SHORT, DOMAIN_XYZ], "siehe %s heute" % DOMAIN_XYZ),
    case("d-domain-nohyphen", "url", "txt, domain without the hyphen", [SHORT, DOMAIN_NOHYPHEN], "siehe %s heute" % DOMAIN_NOHYPHEN),
    case("d-subdomain", "url", "txt, subdomain of the registered domain", [DOMAIN], "vpn.%s ist erreichbar" % DOMAIN),
    case("d-host-acronym-affix", "name", "txt, acronym host with affixes", [ACRONYM], "host %s-vpn-01 und srv%s01 laufen" % (acr, acr)),
    # ---- the acronym glued with digits and affixes, in and outside the affix list
    case("d-acr-glue-affix-ok", "name", "txt, acronym + prod01 and backup", [ACRONYM], "hosts %sprod01 und %sbackup" % (acr, acr)),
    case("d-acr-glue-vm", "name", "txt, acronym + vm01", [acr + "vm01"], "host %svm01 ist down" % acr),
    case("d-acr-glue-fs-unc", "name", "txt, UNC path with acronym + fs01", [acr + "fs01"], "Pfad \\\\%sfs01\\daten\\plan" % acr),
    case("d-acr-glue-node", "name", "txt, acronym + node1", [acr + "node1"], "k8s %snode1 ready" % acr),
    case("d-acr-glue-client", "name", "txt, acronym + client in code", [acr + "client"], "import %sclient" % acr),
    case("d-acr-glue-share", "name", "txt, share name acronym + daten$", [acr + "daten"], "share \\\\srv01\\%sdaten$" % acr),
    case("d-acr-glue-upper-vm", "name", "txt, ACRONYM + vm01", [ACRONYM + "vm01"], "host %svm01 ist down" % ACRONYM),
    case("d-acr-digits-lower", "name", "txt, lower acronym + digits", [ACRONYM], "cluster %s2026 ist neu" % acr),
    # ---- the acronym with dots and spaces
    case("d-acr-dotted", "name", "txt, the acronym dotted", [ACR_DOTTED + " Angebot"], "%s Angebot liegt vor" % ACR_DOTTED),
    case("d-acr-dot-space", "name", "txt, the acronym dotted and spaced", [ACR_DOT_SPACE + " Angebot"], "%s Angebot liegt vor" % ACR_DOT_SPACE),
    case("d-acr-dot-2space", "name", "txt, the acronym dotted with two spaces (three chars between)", [ACR_DOT_2SPACE + " Angebot"], "%s Angebot liegt vor" % ACR_DOT_2SPACE),
    case("d-acr-spaced", "name", "txt, the acronym spaced", [ACR_SPACED + " Angebot"], "%s Angebot liegt vor" % ACR_SPACED),
    case("d-acr-dashed", "name", "txt, the acronym with spaced dashes", [ACR_DASHED + " Angebot"], "%s Angebot liegt vor" % ACR_DASHED),
    # ---- the acronym in identifiers of every kind
    case("d-acr-lower-sentence", "name", "txt, acronym lower case in a sentence", [ACRONYM], "die %s hat zugesagt" % acr),
    case("d-acr-hashtag", "name", "txt, hashtag", [ACRONYM], "siehe #%s für den Stand" % acr),
    case("d-acr-mention", "name", "txt, mention", [ACRONYM], "frag @%s heute" % acr),
    case("d-acr-winpath", "name", "txt, Windows path", [ACRONYM], "C:\\Daten\\%s\\Angebot.docx" % ACRONYM),
    case("d-acr-unc", "name", "txt, UNC path with hyphen", [ACRONYM], "\\\\%s-fs01\\daten" % acr),
    case("d-acr-share", "name", "txt, share name with $", [ACRONYM], "\\\\srv01\\%s$" % acr),
    case("d-acr-bucket", "name", "txt, bucket name", [ACRONYM], "obs://%s-backup-2026/plan" % acr),
    case("d-acr-git", "name", "txt, git remote", [ACRONYM], "git@git.example:%s/infra.git" % acr),
    case("d-acr-resource", "name", "txt, platform resource name", [ACRONYM], "ecs-%s-prod-01" % acr),
    case("d-slug", "name", "txt, slug of the full form", [FULL, SHORT], "ordner %s" % SLUG),
    case("d-camel", "name", "txt, CamelCase identifiers", [SHORT, ACRONYM], "class %s: get%sConfig()" % (CAMEL, ACRONYM.capitalize())),
    case("d-subject-prefix", "name", "txt, mail subject prefix", [ACRONYM], "Betreff: [%s] Angebot Q4" % ACRONYM),
    # ---- the file number and the tender id
    case("d-file-slash", "name", "txt, file number with slash", [FILE_NUMBER, FILE_SLASH], "Az. %s" % FILE_SLASH),
    case("d-file-nosep", "name", "txt, file number without separator", [FILE_NOSEP], "Az. %s" % FILE_NOSEP),
    case("d-file-prefix-suffix", "name", "txt, file number with prefix and suffix", [FILE_NUMBER], "Akte%s-1 und ref%s/2026" % (FILE_NUMBER, FILE_NUMBER)),
    case("d-tender-space", "name", "txt, tender id with spaces", [TENDER_ID, TENDER_SPACE], "Vergabe %s" % TENDER_SPACE),
    case("d-tender-nosep", "name", "txt, tender id without separators", [TENDER_NOSEP], "Vergabe %s" % TENDER_NOSEP),
    case("d-tender-partial", "name", "txt, tender id year and number only", [TENDER_PARTIAL], "Vergabe %s" % TENDER_PARTIAL),
    case("d-tender-underscore", "name", "txt, tender id with underscores", [TENDER_UNDERSCORE], "ordner %s" % TENDER_UNDERSCORE),
    case("d-tender-lower-dot", "name", "txt, tender id lower with dots", [TENDER_LOWER_DOT], "ordner %s" % TENDER_LOWER_DOT),
    case("d-tender-suffix", "name", "txt, tender id with -L1", [TENDER_ID], "Los %s-L1" % TENDER_ID),
    case("d-tender-prefixed", "name", "txt, tender id after Vergabe-Nr.", [TENDER_ID], "Vergabe-Nr. %s" % TENDER_ID),
    # ---- the place
    case("d-place-nord", "name", "txt, place-Nord", [PLACE], "Werk %s-Nord" % PLACE),
    case("d-place-address", "name", "txt, address line with postal code", [PLACE], "Beispielweg 5, 12345 %s" % PLACE),
    # ---- the law firm
    case("d-law-kanzlei", "name", "txt, Kanzlei + short form", [LAW_SHORT], "vertreten durch Kanzlei %s" % LAW_SHORT),
    case("d-law-partner", "name", "txt, short form & Partner", [LAW_SHORT], "vertreten durch %s & Partner" % LAW_SHORT),
    case("d-law-mbb", "name", "txt, short form Partner mbB", [LAW_SHORT], "vertreten durch %s Partner mbB" % LAW_SHORT),
    # ---- the org with umlaut spellings and genitives
    case("d-org-ae-oe", "name", "txt, org with ae and oe", [ORG_FULL, ORG_SHORT, ORG_LONG], "Lieferant %s" % ORG_LONG),
    case("d-org-dropped", "name", "txt, org short with umlaut dropped", [ORG_DROPPED], "Lieferant %s" % ORG_DROPPED),
    case("d-org-glued-ag", "name", "txt, org short with AG glued", [ORG_SHORT], "Lieferant %sAG" % ORG_SHORT),
    case("d-org-genitive-apos", "name", "txt, org genitive with apostrophe", [ORG_SHORT], "%s' Angebot" % ORG_SHORT),
    case("d-org-genitive-noapos", "name", "txt, org genitive without apostrophe", [ORG_SHORT], "%ss Angebot" % ORG_SHORT),
    case("d-org-mixed-umlaut", "name", "txt, org oe in first word, umlaut in second", [ORG_SHORT, ORG_SHORT_LONG],
         "Lieferant %s %s" % (ORG_SHORT_LONG, ORG_FULL.split(" ", 1)[1])),
]


# ---- round 2: the boundary of every escape, and the Markdown carriers
def md(cid, cls, carrier, values, text, note=""):
    return case(cid, cls, carrier, values, text, name="note.md", note=note)


PLACE_AE = PLACE[:-5] + "staedt"
LOGISTIK_TYPO = FULL.split()[1][:-1] + "g"      # second word of the full form, last letter off
FULL_SECOND_TYPO = " ".join([FULL.split()[0], LOGISTIK_TYPO, FULL.split()[2]])
DOMAIN_TYPO = DOMAIN.replace(SHORT.lower(), SHORT_SWAP.lower())
FILE_NOZERO = FILE_NUMBER.replace("-0", "-")
TENDER_PARTS = TENDER_ID.split("-")
TENDER_PREFIX_NUMBER = TENDER_PARTS[0] + "-" + TENDER_PARTS[2]
TENDER_REVERSED = TENDER_PARTS[2] + "/" + TENDER_PARTS[1]
TENDER_NOZERO = TENDER_ID.replace("-0", "-")
ACR_3SPACE = "   ".join(ACRONYM)
ACR_UNDERSCORE = "_".join(ACRONYM)
ACR_DOTTED_NOFINAL = ".".join(ACRONYM)

CASES += [
    # acronym glue: the boundary of _short_glued
    case("d2-acr-glue-hyphen-vm", "name", "txt, acronym-vm01 (one separator)", [ACRONYM], "host %s-vm01 ist down" % acr),
    case("d2-acr-glue-vm-nodigits", "name", "txt, acronym + vm, no digits", [acr + "vm"], "host %svm ist down" % acr),
    case("d2-acr-glue-both-sides", "name", "txt, prod + acronym + vm01", [ "prod" + acr + "vm01"], "host prod%svm01 ist down" % acr),
    case("d2-acr-glue-platform", "name", "txt, acronym + platform service names", [acr + "ecs01", acr + "obs", acr + "rds01"],
         "ressourcen %secs01, %sobs und %srds01" % (acr, acr, acr)),
    case("d2-acr-glue-roles", "name", "txt, acronym + ad01 dns01 hv01 esx01 nas01",
         [acr + "ad01", acr + "dns01", acr + "hv01", acr + "esx01", acr + "nas01"],
         "hosts %sad01 %sdns01 %shv01 %sesx01 %snas01" % (acr, acr, acr, acr, acr)),
    case("d2-acr-glue-caps", "name", "txt, ACRONYMVM01 all capitals", [ACRONYM + "VM01"], "host %sVM01 ist down" % ACRONYM),
    case("d2-acr-glue-1a", "name", "txt, acronym + 1a", [acr + "1a"], "host %s1a ist down" % acr),
    case("d2-acr-glue-it", "name", "txt, acronym + it01", [acr + "it01"], "host %sit01 ist down" % acr),
    case("d2-acr-glue-digits-between", "name", "txt, acronym + 01 + vm", [acr + "01vm"], "host %s01vm ist down" % acr),
    case("d2-acr-glue-affix-then-rest", "name", "txt, acronym + prod + vm01", [acr + "prodvm01"], "host %sprodvm01 ist down" % acr),
    case("d2-acr-dotted-nofinal", "name", "txt, the acronym dotted without the final dot", [ACR_DOTTED_NOFINAL + " Angebot"], "%s Angebot liegt vor" % ACR_DOTTED_NOFINAL),
    case("d2-acr-underscore", "name", "txt, the acronym underscored as an identifier", [ACR_UNDERSCORE + "_prod"], "bucket %s_prod" % ACR_UNDERSCORE),
    case("d2-acr-3space", "name", "txt, the acronym with three spaces between the letters", [ACR_3SPACE + " Angebot"], "%s Angebot liegt vor" % ACR_3SPACE),
    # typos: the boundary of the candidate rules
    case("d2-typo-inverted", "name", "txt, surname typo, Last, First", [SURNAME_MISSING], "Ansprechpartner: %s, %s" % (SURNAME_MISSING, FIRST)),
    case("d2-org-typo-ag", "name", "txt, org typo + AG", [ORG_SHORT_MISSING], "Lieferant %s AG" % ORG_SHORT_MISSING),
    case("d2-typo-second-word", "name", "txt, full form with a typo in the second word", [SHORT], "Angebot an die %s" % FULL_SECOND_TYPO),
    case("d2-typo-doubled-first", "name", "txt, short form with a doubled first letter", [SHORT], "das Angebot an %s ist raus" % (SHORT[0] + SHORT)),
    case("d2-typo-lower-alone", "name", "txt, swapped short form in lower case", [SHORT_SWAP.lower()], "das angebot an %s ist raus" % SHORT_SWAP.lower()),
    case("d2-typo-in-domain", "url", "txt, swapped short form inside the domain", [DOMAIN_TYPO], "siehe %s heute" % DOMAIN_TYPO),
    case("d2-typo-in-mail", "mail", "txt, surname typo in a mail address", [SURNAME_MISSING], "an t.%s@example.org" % SURNAME_MISSING.lower()),
    case("d2-typo-transposed-first", "name", "txt, first two letters of the short form swapped", [SHORT[1] + SHORT[0] + SHORT[2:]],
         "das Angebot an %s ist raus" % (SHORT[1] + SHORT[0] + SHORT[2:])),
    # the first name: the boundary of the title and run rules
    case("d2-firstname-title", "name", "txt, Herr + first name", [FIRST], "Sehr geehrter Herr %s," % FIRST),
    case("d2-surname-first-nocomma", "name", "txt, Last First without comma", [SURNAME, FIRST], "Ansprechpartner %s %s" % (SURNAME, FIRST)),
    md("d2-firstname-md-table", "name", "md table, surname and first name in two cells", [SURNAME, FIRST],
       "| Name | Vorname |\n|---|---|\n| %s | %s |" % (SURNAME, FIRST)),
    case("d2-initial-surname", "name", "txt, T. Surname", [SURNAME], "Rückfrage bei %s. %s" % (FIRST[0], SURNAME)),
    case("d2-firstname-lower-login", "name", "txt, first name alone as a login", [FIRST.lower()], "user %s logged in" % FIRST.lower()),
    # the place with ae
    case("d2-place-ae-alone", "name", "txt, place with ae alone", [PLACE_AE], "Standort in %s" % PLACE_AE),
    case("d2-place-ae-lower", "name", "txt, place adjective with ae in lower case", [PLACE_ADJ_AE.lower()], "im %s gewerbegebiet" % PLACE_ADJ_AE.lower()),
    # numbers
    case("d2-file-leading-zero", "name", "txt, file number without the leading zero", [FILE_NOZERO], "Az. %s" % FILE_NOZERO),
    case("d2-file-spaces-around", "name", "txt, file number with spaces around the hyphen", [FILE_NUMBER], "Az. %s" % FILE_NUMBER.replace("-", " - ")),
    case("d2-tender-prefix-number", "name", "txt, tender prefix + number only", [TENDER_PREFIX_NUMBER], "Vergabe %s" % TENDER_PREFIX_NUMBER),
    case("d2-tender-reversed", "name", "txt, tender number/year", [TENDER_REVERSED], "Vergabe %s" % TENDER_REVERSED),
    case("d2-tender-nozero", "name", "txt, tender id without the leading zero", [TENDER_NOZERO], "Vergabe %s" % TENDER_NOZERO),
    # domain and AD shapes
    case("d2-ad-domain-user", "name", "txt, NetBIOS domain\\user", [SHORT, SURNAME], "login %s\\%s" % (DOMAIN_STEM.upper(), LOGIN_INITIAL_GLUED)),
    case("d2-domain-xyz-rest", "url", "txt, unlisted TLD: what stays after the short form", [SHORT, DOMAIN_STEM.split("-", 1)[1] + ".xyz"],
         "siehe %s heute" % DOMAIN_XYZ),
    # Markdown carriers
    md("d2-md-typo-heading", "name", "md heading with the swapped short form", [SHORT_SWAP], "# Angebot %s\n\ntext" % SHORT_SWAP),
    md("d2-md-glue-codeblock", "name", "md fenced code with acronym + vm01", [acr + "vm01"], "```\nssh %svm01\n```" % acr),
    md("d2-md-link-domain-xyz", "url", "md link with an unlisted TLD", [SHORT], "[Portal](https://portal.%s/)" % DOMAIN_XYZ),
    md("d2-md-inverted-list", "name", "md list item Last, First", [SURNAME, FIRST], "- %s, %s (IT)" % (SURNAME, FIRST)),
]


# ---- round 3: the near shapes the same rules should catch, and the label rule as a candidate boundary
TENDER_EN_DASH = TENDER_ID.replace("-", "–")
SURNAME_PLURAL_UMLAUT = SURNAME[:-4] + "männer"     # -mann -> -männer

CASES += [
    case("d3-acr-compound-upper-lower", "name", "txt, ACRONYM glued before a capitalised word", [ACRONYM], "das %sAngebot liegt vor" % ACRONYM),
    case("d3-acr-srv-prefix-caps", "name", "txt, srvACRONYM and ACRONYMProd01", [ACRONYM], "hosts srv%s und %sProd01" % (ACRONYM, ACRONYM)),
    case("d3-acr-plural-digits", "name", "txt, acronym plural variant + digits", [ACRONYM], "host %ss01 läuft" % acr),
    case("d3-short-glued-digits", "name", "txt, short form + digits in a host", [SHORT], "host %s01-prod läuft" % SHORT.lower()),
    case("d3-acr-obs-host", "url", "txt, bucket host on the platform domain", [ACRONYM], "https://%s-backup.obs.eu-de.otc.t-systems.com/plan" % acr),
    case("d3-cost-centre", "name", "txt, cost centre code with the acronym", [ACRONYM], "KST-%s-4711" % ACRONYM),
    case("d3-acr-prefix-caps", "name", "txt, one capital + ACRONYM + digits", ["P" + ACRONYM + "01"], "Projekt P%s01 startet" % ACRONYM),
    case("d3-person-two-lines-inverted", "name", "txt, surname then first name on the next line", [SURNAME, FIRST], "%s\n%s\nIT" % (SURNAME, FIRST)),
    case("d3-org-full-lower-ae", "name", "txt, org full form lower case with ae and oe", [ORG_FULL, ORG_LONG], "lieferant %s" % ORG_LONG.lower()),
    case("d3-english-genitive", "name", "txt, English form with 's", [ENGLISH], "%s's offer" % ENGLISH),
    case("d3-domain-acr-xyz", "name", "txt, acronym domain with an unlisted TLD", [ACRONYM], "siehe %s.xyz heute" % acr),
    case("d3-tender-en-dash", "name", "txt, tender id with en dashes", [TENDER_ID, TENDER_EN_DASH], "Vergabe %s" % TENDER_EN_DASH),
    case("d3-file-letter-after", "name", "txt, file number with a letter glued after", [FILE_NUMBER], "ref%sa" % FILE_NUMBER),
    case("d3-surname-plural-umlaut", "name", "txt, surname plural with umlaut", [SURNAME], "die %s" % SURNAME_PLURAL_UMLAUT),
    case("d3-label-typo", "name", "txt, Kunde: + swapped short form", [SHORT_SWAP], "Kunde: %s" % SHORT_SWAP),
    case("d3-label-firstname", "name", "txt, Kunde: + first name", [FIRST], "Kunde: %s" % FIRST),
    case("d3-acr-typo-company", "name", "txt, swapped acronym + GmbH", [ACRONYM_SWAP], "Kunde %s GmbH" % ACRONYM_SWAP),
    case("d3-acr-typo-resource", "name", "txt, swapped acronym in a resource name", [ACRONYM_SWAP], "ecs-%s-prod-01" % ACRONYM_SWAP.lower()),
    case("d3-ansprechpartner-firstname", "name", "txt, Ansprechpartner: + first name (no label rule)", [FIRST], "Ansprechpartner: %s" % FIRST),
]


# ---- round 4: more near shapes, and the boundary of the withholding net for compressed numbers
CASES += [
    case("d4-legal-se", "name", "txt, short form + Logistik SE", [SHORT], "Angebot an die %s Logistik SE" % SHORT),
    case("d4-legal-holding", "name", "txt, short form + Holding GmbH", [SHORT], "Vertrag mit der %s Holding GmbH" % SHORT),
    case("d4-english-inc", "name", "txt, short form + Logistics Inc.", [SHORT], "offer for %s Logistics Inc. today" % SHORT),
    case("d4-short-ampersand", "name", "txt, short form & Co.", [SHORT], "Angebot an %s & Co. heute" % SHORT),
    case("d4-person-double-surname", "name", "txt, full person + -short form", [FIRST_LAST, SHORT], "Termin mit %s-%s" % (FIRST_LAST, SHORT)),
    case("d4-login-upn", "mail", "txt, tsurname at another domain", [SURNAME], "upn %s@corp.example" % LOGIN_INITIAL_GLUED),
    case("d4-acr-vlan", "name", "txt, vlan_acronym_10", [ACRONYM], "vlan_%s_10 ist frei" % acr),
    case("d4-acr-json-key", "name", "txt, json key acronymHost", [ACRONYM], "{\"%sHost\": \"x\"}" % acr),
    case("d4-label-customer-typo", "name", "txt, Customer: + short form one letter off", [SHORT_TYPO], "Customer: %s" % SHORT_TYPO),
    case("d4-tender-year-swapped", "name", "txt, tender prefix-number-year", [TENDER_PARTS[0] + "-" + TENDER_PARTS[2] + "-" + TENDER_PARTS[1]],
         "Vergabe %s-%s-%s" % (TENDER_PARTS[0], TENDER_PARTS[2], TENDER_PARTS[1])),
    case("d4-file-in-path", "name", "txt, file number as a path segment", [FILE_NUMBER], "/akten/%s/plan.pdf" % FILE_NUMBER),
    case("d4-place-genitive", "name", "txt, place genitive", [PLACE], "%ss Werk" % PLACE),
    case("d4-org-dative-plural", "name", "txt, org short form dative plural", [ORG_SHORT], "den %sen" % ORG_SHORT),
    case("d4-acr-affix-chain", "name", "txt, srv + acronym + db01", [ACRONYM], "host srv%sdb01" % acr),
    md("d4-short-md-code", "name", "md inline code with the short form", [SHORT], "run `%s-prod` now" % SHORT.lower()),
    case("d4-file-nosep-glued", "name", "txt, Az glued to the compressed file number", ["Az" + FILE_NOSEP], "Akte Az%s" % FILE_NOSEP),
    case("d4-tender-nosep-suffix-glued", "name", "txt, compressed tender id with L1 glued", [TENDER_NOSEP + "L1"], "Los %sL1" % TENDER_NOSEP),
    case("d4-tender-nosep-suffix-hyphen", "name", "txt, compressed tender id with -L1", [TENDER_NOSEP], "Los %s-L1" % TENDER_NOSEP),
]


# ---- round 5: confirmation, near shapes predicted caught; one re-test in neutral text
R5 = [
    case("d5-file-nosep-glued-neutral", "name", "txt, Az glued to the compressed file number, neutral text", ["Az" + FILE_NOSEP], "unter Az%s abgelegt" % FILE_NOSEP),
    case("d5-acr-hashtag-digits", "name", "txt, #acronym2026", [ACRONYM], "siehe #%s2026 für den Stand" % acr),
    case("d5-acr-mention-underscore", "name", "txt, @acronym_it", [ACRONYM], "frag @%s_it heute" % acr),
    case("d5-short-hashtag", "name", "txt, #shortform", [SHORT], "siehe #%s heute" % SHORT.lower()),
    case("d5-surname-hr-abbrev", "name", "txt, Hr. + surname", [SURNAME], "Hr. %s hat zugesagt" % SURNAME),
    case("d5-place-in-allowed-url", "name", "txt, place slug inside an allow-listed URL", [PLACE], "https://www.example.org/standorte/%s-nord" % PLACE.lower()),
    case("d5-short-in-allowed-url", "name", "txt, short form as a path segment of an allow-listed host", [SHORT], "https://github.com/%s/infra" % DOMAIN_STEM),
    case("d5-acr-in-allowed-url", "name", "txt, acronym as a path segment of an allow-listed host", [ACRONYM], "https://github.com/%s/infra" % acr),
    case("d5-acr-in-allowed-url-glued", "name", "txt, acronym + devops as a path segment", [acr + "devops"], "https://github.com/%sdevops/infra" % acr),
    case("d5-tender-in-subject", "name", "txt, tender id in a reply subject", [TENDER_ID], "AW: %s // Fragen" % TENDER_ID),
    case("d5-file-with-year", "name", "txt, file number/year", [FILE_NUMBER], "Az. %s/26" % FILE_NUMBER),
    case("d5-org-hyphen-compound", "name", "txt, org short form-Werk", [ORG_SHORT], "im %s-Werk" % ORG_SHORT),
    case("d5-law-no-ampersand", "name", "txt, Kanzlei short form Partner mbB (no &)", [LAW_SHORT], "Kanzlei %s Partner mbB" % LAW_SHORT),
    case("d5-domain-upper-nohyphen-de", "url", "txt, DOMAIN without hyphen, upper case, .de", [SHORT], "siehe %s" % (DOMAIN_NOHYPHEN.rsplit(".", 1)[0] + ".de").upper()),
]
# ---- round 6: confirmation, near shapes predicted caught or known forced-only
R6 = [
    case("d6-firstname-mail-only", "mail", "txt, first name alone as a mail local part", [FIRST], "an %s@example.org" % FIRST.lower()),
    case("d6-person-comma-title", "name", "txt, Last, Dr. First", [SURNAME, FIRST], "Ansprechpartner: %s, Dr. %s" % (SURNAME, FIRST)),
    case("d6-acr-lower-plural", "name", "txt, lower acronym plural", [ACRONYM], "die %ss haben" % acr),
    case("d6-acr-genitive-lower", "name", "txt, lower acronym 's", [ACRONYM], "%s's Angebot" % acr),
    case("d6-short-camel-lower-first", "name", "txt, shortformLogistik", [SHORT], "var %sLogistik = 1" % SHORT.lower()),
    case("d6-org-ae-glued-ag", "name", "txt, org oe spelling with AG glued", [ORG_SHORT_LONG], "Lieferant %sAG" % ORG_SHORT_LONG),
    case("d6-org-dropped-glued-ag", "name", "txt, org umlaut dropped with AG glued", [ORG_DROPPED], "Lieferant %sAG" % ORG_DROPPED),
    case("d6-place-adj-lower-umlaut", "name", "txt, place adjective lower case with umlaut", [PLACE], "im %s gewerbegebiet" % PLACE_ADJ.lower()),
    case("d6-acr-dot-space-lower", "name", "txt, the acronym dotted and spaced in lower case", [". ".join(acr) + ". Angebot"], "%s. Angebot liegt vor" % ". ".join(acr)),
    case("d6-file-sentence-end", "name", "txt, file number before a full stop", [FILE_NUMBER], "siehe Az. %s." % FILE_NUMBER),
    case("d6-typo-mail-domain", "mail", "txt, info@ at the domain with the swapped short form", [DOMAIN_TYPO], "an info@%s" % DOMAIN_TYPO),
    case("d6-acr-windows-profile", "name", "txt, Windows profile path acronym-admin", [ACRONYM], "C:\\Users\\%s-admin\\Desktop" % acr),
    case("d6-short-legal-glued", "name", "txt, short form with GmbH glued", [SHORT], "die %sGmbH" % SHORT),
    case("d6-acr-upper-in-caps-sentence", "name", "txt, acronym inside an all-capitals line", [ACRONYM], "ANGEBOT %s 2026 FINAL" % ACRONYM),
]
CASES += R5 + R6


# What tests/test_redteam_pack.py accepts besides the fixture forms. DERIVED: strings built from a fixture form
# (a typo, an encoding, a transliteration). INVENTED: structured values this module builds, invented but valid
# in shape (documentation ranges, fake digit families). A value may carry one of them whole or in part.
DERIVED = (SHORT_TYPO, SHORT_SWAP, SHORT_DOUBLED_MID, SHORT_DOUBLED_LAST, SHORT_MISSING, SURNAME_MISSING,
           ORG_SHORT_MISSING, LAW_SHORT_SWAP, ACRONYM_SWAP, PLACE_ADJ, PLACE_ADJ_AE, ORG_LONG, ORG_SHORT_LONG, ORG_DROPPED,
           DOMAIN_DE, DOMAIN_XYZ, DOMAIN_NOHYPHEN, FILE_SLASH, FILE_NOSEP, TENDER_SPACE, TENDER_NOSEP, TENDER_PARTIAL,
           TENDER_UNDERSCORE, TENDER_LOWER_DOT, SLUG, CAMEL, LOGIN_SURNAME_FIRST, LOGIN_INITIAL_GLUED,
           LOGIN_INITIAL_HYPHEN, LOGIN_SURNAME_INITIAL, LOGIN_FIRST_INITIAL, FIRST_INITIAL, ACR_DOTTED, ACR_DOT_SPACE,
           ACR_DOT_2SPACE, ACR_SPACED, ACR_DASHED, PLACE_AE, LOGISTIK_TYPO, FULL_SECOND_TYPO, DOMAIN_TYPO, FILE_NOZERO,
           TENDER_PREFIX_NUMBER, TENDER_REVERSED, TENDER_NOZERO, ACR_3SPACE, ACR_UNDERSCORE, ACR_DOTTED_NOFINAL,
           TENDER_EN_DASH, SURNAME_PLURAL_UMLAUT, SHORT[1] + SHORT[0] + SHORT[2:],
           TENDER_PARTS[0] + "-" + TENDER_PARTS[2] + "-" + TENDER_PARTS[1])
INVENTED = ()
