"""Red team of wipe mode, dimension russian: Russian prose, mail, chats and documents.

Cyrillic surnames with case endings, patronymics, initials before and after the surname, the Russian titles
(г-н, г-жа, господин, уважаемый) in prose and in their declined forms, Russian company forms (ООО «X», АО «X»,
ИП X, компания X, the Latin transliteration OOO X), the registered customer transliterated into Cyrillic (a leak
by design, measured), Latin names and products inside Russian text, Russian mail headers (От:, От кого:, Кому:,
Тема:), Russian chats and transcripts, mixed-script names, first names alone and declined in Russian prose.
Losses: Russian technical phrases after the abbreviation г. (year), all-caps and title-cased Russian headings,
one-word Russian headings and subjects, repeated Russian field labels, department names under Ответственный,
Cyrillic vendor names, the sentence-start word swallowed and learned with a name.

Every name is from tests/fixtures.py or from INVENTED below. Hosts end in .example. Nothing here is real.

A note on the harness: a value whose skeleton is shorter than five characters is looked for as a whole word
only, and the whole-word view does not fold look-alike letters while the skeleton does (Cyrillic а, е, о, р, с,
у, х, к become Latin letters there). A four-letter Cyrillic brand with one of those letters (the first run of this
module used one) is therefore never reported as a leak, whatever the output holds. The brand of this module has
five letters so that the raw view reads it.
"""
import sys
from email import policy
from email.message import EmailMessage
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
    "Тобиас Байшпильман",        # the fixture person transliterated into Cyrillic (unregistered as such)
    "Байшпильман", "Байшпильмана", "Байшпильману", "Байшпильманом",   # the surname and its declined forms
    "Александр Иванович Байшпильман",   # the same surname with an unlisted first name and a patronymic
    "Тобиас Иванович",           # first name and patronymic alone
    "Анна Зрблова", "Зрблова", "Зрбловой",   # an invented feminine surname (the Cyrillic Zrbl) and its instrumental
    "Анна Квжрт", "Квжрт",       # an invented person, surname Квжрт
    "Тобиас Квжртский", "Квжртского",   # an invented adjectival surname and its genitive
    "Александр",                 # a common first name alone, not in the prototype's first-name list
    "Ольга", "Ольги", "Ольге",   # a common first name alone, in the list, and its declined forms
    "Компания Нргтц",            # the brand after the capitalised company word
    "Нргтц", "ООО «Нргтц»", "АО «Нргтц Консалтинг»", "Нргтц Консалтинг GmbH", "ГК «Нргтц»",   # the planted brand in
    "ООО «Квжрт»", "банк «Квжрт»",       #   Cyrillic (five letters, see the note on the harness below) and invented forms
    "Nrgtz",                     # the fixture brand, after the Latin transliteration OOO of a Russian legal form
    "Зиксво Логистик ГмбХ", "Зиксво",   # the registered customer transliterated into Cyrillic (a leak by design)
    "Мустерхаузен",              # the unregistered control place in Cyrillic
    "nrgtz.example", "x.pomblet@nrgtz.example", "a.kvzhrt@nrgtz.example",   # the mail host and two addresses
    "Tobias Байшпильман",        # a mixed-script name
)

MARK = "stage two of the invented plan"
DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

RU_FULL = "Тобиас Байшпильман"
RU_FIRST, RU_LAST = RU_FULL.split()
RU_GEN, RU_DAT, RU_INS = RU_LAST + "а", RU_LAST + "у", RU_LAST + "ом"
RU_FULL2 = "Александр Иванович Байшпильман"
ALEX = "Александр"
OLGA = "Ольга"
OLGA_GEN, OLGA_DAT = "Ольги", "Ольге"
F_FULL = "Анна Зрблова"
F_LAST = "Зрблова"
F_INS = "Зрбловой"
K_FULL = "Анна Квжрт"
K_LAST = "Квжрт"
ADJ_FULL = "Тобиас Квжртский"
ADJ_GEN = "Квжртского"
RU_BRAND = "Нргтц"
RU_BRAND2 = "Нргтц Консалтинг"
CUST_RU = "Зиксво Логистик ГмбХ"
CUST_RU_SHORT = "Зиксво"
RU_PLACE = "Мустерхаузен"
HOST = BRAND.lower() + ".example"
MAIL_X = "x.pomblet@" + HOST
MAIL_A = "a.kvzhrt@" + HOST
MIXED = "Tobias " + RU_LAST

