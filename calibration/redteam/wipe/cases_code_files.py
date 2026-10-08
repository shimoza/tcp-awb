"""Red team of wipe mode, dimension code_files: code and configuration files.

Terraform (resource labels, tags, names, comments, heredocs), Python (dunder author, constants, docstrings,
identifiers, SQL and error strings), YAML, JSON, CSV and INI inventories, shell scripts, Dockerfiles, ssh and
nginx configuration, syslog and git logs, LDIF, CODEOWNERS and README files. Names glued into identifiers and
hosts, lower-case logins, names only in a comment, the registered acronym and short form with affixes. Losses:
resource names, environment words, SQL keywords, HTTP status texts, log levels, product names in comments,
attribute accesses that look like hosts.

Every name is from tests/fixtures.py or from INVENTED below. Hosts end in .example. Nothing here is real.
"""
import sys
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

# lower-case and glued spellings of the fixture forms (string operations on the fixtures only)
first_l = FIRST.lower()                      # xqarv
last_l = LAST.lower()                        # pomblet
brand_l = BRAND.lower()                      # nrgtz
acr_l = CUSTOMER_ACRONYM.lower()
short_l = CUSTOMER_SHORT.lower()
LOGIN_T = "t" + last_l                       # tpomblet, a login made of initial and surname
LOGIN_X = first_l[0] + last_l                # xpomblet
LOGIN_CAPS = LOGIN_T.upper()                 # TPOMBLET
HOST_BARE = "srv-%s-02" % last_l             # srv-pomblet-02, a host without a domain
DOMAIN = brand_l + ".example"                # nrgtz.example
HOST_FQDN = HOST_BARE + "." + DOMAIN         # srv-pomblet-02.nrgtz.example
MAIL1 = "%s.%s@%s" % (first_l[0], last_l, DOMAIN)   # x.pomblet@nrgtz.example
CAMEL = FIRST + LAST                         # XqarvPomblet
DOTTED = first_l + "." + last_l              # xqarv.pomblet
RES_LABEL = last_l + "_app01"                # pomblet_app01
FUNC = last_l + "_export"                    # pomblet_export
UPSTREAM = last_l + "_backend"               # pomblet_backend
HOST_WEB = last_l + "-web01"                 # pomblet-web01
HOST_DB = last_l + "-db01"                   # pomblet-db01
SUBHOST = last_l + "." + DOMAIN              # pomblet.nrgtz.example
ACR_PROD = acr_l + "-prod-01"
ACR_VM = acr_l + "vm01"
ACR_APP = acr_l + "-app01"
SHORT_BUCKET = short_l + "-backup-2026"
SHORT_LABEL = short_l + "_backup"

# every invented name of this module, so that the owner can see nothing real slipped in
INVENTED = (
    "Anna Zrbl",                 # an invented person, four-letter surname (login azrbl, host zrbl-db01)
    "Timo Zrbko",                # an invented person, five-letter surname (logins tzrbko, zrbko01)
    "Max Qwertzung",             # an invented person, surname with a German noun suffix, first name of the corpus
    "Jan Qwertzung",             # the same surname after a first name that is also a month abbreviation
    "Max_Qwertzung_export", "Jan_Qwertzung_report",
    "tpomblet", "xpomblet", "TPOMBLET", "azrbl", "AZRBL", "tzrbko", "zrbko01", "zrbl-db01",
    "srv-pomblet-02", "srv-pomblet-02.nrgtz.example", "nrgtz.example", "pomblet.nrgtz.example",
    "x.pomblet@nrgtz.example", "XqarvPomblet", "xqarv.pomblet", "pomblet_app01", "pomblet_export",
    "pomblet_backend", "pomblet-web01", "pomblet-db01", ACR_PROD, ACR_VM, ACR_APP,
    SHORT_BUCKET, SHORT_LABEL, "git.example", "prism.example",
    "203.0.113.5", "203.0.113.6", "203.0.113.9", "203.0.113.10", "203.0.113.11", "203.0.113.20",
)

