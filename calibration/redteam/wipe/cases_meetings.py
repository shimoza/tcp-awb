"""Red team of wipe mode, case module meetings (B-051): 40 invented meeting transcripts in English and German, the
material the rules miss most and the local model pass of `awb clean --llm` is for. Speaker labels, first names
only, a company named once and then by its short name, nicknames, spelled-out mail addresses, names in lower case
and names inside technical talk ("Jonas from the network team said"); the keep terms are the technical words around
them.

Not one of the nine dimensions of harness.DIMENSIONS (their count is fixed by tests/test_redteam_pack.py): `awb clean
measure` reads every cases_*.py of this folder, this one too. Every name comes from tests/fixtures.py (MEETING_*),
written here by reference; hosts end in .example. Nothing here is real.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tests import fixtures as fx  # noqa: E402

FIRSTS, LASTS = fx.MEETING_FIRSTS, fx.MEETING_LASTS
N = {}
for i, key in enumerate("JAMLSHKFVR"):
    N[key] = FIRSTS[i]                  # J Jonas, A Anna, M Markus ... the first names of the fixtures
    N[key + "L"] = LASTS[i]             # JL the surname of the same person
    N[key.lower()] = FIRSTS[i].lower()  # j the first name in lower case
    N[key.lower() + "l"] = LASTS[i].lower()
N["MN"], N["VN"] = fx.MEETING_NICKNAMES            # Markus, Svenja
for key, brand, company in zip(("OX", "PZ", "TD", "GB"), fx.MEETING_BRANDS, fx.MEETING_COMPANIES):
    N[key], N[key + "C"], N[key.lower()] = brand, company, brand.lower()
N["P1"], N["P2"] = fx.MEETING_PLACES
N["D1"], N["D2"] = fx.MEETING_DOMAINS

MARK = "stage two of the invented plan"


def txt(name, text):
    """A text file with `text` (the placeholders of N filled); the marker line proves the carrier was read."""
    def build(inbox):
        p = inbox / name
        p.write_text(text.format(**N) + "\n" + MARK + "\n", encoding="utf-8")
        return p
    return build


def case(cid, carrier, name, text, values, keep=()):
    return {"id": cid, "carrier": carrier, "values": [v.format(**N) for v in values], "keep": list(keep),
            "build": txt(name, text)}


CASES = [
    # ------------------------------------------------------------------ English
    case("en-speaker-first-names", "speaker labels with first names only", "standup.txt",
         "{J}: the VPN tunnel to the second site is up again.\n{A}: good, then we test the failover on Friday.\n"
         "{J}: I will update the runbook.",
         ["{J}", "{A}"], ["VPN tunnel", "failover", "runbook"]),
    case("en-timestamp-full-names", "[hh:mm] First Last: lines", "call.txt",
         "[10:02] {J} {JL}: can everyone hear me?\n[10:03] {K} {KL}: yes. We start with the Kubernetes upgrade.\n"
         "[10:05] {J} {JL}: CCE 1.29 is planned for the weekend.",
         ["{J}", "{JL}", "{K}", "{KL}"], ["Kubernetes", "CCE 1.29"]),
    case("en-from-the-network-team", "a first name inside technical talk", "notes.txt",
         "Status of the migration\n\n{J} from the network team said the MTU on the Direct Connect link is 1500, "
         "so the jumbo frames stay off.",
         ["{J}"], ["network team", "MTU", "Direct Connect", "jumbo frames"]),
    case("en-company-named-once", "a company named once with its legal form, then by its short name", "kickoff.txt",
         "Thanks to everyone from {OXC} for joining the kickoff.\nThe firewall cluster is delivered by {OX}; "
         "{OX} also runs the second-level support.",
         ["{OX}"], ["firewall cluster", "second-level support"]),
    case("en-nickname", "a nickname with no surname anywhere", "chat.txt",
         "Who checks the backup job tonight?\n{VN} will do it, the restore test is on Monday.",
         ["{VN}"], ["backup job", "restore test"]),
    case("en-spelled-mail", "a mail address spelled out in words", "call-notes.txt",
         "{L}: send the HLD to me, my mail is {l} dot {ll} at {ox} dot example.\nWe review the HLD next week.",
         ["{L}", "{LL}", "{OX}"], ["HLD"]),
    case("en-lowercase-name", "a first name in lower case in a chat line", "chat-export.txt",
         "ok, ask {m} about the vpn, he set up the IPsec profile last year",
         ["{m}"], ["IPsec profile"]),
    case("en-name-who-built", "a first name as the subject of a relative clause in technical talk", "review.txt",
         "The Kafka cluster that {H} built runs on ECS with three brokers and Terraform 1.9.",
         ["{H}"], ["Kafka", "ECS", "Terraform 1.9"]),
    case("en-webvtt-voice", "WebVTT voice tags", "meeting.vtt",
         "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n<v {K} {KL}>We need two availability zones.</v>\n\n"
         "00:00:05.000 --> 00:00:09.000\n<v {F} {FL}>Then the database runs as RDS with a standby.</v>",
         ["{K}", "{KL}", "{F}", "{FL}"], ["availability zones", "RDS"]),
    case("en-possessive", "a first name with a possessive", "retro.txt",
         "Retro\n\n{F}'s script restarts the pods after the patch window; nobody else knows it.",
         ["{F}"], ["patch window", "pods"]),
    case("en-place-site", "a place named as a site", "sites.txt",
         "The data centre in {P1} has two rooms and one diesel generator; the second site is cold.",
         ["{P1}"], ["data centre", "diesel generator"]),
    case("en-action-items", "action items with first names", "minutes.md",
         "## Action items\n\n- {R}: open the ticket for the DNS zone\n- {S} to send the sizing sheet\n"
         "- {H} and {A}: test the load balancer",
         ["{R}", "{S}", "{H}", "{A}"], ["DNS zone", "sizing sheet", "load balancer"]),
    case("en-intro-round", "an introduction with a first name and a company short name", "intro.txt",
         "Hi, I'm {L}, I lead infrastructure at {PZ}. We moved the SAP landscape last year.",
         ["{L}", "{PZ}"], ["SAP landscape"]),
    case("en-handle-mention", "an @first.last handle in a chat", "chat.txt",
         "@{h}.{hl} please check the security group rules before the go-live",
         ["{h}", "{hl}"], ["security group", "go-live"]),
    case("en-mr-surname", "a surname after Mr and Ms", "minutes.txt",
         "Mr {SL} asked for the sizing; Ms {AL} will check the licences.",
         ["{SL}", "{AL}"], ["sizing", "licences"]),
    case("en-as-x-said", "a first name in reported speech", "followup.txt",
         "As {K} said yesterday, the object storage bucket keeps versioning on.",
         ["{K}"], ["object storage", "versioning"]),
    case("en-name-and-team", "first names with a team in brackets", "attendees.txt",
         "Attendees: {J} (network), {A} (security), {R} (storage) and two people of the operations team.",
         ["{J}", "{A}", "{R}"], ["operations team"]),
    case("en-zoom-export", "a Zoom chat export with timestamps", "zoom-chat.txt",
         "00:12:31 From {M} {ML} to Everyone: the link to the diagram is in the wiki\n"
         "00:12:58 From {V} {VL} to Everyone: thanks!",
         ["{M}", "{ML}", "{V}", "{VL}"], ["diagram", "wiki"]),
    case("en-teams-inverted", "a Teams transcript with Last, First labels", "teams.txt",
         "{LL}, {L}   0:45\nWe start with the network.\n\n{HL}, {H}   1:10\nThe NAT gateway is sized for 1 Gbit/s.",
         ["{L}", "{LL}", "{H}", "{HL}"], ["NAT gateway"]),
    case("en-company-lowercase-chat", "a company short name in lower case", "chat.txt",
         "fyi {td} sends the new certificates on thursday, the old ones expire on the 30th",
         ["{td}"], ["certificates"]),
    # ------------------------------------------------------------------ German
    case("de-sprecher-vornamen", "Sprecher mit Vornamen", "standup-de.txt",
         "{J}: Der VPN-Tunnel zum zweiten Standort steht wieder.\n{A}: Gut, dann testen wir am Freitag den "
         "Failover.\n{J}: Ich passe das Betriebshandbuch an.",
         ["{J}", "{A}"], ["VPN-Tunnel", "Failover", "Betriebshandbuch"]),
    case("de-vom-netzwerkteam", "ein Vorname in technischer Rede", "notizen.txt",
         "{J} vom Netzwerkteam sagt, die Firewall lässt Port 443 nur aus dem Management-Netz zu.",
         ["{J}"], ["Netzwerkteam", "Firewall", "Port 443"]),
    case("de-firma-einmal", "eine Firma einmal mit Rechtsform, dann mit Kurznamen", "kickoff-de.txt",
         "Vielen Dank an die Kollegen der {GBC} für die Vorbereitung.\n{GB} liefert die Switches, {GB} betreibt "
         "auch das Monitoring.",
         ["{GB}"], ["Switches", "Monitoring"]),
    case("de-spitzname", "ein Spitzname ohne Nachnamen", "chat-de.txt",
         "Wer schaut heute Nacht auf den Backup-Job?\n{MN} macht das, der Restore-Test ist am Montag.",
         ["{MN}"], ["Backup-Job", "Restore-Test"]),
    case("de-mail-buchstabiert", "eine Mailadresse in Worten", "telefonat.txt",
         "{K}: Schick das Grobkonzept bitte an {k} punkt {kl} at {pz} punkt example.",
         ["{K}", "{KL}", "{PZ}"], ["Grobkonzept"]),
    case("de-kleingeschrieben", "ein Vorname klein geschrieben", "chat-de.txt",
         "frag mal {m} wegen dem vpn, der hat das IPsec-Profil eingerichtet",
         ["{m}"], ["IPsec-Profil"]),
    case("de-herr-frau", "Nachnamen nach Herr und Frau", "protokoll.txt",
         "Herr {SL} hat das Angebot geprüft, Frau {AL} klärt die Lizenzen mit dem Einkauf.",
         ["{SL}", "{AL}"], ["Lizenzen", "Einkauf"]),
    case("de-genitiv", "ein Vorname im Genitiv", "retro-de.txt",
         "{H}s Skript startet die Pods nach dem Patchfenster neu, sonst kennt es niemand.",
         ["{H}"], ["Skript", "Patchfenster"]),
    case("de-ort", "ein Ort als Standort", "standorte.txt",
         "Das Rechenzentrum in {P2} hat zwei Räume und ein Notstromaggregat.",
         ["{P2}"], ["Rechenzentrum", "Notstromaggregat"]),
    case("de-aufgaben", "Aufgaben mit Vornamen", "protokoll.md",
         "## Aufgaben\n\n- {R}: Ticket für die DNS-Zone aufmachen\n- {S} schickt das Sizing\n"
         "- {H} und {A} testen den Load Balancer",
         ["{R}", "{S}", "{H}", "{A}"], ["DNS-Zone", "Sizing", "Load Balancer"]),
    case("de-vorstellung", "eine Vorstellung mit Vornamen und Firmenkurznamen", "vorstellung.txt",
         "Ich bin {L} und leite die IT bei {TD}. Wir haben letztes Jahr die SAP-Landschaft umgezogen.",
         ["{L}", "{TD}"], ["SAP-Landschaft"]),
    case("de-anrede-du", "ein Vorname in der Anrede", "chat-de.txt",
         "Danke dir, {F}, das passt so. Dann gehen wir mit zwei Availability Zones live.",
         ["{F}"], ["Availability Zones"]),
    case("de-teams-export", "ein Teams-Protokoll mit Nachname, Vorname", "teams-de.txt",
         "{JL}, {J}   0:45\nWir fangen mit dem Netz an.\n\n{VL}, {V}   1:10\nDas NAT-Gateway reicht für 1 Gbit/s.",
         ["{J}", "{JL}", "{V}", "{VL}"], ["NAT-Gateway"]),
    case("de-wie-gesagt", "ein Vorname in berichteter Rede", "nachgang.txt",
         "Wie {K} gestern gesagt hat, bleibt die Versionierung im Bucket an.",
         ["{K}"], ["Versionierung", "Bucket"]),
    case("de-name-in-technik", "ein Vorname im Relativsatz", "review-de.txt",
         "Der Kafka-Cluster, den {H} gebaut hat, läuft auf ECS mit drei Brokern.",
         ["{H}"], ["Kafka-Cluster", "ECS", "Brokern"]),
    case("de-zeitstempel", "[hh:mm] Vorname Nachname: Zeilen", "call-de.txt",
         "[09:58] {A} {AL}: Guten Morgen zusammen.\n[09:59] {R} {RL}: Morgen! Heute geht es um das Monitoring.",
         ["{A}", "{AL}", "{R}", "{RL}"], ["Monitoring"]),
    case("de-handle", "ein @vorname.nachname im Chat", "chat-de.txt",
         "@{j}.{jl} bitte die Security Groups vor dem Go-live prüfen",
         ["{j}", "{jl}"], ["Security Groups", "Go-live"]),
    case("de-name-und-rolle", "Vornamen mit Rolle in Klammern", "teilnehmer.txt",
         "Teilnehmer: {M} (Netzwerk), {V} (Security) und zwei Kollegen aus dem Betrieb.",
         ["{M}", "{V}"], ["Netzwerk", "Security"]),
    case("de-firma-klein", "ein Firmenkurzname klein im Chat", "chat-de.txt",
         "zur info: {ox} schickt die neuen zertifikate am donnerstag, die alten laufen am 30. ab",
         ["{ox}"], ["zertifikate"]),
    case("de-gemischt", "Deutsch und Englisch gemischt mit Vornamen", "mixed.txt",
         "{S}: let's park that. {F}, kannst du bis Freitag das Terraform-Modul für das VPC fertig machen?\n"
         "{F}: yes, mache ich.",
         ["{S}", "{F}"], ["Terraform-Modul", "VPC"]),
]