ECS = "Elastic Cloud Server"
OBS = "Object Storage Service"
CBR = "Cloud Backup and Recovery"
ELB = "Elastic Load Balancer"


def txt(name, text):
    """A plain file with `text`; the marker line proves the carrier was read."""
    def build(inbox):
        p = inbox / name
        p.write_text(text + "\n" + MARK + "\n", encoding="utf-8")
        return p
    return build


def docx_paragraphs(name, lines):
    def build(inbox):
        import docx
        d = docx.Document()
        for line in lines:
            d.add_paragraph(line)
        d.add_paragraph(MARK)
        p = inbox / name
        d.save(str(p))
        return p
    return build


def xlsx_rows(name, rows, sheet="Лист1"):
    def build(inbox):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet
        for row in rows:
            ws.append(list(row))
        ws.append([MARK])
        p = inbox / name
        wb.save(str(p))
        return p
    return build


def pptx_slides(name, slides):
    """One slide per entry; every entry is a list of text boxes (the first is the title)."""
    def build(inbox):
        import pptx
        from pptx.util import Inches
        prs = pptx.Presentation()
        for texts in list(slides) + [[MARK]]:
            slide = prs.slides.add_slide(prs.slide_layouts[6])
            for i, t in enumerate(texts):
                box = slide.shapes.add_textbox(Inches(1), Inches(1 + i), Inches(8), Inches(0.8))
                box.text_frame.text = t
        p = inbox / name
        prs.save(str(p))
        return p
    return build


def pdf_lines(name, lines):
    """A one-page letter in a font that carries Cyrillic."""
    def build(inbox):
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen import canvas
        if "DejaVu" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("DejaVu", DEJAVU))
        p = inbox / name
        c = canvas.Canvas(str(p), pagesize=A4)
        c.setFont("DejaVu", 11)
        y = 780
        for line in lines:
            c.drawString(60, y, line)
            y -= 16
        c.drawString(60, 100, MARK)
        c.save()
        return p
    return build


def eml(name, headers, body):
    def build(inbox):
        msg = EmailMessage(policy=policy.default)
        for k, v in headers:
            msg[k] = v
        msg.set_content(body + "\n" + MARK + "\n")
        p = inbox / name
        p.write_bytes(msg.as_bytes())
        return p
    return build


def case(cid, carrier, values, keep, build, note="", code=None, expect=None):
    c = {"id": cid, "carrier": carrier, "values": list(values), "keep": list(keep), "build": build, "note": note}
    if code is not None:
        c["code"] = code
    if expect:
        c["expect"] = dict(expect)
    return c