P2_FIRST, P2_LAST = "Anna", "Zrbl"
P2 = "%s %s" % (P2_FIRST, P2_LAST)
P3_FIRST, P3_LAST = "Timo", "Zrbko"
P3 = "%s %s" % (P3_FIRST, P3_LAST)
P4_FIRST, P4_LAST = "Max", "Qwertzung"
P4 = "%s %s" % (P4_FIRST, P4_LAST)
IP1, IP2, IP3, IP4, IP5, IP6 = "203.0.113.5", "203.0.113.6", "203.0.113.9", "203.0.113.10", "203.0.113.11", "203.0.113.20"

MARK = "stage two of the invented plan"


def txt(name, text, mark=True):
    """A plain file with `text`; the marker line proves the carrier was read (left out for csv and json)."""
    def build(inbox):
        p = inbox / name
        p.write_text(text + ("\n" + MARK + "\n" if mark else ""), encoding="utf-8")
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
    # ------------------------------------------------------------------ Terraform
    case("tf-resource-label-lower-surname", "tf, the surname as the lower-case resource label, nowhere else",
         [RES_LABEL, LAST], ["opentelekomcloud_compute_instance_v2", "s3.large.2", "eu-de-01"],
         txt("main.tf",
             'resource "opentelekomcloud_compute_instance_v2" "%s" {\n  name              = "app01"\n'
             '  flavor_id         = "s3.large.2"\n  availability_zone = "eu-de-01"\n}\n' % RES_LABEL)),
    case("tf-tag-owner-quoted-login", "tf, owner tag with a quoted lower-case login (initial plus surname)",
         [LOGIN_T, LAST], ["prod", "s3.large.2", "web01"],
         txt("main.tf",
             'resource "opentelekomcloud_compute_instance_v2" "web01" {\n  name      = "web01"\n'
             '  flavor_id = "s3.large.2"\n  tags = {\n    owner = "%s"\n    env   = "prod"\n  }\n}\n' % LOGIN_T),
         expect={LOGIN_T: "person"}),
    case("tf-name-hyphen-quoted-loss", "tf, quoted resource names after name = (hyphenated, with digits), no name planted",
         [], ["web-prod-01", "db01", "s3.large.2"],
         txt("main.tf",
             'resource "opentelekomcloud_compute_instance_v2" "web" {\n  name      = "web-prod-01"\n'
             '  flavor_id = "s3.large.2"\n}\n\nresource "opentelekomcloud_rds_instance_v3" "db" {\n  name = "db01"\n}\n')),
    case("tf-tag-owner-team-loss", "tf, owner, contact and lead tags holding team names, no person planted",
         [], ["netops", "cloud-ops", "dba-team", "prod"],
         txt("tags.tf",
             'locals {\n  tags = {\n    owner   = "netops"\n    contact = "cloud-ops"\n    lead    = "dba-team"\n'
             '    env     = "prod"\n  }\n}\n')),
    case("tf-acr-prod-affix-loss", "tf, the registered acronym glued with the prod affix in a quoted name",
         [CUSTOMER_ACRONYM], ["prod"],
         txt("sg.tf",
             'resource "opentelekomcloud_networking_secgroup_v2" "sg" {\n  name        = "%s"\n'
             '  description = "security group"\n}\n' % ACR_PROD),
         code=True, note="the acronym must become the code and the environment word must stay"),
    case("tf-acr-vm-glued-hostname", "tf, the registered acronym glued to vm01 after hostname = (no affix)",
         [ACR_VM], ["locals", "fqdn"],
         txt("locals.tf", 'locals {\n  hostname = "%s"\n  fqdn     = "%s.%s"\n}\n' % (ACR_VM, ACR_VM, DOMAIN))),
    case("tf-short-form-snake-code", "tf, the registered short form in a snake_case label and a hyphenated bucket name",
         [CUSTOMER_SHORT], ["opentelekomcloud_obs_bucket", "backup", "private"],
         txt("obs.tf",
             'resource "opentelekomcloud_obs_bucket" "%s" {\n  bucket = "%s"\n  acl    = "private"\n}\n'
             % (SHORT_LABEL, SHORT_BUCKET)),
         code=True),
    case("tf-comment-lower-surname", "tf, the surname in lower case in a comment only",
         [LAST], ["module", "subnet"],
         txt("subnet.tf",
             '# %s owns this module, ask before changing the subnet\nmodule "subnet" {\n  source = "./modules/subnet"\n}\n'
             % last_l)),
    case("tf-comment-known-first-suffix-surname", "tf, a comment with a first name of the corpus and a surname ending in -ung",
         [P4, P4_LAST], ["variable", "cidr"],
         txt("vars.tf", '# maintained by %s since 2024\nvariable "cidr" {\n  default = "10.0.0.0/16"\n}\n' % P4)),
    case("tf-hostname-bare-vs-fqdn", "tf, the host with the surname as a fqdn in a DNS record and bare in an output",
         [HOST_BARE, LAST], ["records", "output", "hostname"],
         txt("dns.tf",
             'resource "opentelekomcloud_dns_recordset_v2" "a" {\n  name    = "%s."\n  records = ["%s"]\n}\n\n'
             'output "hostname" {\n  value = "%s"\n}\n' % (HOST_FQDN, IP1, HOST_BARE))),
    case("tf-heredoc-cloudinit-user", "tf, a cloud-init heredoc with name: login (unquoted) in the users list",
         [LOGIN_T, LAST], ["#cloud-config", "sudo", "/bin/bash"],
         txt("jump.tf",
             'resource "opentelekomcloud_compute_instance_v2" "jump" {\n  name      = "bastion01"\n'
             '  user_data = <<-EOF\n    #cloud-config\n    users:\n      - name: %s\n        groups: sudo\n'
             '        shell: /bin/bash\n  EOF\n}\n' % LOGIN_T)),
    case("tf-comment-product-no-vendor", "tf, a product name in a comment without its vendor in front (Prism Central)",
         [], ["Prism Central", "variable"],
         txt("prism.tf", '# Prism Central export of the VM inventory\nvariable "inventory_url" {\n  default = "https://prism.example"\n}\n')),

    # ------------------------------------------------------------------ Python
    case("py-dunder-author-class", "py, __author__ dunder with the full name",
         [PLANTED_PERSON, FIRST, LAST], ["__version__"],
         txt("export.py", '"""Export of the VM inventory."""\n__author__ = "%s"\n__version__ = "1.2"\n' % PLANTED_PERSON),
         expect={PLANTED_PERSON: "person"}),
    case("py-author-const-learned-glue", "py, AUTHOR constant, then the name glued into a function, a class, a module path and a caps login",
         [PLANTED_PERSON, FIRST, LAST, FUNC, CAMEL, DOTTED, LOGIN_CAPS], ["import", "export", "class", "Base"],
         txt("export.py",
             'AUTHOR = "%s"\nimport %s\n\n\ndef %s(rows):\n    return rows\n\n\nclass %s(Base):\n    USER = "%s"\n'
             % (PLANTED_PERSON, DOTTED, FUNC, CAMEL, LOGIN_CAPS)),
         expect={PLANTED_PERSON: "person"}),
    case("py-lower-comment-poisons-learned", "py, AUTHOR constant plus the surname in lower case in a comment, then glued forms",
         [LAST, FUNC, LOGIN_T], ["AUTHOR", "export"],
         txt("export.py",
             'AUTHOR = "%s"\n# ask %s before changing the export\n\n\ndef %s(rows):\n    return rows\n\n\nuser = "%s"\n'
             % (PLANTED_PERSON, last_l, FUNC, LOGIN_T))),
    case("py-module-path-brand-surname", "py, a module path made of the company brand and the surname in lower case",
         [BRAND, LAST], ["import", "export"],
         txt("main.py", "from %s.%s import export\n\n\ndef main():\n    export.start()\n" % (brand_l, last_l))),
    case("py-camel-class-only", "py, the name as a CamelCase class name, nowhere else",
         [CAMEL, LAST], ["Base", "Exporter"],
         txt("models.py", 'class %s(Base):\n    """Exporter."""\n' % CAMEL)),
    case("py-sql-name-vorname-inverted", "py, SQL with the columns Name, Vorname (a Last, First shape), no name planted",
         [], ["SELECT", "Name, Vorname", "FROM Mitarbeiter", "WHERE Abteilung"],
         txt("db.py", 'cur.execute("SELECT Name, Vorname FROM Mitarbeiter WHERE Abteilung = %s", (dept,))\n')),
    case("py-http-error-strings", "py, HTTP status texts in error strings, no name planted",
         [], ["Internal Server Error", "Not Found", "HTTPError"],
         txt("http.py",
             'if resp.status == 500:\n    raise HTTPError(500, "Internal Server Error")\n'
             'if resp.status == 404:\n    raise HTTPError(404, "Not Found")\n')),
    case("py-learned-product-wipes-identifiers", "py, a product name in Title Case in the docstring and the same word in identifiers",
         [], ["prism_central_export", "PRISM_URL"],
         txt("prism.py",
             '"""Prism Central export (Nutanix)."""\nPRISM_URL = "https://prism.example"\n\n\n'
             'def prism_central_export(rows):\n    return rows\n')),
    case("py-attr-access-read-as-host", "py, attribute accesses whose attribute is a top-level domain (os.name, user.email, app.run)",
         [], ["os.name", "user.email", "app.run"],
         txt("app.py", "print(os.name, user.email)\napp.run()\n")),

    # ------------------------------------------------------------------ YAML, JSON, CSV, INI
    case("yaml-owner-unquoted-login", "yaml, owner: login (unquoted lower case)",
         [LOGIN_T, LAST], ["replicaCount", "platform"],
         txt("values.yaml", "replicaCount: 2\nowner: %s\nteam: platform\n" % LOGIN_T)),
    case("yaml-contact-fullname-learns-host", "yaml, contact: full name, then the surname glued into a host name",
         [PLANTED_PERSON, FIRST, LAST, HOST_DB], ["host", "contact"],
         txt("values.yaml", "contact: %s\nhost: %s\n" % (PLANTED_PERSON, HOST_DB)),
         expect={PLANTED_PERSON: "person"}),
    case("ini-ansible-inventory-hosts", "ini, Ansible inventory with the surname in a host, the acronym glued with app01, a login in vars",
         [HOST_WEB, LOGIN_X, LAST], ["webservers", "ansible_host", "ansible_user"],
         txt("inventory.ini",
             "[webservers]\n%s ansible_host=%s\n%s ansible_host=%s\n\n[all:vars]\nansible_user=%s\n"
             % (HOST_WEB, IP3, ACR_APP, IP4, LOGIN_X)),
         code=True),
    case("json-camel-key-surname", "json, camelCase keys (ownerName, createdBy) with the surname and a login",
         [LAST, LOGIN_X], ["ownerName", "createdBy", "replicas"],
         txt("config.json", '{\n  "ownerName": "%s",\n  "createdBy": "%s",\n  "replicas": 2\n}\n' % (LAST, LOGIN_X), mark=False)),
    case("json-users-caps-list", "json, a list of logins in capitals",
         [LOGIN_CAPS, "AZRBL", LAST], ["users", "admins"],
         txt("users.json", '{\n  "users": ["%s", "AZRBL"],\n  "group": "admins"\n}\n' % LOGIN_CAPS, mark=False)),
    case("csv-inventory-owner-lower", "csv, inventory with an owner column holding a lower-case login and a host with the surname",
         [HOST_BARE, LOGIN_T, LAST], ["hostname", "netops"],
         txt("hosts.csv", "hostname,owner,ip\n%s,%s,%s\nweb01,netops,%s\n" % (HOST_BARE, LOGIN_T, IP1, IP2), mark=False)),

    # ------------------------------------------------------------------ shell, Dockerfile, configuration
    case("sh-ssh-user-at-bare-host", "sh, ssh login@host where the host has no domain",
         [LOGIN_T, HOST_BARE, LAST], ["systemctl restart nginx"],
         txt("deploy.sh", "#!/bin/sh\nssh %s@%s 'sudo systemctl restart nginx'\n" % (LOGIN_T, HOST_BARE))),
    case("sh-git-remote-handle", "sh, a git remote with the surname as the repository owner after the colon",
         [LAST], ["git clone", "infra.git"],
         txt("clone.sh", "git clone git@git.example:%s/infra.git\ncd infra\n" % last_l)),
    case("dockerfile-label-maintainer-class", "Dockerfile, LABEL maintainer with the full name and the mail address",
         [PLANTED_PERSON, FIRST, LAST, MAIL1], ["FROM ubuntu:22.04", "apt-get install"],
         txt("Dockerfile",
             'FROM ubuntu:22.04\nLABEL maintainer="%s <%s>"\nRUN apt-get update && apt-get install -y nginx\n'
             % (PLANTED_PERSON, MAIL1)),
         expect={PLANTED_PERSON: "person"}),
    case("dockerfile-env-owner-unquoted", "Dockerfile, ENV APP_OWNER=login without quotes",
         [LOGIN_T, LAST], ["APP_PORT", "8080"],
         txt("Dockerfile", "FROM ubuntu:22.04\nENV APP_OWNER=%s\nENV APP_PORT=8080\n" % LOGIN_T)),

    # ------------------------------------------------------------------ logs
    case("log-syslog-sshd-user-host", "log, syslog lines with the host and the login of the surname",
         [HOST_BARE, LOGIN_T, LAST], ["Accepted publickey", "sshd", "COMMAND=/usr/bin/systemctl"],
         txt("auth.log",
             "Oct  5 10:03:01 %s sshd[1234]: Accepted publickey for %s from %s port 51234 ssh2\n"
             "Oct  5 10:03:05 %s sudo: %s : TTY=pts/0 ; COMMAND=/usr/bin/systemctl restart nginx\n"
             % (HOST_BARE, LOGIN_T, IP2, HOST_BARE, LOGIN_T))),
    case("log-levels-status-texts", "log, log levels in capitals followed by status texts, no name planted",
         [], ["WARNING Connection refused", "ERROR Internal Server Error", "INFO Service Unavailable"],
         txt("app.log",
             "2026-10-05 10:03:02 WARNING Connection refused by backend\n"
             "2026-10-05 10:03:03 ERROR Internal Server Error from upstream\n"
             "2026-10-05 10:03:04 INFO Service Unavailable, retry in 30s\n")),
    case("log-gitlog-author-class", "log, git log with Author: full name <mail>",
         [PLANTED_PERSON, FIRST, LAST], ["commit", "Date:", "inventory export"],
         txt("git.log",
             "commit 3f2a9c8\nAuthor: %s <%s>\nDate:   Mon Oct 5 10:03:00 2026 +0200\n\n    add the inventory export\n"
             % (PLANTED_PERSON, MAIL1)),
         expect={PLANTED_PERSON: "person"}),

    # ------------------------------------------------------------------ directory, web server, repository files
    case("ldif-cn-dc-brand", "ldif, CN= with the full name, the company brand as a DC component, the login as sAMAccountName",
         [PLANTED_PERSON, FIRST, LAST, LOGIN_X, BRAND], ["OU=Users", "sAMAccountName", "objectClass"],
         txt("users.ldif",
             "dn: CN=%s,OU=Users,DC=%s,DC=example\nobjectClass: user\nsAMAccountName: %s\n" % (PLANTED_PERSON, brand_l, LOGIN_X)),
         expect={PLANTED_PERSON: "person"}),
    case("nginx-upstream-subdomain-label", "conf, nginx with the surname as a subdomain label and as an upstream name",
         [UPSTREAM, LAST], ["upstream", "server_name", "proxy_pass"],
         txt("nginx.conf",
             "upstream %s {\n  server %s:8080;\n}\nserver {\n  server_name %s;\n  location / {\n"
             "    proxy_pass http://%s;\n  }\n}\n" % (UPSTREAM, IP5, SUBHOST, UPSTREAM))),
    case("codeowners-handles", "CODEOWNERS, handles made of the surname and the company brand",
         [LAST, "azrbl", BRAND], ["/terraform/", "platform-team"],
         txt("CODEOWNERS", "# review rules\n/terraform/   @%s @azrbl\n*             @%s/platform-team\n" % (last_l, brand_l))),

    # ------------------------------------------------------------------ README
    case("md-readme-julia-line-start", "md, a programming language that is also a first name at the start of a line",
         [], ["Julia", "Pascal", "Requirements"],
         txt("README.md",
             "## Requirements\n\nJulia 1.10 or newer is required for the notebooks.\n"
             "Pascal sources live under legacy/ and are not built.\n")),
    case("md-readme-nimble-after-hpe", "md, a product name after a three-letter vendor acronym (HPE Nimble Storage)",
         [], ["HPE Nimble Storage", "Nimble Storage", "Cloud Backup and Recovery"],
         txt("README.md",
             "## Storage\n\nThe export reads the HPE Nimble Storage snapshots before the Cloud Backup and Recovery run.\n")),

    # ------------------------------------------------------------------ run 2: the mechanisms of run 1, varied
    case("json-name-field-loss", "json, quoted technical values after name and assignee keys, no name planted",
         [], ["payments-api", "unassigned", "replicas"],
         txt("job.json", '{\n  "name": "payments-api",\n  "assignee": "unassigned",\n  "replicas": 2\n}\n', mark=False),
         note="varies tf-name-hyphen-quoted-loss: the quoted weak label rule in JSON"),
    case("yaml-name-quoted-loss", "yaml, a quoted Kubernetes name and a quoted Ansible task title, no name planted",
         [], ["nginx-ingress", "Deploy payments-api", "Deployment"],
         txt("deploy.yaml",
             'apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: "nginx-ingress"\n---\n'
             '- name: "Deploy payments-api"\n  shell: make deploy\n'),
         note="varies tf-name-hyphen-quoted-loss: the quoted weak label rule in YAML"),
    case("yaml-ansible-task-with-surname", "yaml, an unquoted Ansible task title with a verb, the surname and a noun",
         [LAST, LOGIN_X], ["Create", "user", "/bin/bash"],
         txt("users.yaml", "- name: Create %s user\n  user:\n    name: %s\n    shell: /bin/bash\n" % (LAST, LOGIN_X)),
         note="the label rule takes the verb into the person span"),
    case("py-learned-short-surname-glue", "py, learned persons with a four-letter and a five-letter surname, then the surnames glued into logins and a host",
         [P2, P3, P2_LAST, P3_LAST, "azrbl", "tzrbko", "zrbko01", "zrbl-db01"], ["AUTHORS", "login1"],
         txt("owners.py",
             'AUTHORS = "%s, %s"\nlogin1 = "azrbl"\nlogin2 = "tzrbko"\nlogin3 = "zrbko01"\nhost = "zrbl-db01"\n' % (P2, P3)),
         expect={P2: "person", P3: "person"},
         note="varies py-author-const-learned-glue: the learned matcher for short surnames"),
    case("tf-ident-known-first-suffix-surname", "tf, identifiers made of a corpus first name, a surname ending in -ung and a noun",
         ["Max_Qwertzung_export", "Jan_Qwertzung_report", P4_LAST], ["export", "report"],
         txt("jobs.tf", 'locals {\n  job1 = "Max_Qwertzung_export"\n  job2 = "Jan_Qwertzung_report"\n}\n'),
         note="varies tf-comment-known-first-suffix-surname: the identifier run rule with the same exemption"),
    case("py-todo-paren-poisons-learned", "py, AUTHOR constant plus the surname in lower case inside TODO(...), then a glued function name",
         [LAST, FUNC], ["TODO", "migration"],
         txt("export.py",
             'AUTHOR = "%s"\n# TODO(%s): drop after the migration\n\n\ndef %s(rows):\n    return rows\n'
             % (PLANTED_PERSON, last_l, FUNC)),
         note="varies py-lower-comment-poisons-learned: a parenthesised lower-case occurrence"),
]