CASES = [
    # ------------------------------------------------------------------ Russian titles in prose
    case("ru-title-gn-gzha-prose", "txt, the abbreviated titles г-н and г-жа before bare Cyrillic surnames mid-sentence",
         [RU_LAST, F_LAST], [ECS],
         txt("zapiska.txt", "Миграцию согласовал г-н %s, сроки держим. Документы передала г-жа %s, %s заказан.\n"
             % (RU_LAST, F_LAST, ECS)),
         expect={RU_LAST: "person", F_LAST: "person"},
         note="proposed has г-н as a title, but the cut after the title rule splits г-н at its hyphen and drops the lower-case piece"),
    case("ru-title-gospodin-lower", "txt, the full title господин in lower case before a bare surname mid-sentence",
         [RU_LAST], [ELB],
         txt("zapiska.txt", "Сроки подтвердил господин %s, %s в работе.\n" % (RU_LAST, ELB)),
         expect={RU_LAST: "person"},
         note="a lower-case title word is dropped by the unit guard of the title cut"),
    case("ru-title-declined", "txt, the declined titles г-ну and г-же before declined surnames (dative)",
         [RU_DAT, F_INS], ["NAT Gateway"],
         txt("pismo.txt", "Письмо направлено г-ну %s, копия г-же %s. NAT Gateway настроен.\n" % (RU_DAT, F_INS)),
         expect={RU_DAT: "person", F_INS: "person"},
         note="only the nominative forms г-н and г-жа are title words"),
    case("ru-salutation-gospodin", "txt, Уважаемый господин Surname, as the salutation line (control for the title cases)",
         [RU_LAST], [ECS],
         txt("pismo.txt", "Уважаемый господин %s,\n\n%s готов к приёмке.\n" % (RU_LAST, ECS)),
         expect={RU_LAST: "person"}),

    # ------------------------------------------------------------------ initials
    case("ru-initial-cyrillic-first", "txt, a Cyrillic initial before the surname (Т. Surname) in prose",
         [RU_LAST], ["Cloud Eye"],
         txt("protokol.txt", "Ответственный за площадку Т. %s подтвердил сроки, Cloud Eye настроен.\n" % RU_LAST),
         expect={RU_LAST: "person"},
         note="the initial rule of the intake takes Latin capitals only"),

    # ------------------------------------------------------------------ patronymics and name order
    case("ru-patronymic-unlisted-first", "txt, first name, patronymic and surname in prose, the first name not in the list; the surname declined later",
         [RU_FULL2, RU_GEN], ["Direct Connect"],
         txt("protokol.txt", "Сроки согласовал %s, Direct Connect заказан. Доступ у %s есть.\n" % (RU_FULL2, RU_GEN)),
         expect={RU_FULL2: "person"},
         note="three capitalised words are [name]; the patronymic suffix is not read as a person signal"),
    case("ru-patronymic-pair-alone", "txt, first name and patronymic alone in prose (the polite reference), the patronymic alone later",
         ["Тобиас Иванович", "Иванович"], [ECS],
         txt("protokol.txt", "Как сказал Тобиас Иванович, сроки держим. Иванович всегда прав, %s заказан.\n" % ECS),
         expect={"Тобиас Иванович": "person"}),

    # ------------------------------------------------------------------ first names alone and declined
    case("ru-first-name-declined-short", "txt, a five-letter first name as a speaker label, then declined inside the speech",
         [OLGA, OLGA_GEN, OLGA_DAT], [ECS],
         txt("chat.txt", "%s: %s готов.\n%s: Есть ли у %s доступ? Отдай ключ %s, пожалуйста.\n%s: Да.\n"
             % (OLGA, ECS, RU_FIRST, OLGA_GEN, OLGA_DAT, OLGA)),
         expect={OLGA: "person"},
         note="the learned Cyrillic stem drops the last vowel only from six letters on"),
    case("ru-first-name-unlisted-mid", "txt, a common first name that is not in the list, alone mid-sentence",
         [ALEX], [OBS],
         txt("zapiska.txt", "Доступ выдал %s, ключи лежат в %s.\n" % (ALEX, OBS)),
         expect={ALEX: "person"}),

    # ------------------------------------------------------------------ greetings and closings
    case("ru-greeting-dobrogo-vremeni", "txt, the greeting Доброго времени суток before a first name",
         [ALEX], [ECS],
         txt("pismo.txt", "Доброго времени суток, %s!\n\n%s готов.\n" % (ALEX, ECS)),
         expect={ALEX: "person"},
         note="the greeting list has добрый день and доброе утро but not this one"),
    case("ru-closing-horoshego-dnya", "txt, the closing Хорошего дня, with the signer on the next line",
         [ALEX], [ECS],
         txt("pismo.txt", "Добрый день!\n\n%s готов.\n\nХорошего дня,\n%s\n" % (ECS, ALEX)),
         expect={ALEX: "person"}),
    case("ru-closing-role-line", "txt, С уважением, the full name, then a Russian role line under it",
         [RU_FULL, RU_LAST], ["Руководитель отдела ИТ", ECS],
         txt("pismo.txt", "Добрый день!\n\n%s готов.\n\nС уважением,\n%s\nРуководитель отдела ИТ\n" % (ECS, RU_FULL)),
         expect={RU_FULL: "person"},
         note="the Russian role line is wiped as a second person"),
    case("ru-signature-no-closing-role", "txt, a bare surname on its own line over a Russian role line and the company, no closing",
         [RU_LAST, RU_BRAND], ["Руководитель проекта", ECS],
         txt("pismo.txt", "Добрый день!\n\n%s готов.\n\n%s\nРуководитель проекта\nООО «%s»\n" % (ECS, RU_LAST, RU_BRAND)),
         expect={RU_LAST: "person", RU_BRAND: "company"},
         note="the lone-line rule knows German and English role words only"),
    case("ru-privet-product-loss", "txt, Привет, followed by a sentence that starts with a vendor name",
         [], ["Zabbix", ECS],
         txt("chat.txt", "Привет, Zabbix настроен.\n\n%s тоже готов.\n" % ECS),
         note="the salutation rule takes the words after the greeting as the addressee"),

    # ------------------------------------------------------------------ company forms
    case("ru-ooo-guillemets-pptx", "pptx, ООО «X» and АО «X Y» in guillemets on a slide",
         [RU_BRAND, RU_BRAND2], ["Участники проекта", ECS],
         pptx_slides("slajdy.pptx", [["Участники проекта", "ООО «%s»" % RU_BRAND, "АО «%s»" % RU_BRAND2],
                                     [ECS, "8 vCPU, 32 GB"]]),
         expect={RU_BRAND: "company", RU_BRAND2: "company"}),
    case("ru-ooo-no-quotes-prose", "txt, ООО X without quotes followed by prose, and ООО Y without quotes at the sentence end",
         [RU_BRAND, K_LAST], [ECS],
         txt("zapiska.txt", "ООО %s использует %s. Диски поставляет ООО %s.\n" % (RU_BRAND, ECS, K_LAST)),
         expect={RU_BRAND: "company", K_LAST: "company"},
         note="the value runs to the next punctuation, so the prose makes it implausible and the whole candidate is dropped"),
    case("ru-company-guillemets-no-form", "txt, компания «X» in guillemets without a legal form",
         [RU_BRAND], [CBR],
         txt("zapiska.txt", "Договор с компанией «%s» подписан, %s включён.\n" % (RU_BRAND, CBR)),
         expect={RU_BRAND: "company"},
         note="the Firma rule wants a capital letter right after the company word; the guillemet rule wants a legal form"),
    case("ru-company-noun-cases", "txt, the company nouns in the accusative and the instrumental (в компанию X, с заказчиком Y)",
         [RU_BRAND, K_LAST], [ECS],
         txt("zapiska.txt", "Мы обратились в компанию %s; встреча с заказчиком %s прошла вчера, %s заказан.\n"
             % (RU_BRAND, K_LAST, ECS)),
         expect={RU_BRAND: "company", K_LAST: "company"},
         note="компани[яией] and заказчик[аеу]? miss the endings -ю and -ом"),
    case("ru-company-capital-start", "txt, Компания X at the start of a sentence",
         ["Компания " + RU_BRAND, RU_BRAND], [ECS],
         txt("zapiska.txt", "Компания %s поставляет оборудование для %s.\n" % (RU_BRAND, ECS)),
         expect={"Компания " + RU_BRAND: "company"},
         note="the Firma rule is case-sensitive, the run rule gives [name] and knows no Russian company word"),
    case("ru-ooo-latin-translit", "txt, the Latin transliteration OOO before a brand in English prose",
         [BRAND], [ECS],
         txt("contract.txt", "The contract is signed with OOO %s; the %s order follows.\n" % (BRAND, ECS)),
         expect={BRAND: "company"},
         note="OOO, AO, ZAO, PAO are no legal forms of either rule set"),
    case("ru-label-ispolnitel-docx", "docx, a contract head with Заказчик: ООО «X» and Исполнитель: Y",
         [K_LAST, RU_BRAND], ["Предмет", ECS],
         docx_paragraphs("dogovor.docx", ["ДОГОВОР № 12/2026", "Заказчик: ООО «%s»" % K_LAST, "Исполнитель: %s" % RU_BRAND,
                                          "Предмет: поставка %s" % ECS]),
         expect={K_LAST: "company", RU_BRAND: "company"},
         note="Исполнитель is no label of the lists"),
    case("ru-label-compound-director", "txt, Генеральный директор: Surname and Главный бухгалтер: Surname",
         [RU_LAST, F_LAST], [ECS],
         txt("rekvizity.txt", "Генеральный директор: %s\nГлавный бухгалтер: %s\n\n%s заказан.\n" % (RU_LAST, F_LAST, ECS)),
         expect={RU_LAST: "person", F_LAST: "person"},
         note="a two-word Russian label matches no label of the lists"),

    # ------------------------------------------------------------------ the registered customer in Cyrillic
    case("ru-customer-translit-full", "txt, the registered customer transliterated into Cyrillic in prose",
         [CUST_RU, CUST_RU_SHORT], [ECS],
         txt("zapiska.txt", "Наш заказчик, %s, переезжает в T Cloud Public, %s заказан.\n" % (CUST_RU, ECS)),
         code=True,
         note="a leak by design: the register carries the Latin forms only"),
    case("ru-customer-translit-short-alone", "txt, the short form of the customer in Cyrillic alone in prose",
         [CUST_RU_SHORT], [OBS],
         txt("zapiska.txt", "Коллеги из %s просят доступ к %s.\n" % (CUST_RU_SHORT, OBS)),
         code=True,
         note="a leak by design"),

    # ------------------------------------------------------------------ mail headers and the carrier
    case("ru-header-ot-kogo", "txt, the Yandex header От кого: with a bare surname, Кому: with a bare surname, Тема: with two words",
         [RU_LAST, K_LAST], [ECS, "Миграция данных"],
         txt("pismo.txt", "От кого: %s <%s>\nКому: %s <%s>\nТема: Миграция данных\n\nДобрый день!\n\n%s готов.\n"
             % (RU_LAST, MAIL_X, K_LAST, MAIL_A, ECS)),
         expect={RU_LAST: "person", K_LAST: "person"},
         note="the header list has От: but not От кого:"),
    case("ru-eml-cyrillic-headers", "eml, Cyrillic display names in From and To (RFC 2047), a Cyrillic subject and body with a sign-off",
         [RU_FULL, RU_LAST, K_FULL, K_LAST], [ECS],
         eml("mail.eml", [("From", "%s <%s>" % (RU_FULL, MAIL_X)), ("To", "%s <%s>" % (K_FULL, MAIL_A)),
                          ("Subject", "Миграция в T Cloud Public")],
             "Анна, добрый день!\n\n%s готов.\n\nС уважением,\n%s\n" % (ECS, RU_FULL)),
         expect={RU_FULL: "person", K_FULL: "person"}),

    # ------------------------------------------------------------------ losses: Russian technical phrases and headings
    case("ru-loss-god-sentence-start", "txt, the year abbreviation г. followed by a sentence that starts with a technical phrase",
         [], ["Резервное копирование", CBR],
         txt("zapiska.txt", "Площадка готова с 2025 г. Резервное копирование настроено через %s.\n" % CBR),
         note="the Russian city rule reads г. as город and takes the next capitalised word as a place"),
    case("ru-loss-caps-headings-docx", "docx, the all-caps headings of a Russian specification and offer",
         [], ["ТЕХНИЧЕСКОЕ ЗАДАНИЕ", "КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ", ECS],
         docx_paragraphs("tz.docx", ["ТЕХНИЧЕСКОЕ ЗАДАНИЕ", "на поставку %s" % ECS, "КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ",
                                     "Цена указана без НДС."]),
         note="two capitalised words of which none is a known word"),
    case("ru-loss-one-word-heading-subject", "md, a one-word Russian subject line (Тема:) and a one-word Russian heading",
         [], ["Миграция", "Архитектура", ECS],
         txt("otchet.md", "Тема: Миграция\n\n# Архитектура\n\n%s с 8 vCPU.\n\nПоэтапно, по площадкам.\n" % ECS),
         note="a lone unknown word in a subject line or a heading is a name; the vocabulary has no Russian"),
    case("ru-loss-titlecase-headings-pptx", "pptx, title-cased Russian slide titles of two words",
         [], ["Архитектура Решения", "Резервное Копирование", ECS],
         pptx_slides("slajdy.pptx", [["Архитектура Решения", "%s с 8 vCPU" % ECS], ["Резервное Копирование", CBR]])),
    case("ru-loss-heading-swallow", "md, a heading of a Russian technical word and the brand",
         [RU_BRAND], ["Миграция", ECS],
         txt("otchet.md", "# Миграция %s в T Cloud Public\n\n%s заказан.\n" % (RU_BRAND, ECS)),
         expect={RU_BRAND: "company"},
         note="the run rule wipes the known heading word with the unknown one, and no Russian word is known"),
    case("ru-loss-sentence-start-learned", "txt, a sentence-start word before a surname, the same word opening the next sentence",
         [RU_LAST], ["Сегодня же начинаем", ECS],
         txt("zapiska.txt", "Сегодня %s подтвердил сроки. Сегодня же начинаем миграцию на %s.\n" % (RU_LAST, ECS)),
         expect={RU_LAST: "person"},
         note="the swallowed sentence-start word is learned with the run and wiped wherever it stands"),
    case("ru-loss-repeated-labels", "txt, the labels Описание: and Результат: repeated in a Russian test protocol",
         [], ["Описание", "Результат", ECS],
         txt("protokol.txt", "Описание: создать %s\nРезультат: OK\nОписание: подключить диск\nРезультат: OK\n"
             "Описание: проверить бэкап\nРезультат: FAIL\n" % ECS),
         note="a repeated label that is no neutral label of the lists is a speaker"),
    case("ru-loss-department-column-xlsx", "xlsx, a task sheet with the column Ответственный holding departments and one surname, on the default sheet Лист1",
         [RU_LAST], ["Отдел ИТ", "Служба безопасности", "Лист1"],
         xlsx_rows("zadachi.xlsx", [["Задача", "Ответственный", "Срок"], ["Бэкап", "Отдел ИТ", "10.10.2026"],
                                    ["Сеть", "Служба безопасности", "12.10.2026"], ["Диски", RU_LAST, "15.10.2026"]]),
         expect={RU_LAST: "person"},
         note="a department under a person header is a person when no word of it is known; the sheet name becomes a "
              "heading whose one word before the digit is unknown (seen in the output of run 1, measured from run 2)"),
    case("ru-loss-cyrillic-vendors", "txt, vendor and product names written in Cyrillic inside Russian prose",
         [], ["Яндекс Облако", "Астра Линукс", ECS],
         txt("zapiska.txt", "Исходная площадка: Яндекс Облако и Астра Линукс, целевая: %s.\n" % ECS),
         note="two capitalised words, none in the platform lists"),
    case("ru-keep-latin-suffixes", "txt, Latin product names with Russian case endings glued by hyphen and apostrophe (control)",
         [], ["Kubernetes-кластер", "Terraform'ом", "Zabbix-ом", ECS + "'е"],
         txt("chat.txt", "Собираем Kubernetes-кластер, деплоим Terraform'ом, мониторим Zabbix-ом, диски на %s'е.\n" % ECS)),

    # ------------------------------------------------------------------ places
    case("ru-place-date-line-pdf", "pdf, the Russian place and date line that opens a letter (Town, 5 октября 2026 г.)",
         [RU_PLACE], [ECS],
         pdf_lines("pismo.pdf", ["%s, 5 октября 2026 г." % RU_PLACE, "", "Уважаемые коллеги!", "", "%s заказан." % ECS]),
         expect={RU_PLACE: "place"},
         note="the place-and-date rule takes Latin capitals only"),

    # ------------------------------------------------------------------ declensions of surname shapes
    case("ru-adjectival-surname-declined", "txt, an adjectival surname (-ский) after a listed first name, then the genitive alone",
         [ADJ_FULL, ADJ_GEN], [ECS],
         txt("zapiska.txt", "%s прислал план. У %s есть доступ к %s.\n" % (ADJ_FULL, ADJ_GEN, ECS)),
         expect={ADJ_FULL: "person"},
         note="the learned stem keeps the -и of -ский, so -ского and -скому are not reached"),

    # ------------------------------------------------------------------ mixed script
    case("ru-mixed-latin-first-name", "txt, a Latin first name before a Cyrillic surname mid-sentence (control)",
         [MIXED, RU_LAST], [ECS],
         txt("zapiska.txt", "План прислал коллега %s, %s заказан.\n" % (MIXED, ECS)),
         expect={MIXED: "person"}),

    # ------------------------------------------------------------------ run 2: variations of the mechanisms found
    case("ru-initial-cyrillic-two", "txt, two Cyrillic initials without a space before the surname (Т.И. Surname) in prose",
         [RU_LAST], ["Cloud Eye"],
         txt("protokol.txt", "Отчёт подготовил Т.И. %s, Cloud Eye настроен.\n" % RU_LAST),
         expect={RU_LAST: "person"},
         note="varies ru-initial-cyrillic-first: the initial rule takes Latin capitals only"),
    case("ru-title-declined-gospodinom", "txt, the full title in the instrumental (с господином Surname-ом)",
         [RU_INS], ["NAT Gateway"],
         txt("zapiska.txt", "Встреча с господином %s прошла вчера, NAT Gateway настроен.\n" % RU_INS),
         expect={RU_INS: "person"},
         note="varies ru-title-declined: the full title word declines too"),
    case("ru-company-guillemets-gk-bank", "txt, ГК «X» (group of companies) and банком «Y» in guillemets without a legal form",
         [RU_BRAND, K_LAST], [ECS],
         txt("zapiska.txt", "Договор с ГК «%s» и банком «%s» подписан, %s заказан.\n" % (RU_BRAND, K_LAST, ECS)),
         expect={RU_BRAND: "company", K_LAST: "company"},
         note="varies ru-company-guillemets-no-form: a two-letter group abbreviation and another company noun"),
    case("ru-loss-pr-sentence-start", "txt, the abbreviation и пр. at a sentence end, the next sentence starts with a technical phrase",
         [], ["Миграция данных", CBR],
         txt("zapiska.txt", "Нужны диски, сеть и пр. Миграция данных выполняется ночью через %s.\n" % CBR),
         note="varies ru-loss-god-sentence-start: the Russian street rule reads пр. as проспект and takes the words after it"),
    case("ru-cyrillic-brand-latin-form", "txt, a two-word Cyrillic company name before the Latin legal form GmbH",
         [RU_BRAND2 + " GmbH", RU_BRAND], [ECS],
         txt("zapiska.txt", "Договор с %s GmbH подписан, %s заказан.\n" % (RU_BRAND2, ECS)),
         note="the company rule wants Latin capitals before a legal form; the lower-case brand rule takes the last Cyrillic word only"),
    case("ru-first-name-listed-mid", "txt, a first name of the list alone mid-sentence after a verb",
         [RU_FIRST], [OBS],
         txt("zapiska.txt", "Доступ выдал %s, ключи лежат в %s.\n" % (RU_FIRST, OBS)),
         expect={RU_FIRST: "person"},
         note="varies ru-first-name-unlisted-mid: the lone first name rule wants a line start or punctuation before the word"),
]
