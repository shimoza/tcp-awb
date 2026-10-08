# Expected leftovers

What every dimension still leaves over after the run of 2026-09-27 and the fixes that followed, one line per case
id with the reason in a few words. A run with `--expected calibration/redteam/EXPECTED.md` names what is new against
this list and what is clean now. Design limits are the ones `calibration/redteam-2026-09-27.md` keeps on purpose;
"not promised" is a kind no pattern claims. Ids and reasons only, never a value.

The clipped cases at the end of a section are not leftovers by design: their marker lost its first letter to the
replacement of the form before it (the matcher defect the curation found, see the record). They stay listed so that
a run does not report them as new; when the defect is fixed they show as clean and leave the list.

## structured

- ip4-numeric-forms: escape via raw. IPv4 in decimal, octal or hex form, no reader shape (design limit)
- mac-no-separators: escape via raw. twelve hex characters without a label read as an id (design limit)
- iban-typo: escape via raw. an IBAN with a wrong check digit, mod 97 fails (design limit)
- bic-bare-eight: escape via raw. a BIC of eight letters without a label or a digit (design limit)
- phone-letters: escape via raw. a vanity number with letters (design limit)
- tax-no-label-spaces: escape via raw. a tax number without a label (design limit)
- tax-steuer-id-no-label: escape via raw. eleven digits without a label (design limit)
- np-card: escape via raw. card number: a kind no pattern claims, listed for his decision
- np-svnr: escape via raw. social security number: a kind no pattern claims, listed for his decision
- np-id-card: escape via raw. id card and passport numbers: a kind no pattern claims, listed for his decision
- np-plate: escape via raw. license plate: a kind no pattern claims, listed for his decision
- np-konto-blz: escape via raw. account number and bank code: a kind no pattern claims, listed for his decision
- np-coordinates: escape via raw. coordinates: a kind no pattern claims, listed for his decision
- np-birthdate: escape via raw. date of birth: a kind no pattern claims, listed for his decision
- np-platform-ids: escape via raw. 32 hex id and UUID: a kind no pattern claims, listed for his decision
- np-access-keys: escape via raw. access key pair: a kind no pattern claims, listed for his decision
- np-tokens: escape via raw. session and bearer tokens: a kind no pattern claims, listed for his decision
- np-config-password: escape via raw. passwords in a config file: a kind no pattern claims, listed for his decision
- np-private-key: escape via raw. a private key block: a kind no pattern claims, listed for his decision
- r2-url-tlds-batch2: escape via raw. one bare host of the batch is under .ai, a file suffix domain (design limit)
- r4-phone-cc-no-plus: escape via raw. a country code without + or 00 (design limit)

## forms

- leet-short-0: escape via leet. leet spelling, 0 for o (design limit)
- leet-full: forced-only via leet-i. leet spelling of the full form, forced-only (the leet company form is a candidate)
- b32-full: escape via raw. base32 is not decoded (design limit)
- b64-gzip-full: escape via raw. base64 of gzip is not decoded (design limit)
- b64-short-8chars: escape via raw. a base64 block under 16 characters is not decoded (design limit)
- b64-lines-20-cut: escape via raw. base64 lines of 20 characters are decoded one by one, the id is in two pieces (MIME blocks start at 24 characters per line)
- uuencode-full: escape via raw. uuencode in a plain text file (design limit)
- qp-ascii-short: escape via raw. quoted printable of ASCII letters stays as written (design limit)
- encw-unknown-charset-short: escape via raw. an encoded word with an unknown charset and a short payload (design limit)
- utf7-plain-org: escape via raw. utf-7 without a charset label (design limit)
- utf7-plain-all-encoded: escape via raw. utf-7 without a charset label, every letter encoded (design limit)
- ent-named-nosemi-md: escape via skeleton-unescaped. a named entity without its semicolon (his decision of the normaliser test)
- css-content-html: unseen. CSS content strings go to detection only (by design of the html reader)
- html-attrs-unseen: unseen. alt, title, aria-label and data attributes go to detection only (by design)
- cyrillic-phonetic: forced-only via raw. a phonetic spelling in another alphabet, forced-only (the capitalised words are candidates)
- tender-upper-prefix-glued: escape via raw. the tender id glued after an upper-case run, no boundary (design limit: numbers glued to letters)
- r2-b64-utf32-nobom: escape via decoded. base64 of utf-32 without a BOM is not decoded (design limit)
- r2-b64-first-line-short: escape via raw. a first base64 line of 16 characters is not joined with the 76 character lines after it, the id is in two pieces
- r2-tender-digit-prefix-glued: escape via raw. the tender id glued after a digit, no boundary (design limit)
- r2-cyrillic-phonetic-lower: escape via raw. a phonetic spelling in another alphabet, lower case (design limit)
- r4-b64-utf32-be-nobom: escape via decoded. base64 of utf-32 big endian without a BOM is not decoded (design limit)
- r4-html-template-input-unseen: unseen. template content and input values go to detection only (by design)

## derived

- d-typo-one-letter: escape via raw. a typo of a registered form standing alone, the register decides what a form is (design limit)
- d-typo-swap: escape via raw. a typo of a registered form standing alone, the register decides what a form is (design limit)
- d-typo-doubled-mid: escape via raw. a typo of a registered form standing alone, the register decides what a form is (design limit)
- d-typo-missing: escape via word. a typo of a registered form standing alone, the register decides what a form is (design limit)
- d-typo-in-company: forced-only via raw. a typo inside a candidate shape: blocked without --force, readable with it (forced-only)
- d-typo-run: forced-only via raw. a typo inside a candidate shape: blocked without --force, readable with it (forced-only)
- d-person-typo-alone: escape via raw. a typo of a registered form standing alone, the register decides what a form is (design limit)
- d-person-typo-title: forced-only via raw. a typo inside a candidate shape: blocked without --force, readable with it (forced-only)
- d-org-typo-alone: escape via raw. a typo of a registered form standing alone, the register decides what a form is (design limit)
- d-org-typo-company: forced-only via raw. a typo inside a candidate shape: blocked without --force, readable with it (forced-only)
- d-law-typo-kanzlei: forced-only via raw. a typo inside a candidate shape: blocked without --force, readable with it (forced-only)
- d-law-typo-alone: escape via raw. a typo of a registered form standing alone, the register decides what a form is (design limit)
- d-acr-swap: escape via word. a typo of a registered form standing alone, the register decides what a form is (design limit)
- d-adj-place-ae: forced-only via raw. the place adjective with ae before a capitalised word: blocked without --force (forced-only)
- d-firstname-alone: escape via raw. a first name alone, the register holds the full form and the surname (design limit)
- d-firstname-initial: escape via raw. a first name alone, the register holds the full form and the surname (design limit)
- d-inverted: escape via raw. a first name alone, the register holds the full form and the surname (design limit)
- d-middle-initial: escape via raw, unseen (clipped). a first name alone, the register holds the full form and the surname (design limit)
- d-login-surname-first: escape via raw. a first name alone, the register holds the full form and the surname (design limit)
- d-login-first-initial: escape via raw. a first name alone, the register holds the full form and the surname (design limit)
- d-acr-glue-vm: escape via raw. the acronym glued to a word that is no affix of rules/glue-affixes.txt (design limit)
- d-acr-glue-fs-unc: escape via raw. the acronym glued to a word that is no affix of rules/glue-affixes.txt (design limit)
- d-acr-glue-node: escape via raw. the acronym glued to a word that is no affix of rules/glue-affixes.txt (design limit)
- d-acr-glue-client: escape via raw. the acronym glued to a word that is no affix of rules/glue-affixes.txt (design limit)
- d-acr-glue-share: escape via raw. the acronym glued to a word that is no affix of rules/glue-affixes.txt (design limit)
- d-acr-glue-upper-vm: escape via raw. the acronym glued to a word that is no affix of rules/glue-affixes.txt (design limit)
- d-file-nosep: unseen. the output is withheld by the second check: nothing leaks, the file stays in the inbox with a note
- d-tender-nosep: unseen. the output is withheld by the second check: nothing leaks, the file stays in the inbox with a note
- d-tender-partial: escape via raw. the year and number of the tender id without its prefix (design limit)
- d2-acr-glue-vm-nodigits: escape via raw. the acronym glued to a word that is no affix of rules/glue-affixes.txt (design limit)
- d2-acr-glue-both-sides: escape via raw. the acronym glued to a word that is no affix of rules/glue-affixes.txt (design limit)
- d2-acr-glue-platform: escape via raw. the acronym glued to a word that is no affix of rules/glue-affixes.txt (design limit)
- d2-acr-glue-roles: escape via raw. the acronym glued to a word that is no affix of rules/glue-affixes.txt (design limit)
- d2-acr-glue-caps: escape via raw. the acronym glued to a word that is no affix of rules/glue-affixes.txt (design limit)
- d2-acr-glue-1a: escape via raw. the acronym glued to a word that is no affix of rules/glue-affixes.txt (design limit)
- d2-acr-glue-it: escape via raw. the acronym glued to a word that is no affix of rules/glue-affixes.txt (design limit)
- d2-acr-glue-digits-between: escape via raw. the acronym glued to a word that is no affix of rules/glue-affixes.txt (design limit)
- d2-acr-glue-affix-then-rest: escape via raw. the acronym glued to a word that is no affix of rules/glue-affixes.txt (design limit)
- d2-typo-inverted: forced-only via raw. a typo inside a candidate shape: blocked without --force, readable with it (forced-only)
- d2-org-typo-ag: forced-only via raw. a typo inside a candidate shape: blocked without --force, readable with it (forced-only)
- d2-typo-lower-alone: escape via raw. a typo of a registered form standing alone, the register decides what a form is (design limit)
- d2-typo-transposed-first: escape via raw. a typo of a registered form standing alone, the register decides what a form is (design limit)
- d2-firstname-title: forced-only via raw. the first name after a title: blocked without --force (forced-only)
- d2-surname-first-nocomma: escape via raw. a first name alone, the register holds the full form and the surname (design limit)
- d2-firstname-md-table: escape via raw. a first name alone, the register holds the full form and the surname (design limit)
- d2-firstname-lower-login: escape via raw. a first name alone, the register holds the full form and the surname (design limit)
- d2-place-ae-alone: escape via raw. the place with an ae spelling the register does not list (design limit)
- d2-place-ae-lower: escape via raw. the place adjective with ae in lower case (design limit)
- d2-file-leading-zero: escape via raw. the file number without its leading zero (design limit)
- d2-tender-prefix-number: escape via raw. the prefix and number of the tender id without the year (design limit)
- d2-tender-nozero: escape via raw. the tender id without its leading zero (design limit)
- d2-md-typo-heading: escape via raw. a typo of a registered form standing alone, the register decides what a form is (design limit)
- d2-md-glue-codeblock: escape via raw. the acronym glued to a word that is no affix of rules/glue-affixes.txt (design limit)
- d2-md-inverted-list: escape via raw. a first name alone, the register holds the full form and the surname (design limit)
- d3-acr-prefix-caps: escape via raw. the acronym glued to a word that is no affix of rules/glue-affixes.txt (design limit)
- d3-person-two-lines-inverted: escape via raw. a first name alone, the register holds the full form and the surname (design limit)
- d3-label-typo: forced-only via raw. a typo inside a candidate shape: blocked without --force, readable with it (forced-only)
- d3-label-firstname: forced-only via raw. the first name after a label: blocked without --force (forced-only)
- d3-acr-typo-company: forced-only via word. a typo inside a candidate shape: blocked without --force, readable with it (forced-only)
- d3-acr-typo-resource: escape via word. a typo of a registered form standing alone, the register decides what a form is (design limit)
- d3-ansprechpartner-firstname: escape via raw. a first name alone, the register holds the full form and the surname (design limit)
- d4-label-customer-typo: forced-only via raw. a typo inside a candidate shape: blocked without --force, readable with it (forced-only)
- d4-file-nosep-glued: forced-only via raw. the file number without its separator glued to a label: blocked without --force (forced-only)
- d4-tender-nosep-suffix-glued: escape via raw. the tender id without separators and a lot suffix glued (design limit)
- d4-tender-nosep-suffix-hyphen: unseen. the output is withheld by the second check: nothing leaks, the file stays in the inbox with a note
- d5-file-nosep-glued-neutral: escape via raw. the file number without its separator glued to a label (design limit)
- d5-acr-in-allowed-url-glued: escape via raw. the acronym glued to a word that is no affix of rules/glue-affixes.txt (design limit)
- d6-person-comma-title: forced-only via raw. a typo inside a candidate shape: blocked without --force, readable with it (forced-only)

Clipped, not by design (the marker lost its first letter to the form before it):

- d-compound-place: clipped
- d-surname-title: clipped
- d-slug: clipped
- d-place-address: clipped
- d-org-ae-oe: clipped
- d2-initial-surname: clipped
- d3-org-full-lower-ae: clipped
- d5-law-no-ampersand: clipped

## office

- docx-sdt-listitem: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- docx-table-caption: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- docx-field-instr: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- docx-first-even-header: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- docx-glossary: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- docx-customxml: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- docx-docvars: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- docx-people: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- docx-custom-prop-blob: unseen. a binary property is decoded for detection only
- docx-embedded-font: unseen. an embedded font gives a note and marks the file incomplete; its name table is scanned for registered forms only
- xlsx-rph: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- xlsx-numfmt: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- xlsx-dv-list: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- xlsx-cf-formula: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- xlsx-hyperlink-tooltip: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- xlsx-extlink-cache: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- xlsx-chart-cache: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- xlsx-vml-textbox: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- xlsx-connection: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- xlsx-pivot-records: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- xlsx-veryhidden: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- pptx-tags: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output; every python-pptx deck blocks on the template's author, so the harness counts the marker of the blocked run too
- pptx-section-names: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output; every python-pptx deck blocks on the template's author, so the harness counts the marker of the blocked run too
- pptx-notes-master: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output; every python-pptx deck blocks on the template's author, so the harness counts the marker of the blocked run too
- odt-meta-userdefined: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- odt-settings: unseen. config items are scanned for registered forms only
- odt-tracked-deletion: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- ods-numfmt: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- odt-forms: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- ods-embedded-chart: unseen. an embedded chart object is counted and noted, not read
- docx-numbering-lvltext: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- docx-vba: unseen. a macro project gives a note and marks the file incomplete; the compressed module source is not read
- docx-header-renamed: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- xlsx-hyperlink-mailto: unseen. a hyperlink target goes to detection only (by design)
- xlsx-defined-name-const: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- xlsx-table-part: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- xlsx-querytable: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- xlsx-slicer: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- pptx-slide-orphan: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output; every python-pptx deck blocks on the template's author, so the harness counts the marker of the blocked run too
- docx-duplicate-entry: unseen. a repeated part name gives a note and marks the file incomplete; the shown copy is the second one
- xlsx-hidden-row-col: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- odt-styles-header: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- pptx-layout-placeholder: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output; every python-pptx deck blocks on the template's author, so the harness counts the marker of the blocked run too
- docx-picture-alt: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- docx-tracked-deletion: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output
- xlsx-comment-legacy: unseen. detection-only carrier by the reader's design: a form there is found and reported, the text never reaches the output

## pdf

- pdf-tounicode-remap: unseen. the font maps glyphs to other letters, the text layer does not carry the name (open point of the record)
- pdf-type3-vector: unseen. glyphs drawn as paths without a text layer, the page is noted empty
- pdf-outline: unseen. outline titles reach detection with a note, never the output (by design)
- pdf-hidden-ocg: unseen. content of an optional content group switched off gives no text, the file is noted unreadable
- pdf-image-scan: unseen. image only, noted, the picture held on the vault side
- pdf-js: unseen. document scripts reach detection with a note, never the output (by design)
- pdf-text-annot-iban: unseen. annotation contents reach detection with a note, never the output (by design)
- pdf-popup-annot: unseen. annotation contents reach detection with a note, never the output (by design)
- pdf-acroform-iban-noap-marked: unseen. form field values reach detection with a note, never the output (by design)
- pdf-incremental-superseded: unseen. a superseded revision is scanned for registered forms only, never rendered (by design)
- pdf-freetext-unregistered: forced-only via raw. an unregistered name in a FreeText annotation: blocked without --force, readable with it (forced-only)
- pdf-acroform-unregistered-noap: unseen. form field values reach detection with a note; an unregistered name there blocks the run (by design)
- pdf-stamp-annot: unseen. annotation contents reach detection with a note, never the output (by design)

Clipped, not by design (the marker lost its first letter to the form before it):

- pdf-plain-name: clipped
- pdf-two-columns: clipped
- pdf-embedded-docx: clipped
- pdf-portfolio: clipped
- pdf-invisible-name: clipped
- pdf-junk-prefix: clipped
- pdf-broken-xref: clipped
- pdf-many-pages: clipped
- pdf-invisible-tiny-name: clipped

## mail_archive

- m-hdr-routing: unseen. headers outside the reader's list go to detection only, by design of the reader
- m-body-utf16-unlabelled: unseen. a NUL byte in the first 64 KB makes the whole file unknown, noted (design limit)
- m-alt-html-form: unseen. the alternative that is not chosen goes to detection only, by design of the reader
- m-smime-opaque: unseen. no reader for this format, the file is noted, review the original
- m-yenc-inline: unseen. an inline yEnc block is not decoded, noted (not promised)
- m-binhex-inline: unseen. an inline BinHex block is not decoded, noted (not promised)
- m-tnef: unseen. no reader for this format, the file is noted, review the original
- m-att-msg: unseen. no reader for this format, the file is noted, review the original
- m-att-over-limit: unseen. attachment past the limit, a limit of the reader, noted
- m-body-b64-docx-stored: unseen. a base64 block that decodes to a container stays a blob in the output, its strings go to detection (not promised)
- m-body-b64-docx-deflated: unseen. a base64 block that decodes to a container stays a blob in the output, its strings go to detection (not promised)
- m-body-utf16-8bit-labelled: unseen. a NUL byte in the first 64 KB makes the whole file unknown, noted (design limit)
- m-att-binary-cte: unseen. a NUL byte in the first 64 KB makes the whole file unknown, noted (design limit)
- m-body-b64-docx-stored-oneline: unseen. a base64 block that decodes to a container stays a blob in the output, its strings go to detection (not promised)
- m-body-b64-zip-stored: unseen. a base64 block that decodes to a container stays a blob in the output, its strings go to detection (not promised)
- m-body-single-nul: unseen. a NUL byte in the first 64 KB makes the whole file unknown, noted (design limit)
- m-rfc822-depth4: unseen. nesting past the depth limit, a limit of the reader, noted
- m-broken-boundary-docx: unseen. a base64 block that decodes to a container stays a blob in the output, its strings go to detection (not promised)
- a-zip-7075-form-in-extra: unseen. the raw name of a member with a unicode path field goes to detection only, by design of the reader
- a-zip-comments: unseen. archive and member comments go to detection only, by design
- a-zip-encrypted: unseen. an encrypted member, a limit of the reader, noted
- a-zip-local-name-differs: unseen. a bad zip, a limit of the reader, noted
- a-zip-over-members: unseen. member past the limit, a limit of the reader, noted
- a-tar-names-links-pax: unseen. link targets, pax comment, uname and gname go to detection only, by design
- a-gz-single-header: unseen. the gzip header name and comment go to detection only, by design
- a-targz-header: unseen. the gzip header name and comment go to detection only, by design
- a-7z: unseen. no reader for this format, the file is noted, review the original
- a-rar: unseen. no reader for this format, the file is noted, review the original
- a-nested-depth4: unseen. nesting past the depth limit, a limit of the reader, noted
- a-tar-past-bytes: unseen. a member past the byte limit, a limit of the reader, noted
- a-zstd: unseen. no reader for this format, the file is noted, review the original

## nontext

- rtf-objdata: unseen. an OLE Package inside rtf objdata hex holds the text file; the carrier is noted, its content reaches detection only (the hang of the phone pattern is fixed, 2026-09-27 evening; the case runs again)
- rtf-objdata-lines: unseen. the same OLE Package written in 128 character lines (the hang of the phone pattern is fixed, 2026-09-27 evening; the case runs again)
- rtf-objdata-zip: unseen. an OLE Package holding a compressed docx; the carrier is noted (the hang of the phone pattern is fixed, 2026-09-27 evening; the case runs again)
- txt-xxd-jpg: unseen. a jpg with EXIF as xxd -p lines decodes to a picture, held for a look, its strings reach detection only (the hang of the phone pattern is fixed, 2026-09-27 evening; the case runs again)
- docx-media-svg: unseen. an svg media part is held as a picture, its text nodes reach detection only (by design)
- docx-media-emf: unseen. a metafile media part is held as a picture, its text records reach detection only (by design)
- docx-media-emz: unseen. a compressed metafile is counted in the picture note, not held (open point)
- docx-ole-object: unseen. an embedded object is counted and noted, not read
- docm-vba: unseen. a macro project gives a note and marks the file incomplete; the compressed module source is not read
- docx-embedded-font: unseen. an embedded font gives a note and marks the file incomplete; its name table is scanned for registered forms only
- docx-thumbnail: unseen. a thumbnail is a picture: held, its metadata never scanned
- docx-picture-only: unseen. picture only, noted, held
- pptx-master-only: unseen. slide master text goes to detection only (by design); every python-pptx deck blocks on the template's author, so the harness counts the marker of the blocked run too
- xlsx-chart: unseen. chart text goes to detection only, by design of the xlsx reader
- xlsx-validation-prompt: unseen. validation prompt and error texts go to detection only, by design of the xlsx reader
- jpg-metadata: unseen. a picture is held on the vault side for the owner's eyes; its metadata is never scanned (open point of the record)
- png-text: unseen. a picture is held on the vault side for the owner's eyes; its metadata is never scanned (open point of the record)
- tiff-pages: unseen. a picture is held on the vault side for the owner's eyes; its metadata is never scanned (open point of the record)
- webp-exif: unseen. a picture is held on the vault side for the owner's eyes; its metadata is never scanned (open point of the record)
- gif-comment: unseen. a picture is held on the vault side for the owner's eyes; its metadata is never scanned (open point of the record)
- heic: unseen. a picture is held on the vault side for the owner's eyes; its metadata is never scanned (open point of the record)
- zip-with-jpg: unseen. a picture inside a container is noted, not held (open point of the record)
- eml-with-jpg: unseen. a picture inside a container is noted, not held (open point of the record)
- mp3-id3: unseen. no reader for this format, the file is noted, review the original
- onenote: unseen. no reader for this format, the file is noted, review the original
- pst: unseen. no reader for this format, the file is noted, review the original
- parquet: unseen. no reader for this format, the file is noted, review the original
- sqlite: unseen. no reader for this format, the file is noted, review the original
- doc-ole: unseen. no reader for this format, the file is noted, review the original
- xlsb: unseen. sniffed as a workbook, the binary part fails, noted as failed
- pages-iwork: unseen. a picture inside a container is noted, not held (open point of the record); the format has no reader
- rtf-pict-emf: unseen. a metafile in an rtf is decoded for detection, not expanded
- pdf-zip-polyglot: unseen. trailing data after the end of the document is dropped
- zip-xl-folder: unseen. a zip with an xl folder is sniffed as a workbook and fails, noted as failed
- emf-file: unseen. a picture is held on the vault side for the owner's eyes; its metadata is never scanned (open point of the record)
- wmf-file: unseen. a picture is held on the vault side for the owner's eyes; its metadata is never scanned (open point of the record)
- docx-media-svg-late: unseen. names beyond the 64 KB probe of a media part are not scanned
- pptx-picture-only: unseen. picture only, noted, held; every python-pptx deck blocks on the template's author, so the harness counts the marker of the blocked run too
- pdf-image-only: unseen. image only, noted, held
- pdf-zip-polyglot-ok: unseen. trailing data after the end of the document is dropped
- rtf-pict-png: unseen. a picture in an rtf is decoded for detection, not expanded
- jpg-zip-polyglot: unseen. a picture with trailing data is held as a picture
- sevenz: unseen. no reader for this format, the file is noted, review the original
- ics-attach-docx: unseen. a base64 block that decodes to a container stays a blob in the output, its strings go to detection (not promised)
- vcf-photo-exif: unseen. a picture inside a container is noted, not held (open point of the record)
- docx-text-prefix: unseen. a docx behind a text prefix is unknown, noted
- html-data-uri-jpg: unseen. a base64 block that decodes to a picture is noted, not expanded (not promised)
- txt-b64-single-jpg: unseen. a base64 block that decodes to a picture is noted, not expanded (not promised)
- ics-attach-jpg: unseen. a picture inside a container is noted, not held (open point of the record)
- odt-chart-object: unseen. an embedded chart object is counted and noted, not read
- xlsx-image-exif: unseen. a picture inside a workbook is held, its metadata never scanned (open point of the record)
- vcf-photo-oneline: unseen. a picture inside a container is noted, not held (open point of the record)
- txt-mime-b64-jpg-aligned: unseen. a base64 block that decodes to a picture is noted, not expanded (not promised)
- txt-hex-single-jpg: unseen. a hex block that decodes to a picture gives its strings to detection, not expanded (not promised)
- txt-uuencode-jpg: unseen. a uuencoded picture in a plain text file is not decoded (not promised)
- xlsm-vba: unseen. a macro project gives a note and marks the file incomplete; the compressed module source is not read

Clipped, not by design (the marker lost its first letter to the form before it):

- docx-chart: clipped
- docx-altchunk: clipped
- docx-wordart-vml: clipped
- docx-textbox: clipped
- docx-omml-inline: clipped
- docx-omml-block: clipped
- pptx-table: clipped
- pptx-chart: clipped
- odg: clipped
- vsdx: clipped
- epub: clipped
- pdf-text-prefix: clipped
- pdf-no-prefix-control: clipped
- pdf-compressed-prefix: clipped
- docx-renamed-zip: clipped
- zip-renamed-docx: clipped
- html-renamed-txt-late: clipped
- svg-file-text: clipped
- docx-altchunk-txt: clipped
- docx-altchunk-docx: clipped
- docx-omml-cell: clipped
- pptx-smartart: clipped
- pdf-attachment: clipped
- pdf-ws-prefix: clipped
- pdf-prefix-literal: clipped
- pdf-prefix-long-hex: clipped
- pdf-compressed-prefix-big: clipped
- odg-split-spans: clipped
- pdf-flate-prefix-small: clipped
- pdf-flate-prefix-big: clipped
- pdf-nul-prefix: clipped
- docx-altchunk-mht: clipped
- eml-svg-attachment: clipped
- mht-file: clipped

## gate

- s-bearer-jwt: found token. reported as a token, another class by design (a JWT)
- s-kubeconfig: found private-key. the base64 of a PEM is reported as a private key, another class by design
- s-spaces-unquoted: found nothing. an unquoted value of words with spaces has low entropy (by design)
- s-bearer-key: found token. reported as a token, another class by design (a JWT)
- s-backslash-split: found nothing. a value split by a shell line continuation (design limit: secrets built by code)
- s-concat: found nothing. string concatenation in code (design limit: secrets built by code)
- s-argparse-default: found nothing. an argparse default, a call and no key word (design limit)
- s-comma-unquoted: found nothing. an unquoted value ends at the comma (design limit)
- s-jwt-dotted: found token. reported as a token, another class by design (a JWT)
- s-azure-sas: found nothing. a SAS signature in a url query, sig is no key word (design limit)
- k-sk-3-lines-later: found nothing. a key pair three lines apart, the window is two lines (design limit)
- k-ak-lower: found nothing. a lower-case key id, the id shape is upper case (design limit)
- k-sk-alone: found nothing. a secret without its key id nearby (design limit)
- p-body-only: found nothing. a PEM body without armour lines (design limit)
- p-armour-lower: found nothing. armour in lower case (design limit)
- p-armour-endash: found nothing. en dashes for the armour dashes (an encoding the normaliser does not undo)
- p-armour-split: found nothing. armour wrapped over two lines (design limit)
- h-dashed-inside-word: found nothing. -home-name inside a longer dashed word (design limit)
- h-split-line: found nothing. a path split by a line break (design limit)
- h-ssh-url: found nothing. a home path inside an ssh url after the host (design limit)
- h-upper: found nothing. an upper-case home path (design limit)
- o-space: found nothing. a blocked name spelled with a space (the pattern is the list, by design)
- o-customer-lower: found nothing. a blocked naming scheme in lower case (the list decides the case, by design)
- o-customer-letter-o: found nothing. a blocked naming scheme with a letter O (by design)
- o-ptck-bare: found nothing. a blocked naming scheme without digits (by design)
- o-detect-lower: found nothing. a blocked marker in lower case (by design)
- o-en-dash: found nothing. a blocked name with an en dash (an encoding the normaliser does not undo)
- i-hex-spaced: found nothing. a 32 hex id in spaced groups (design limit)
- i-hex-dash-split: found nothing. a 32 hex id with one dash (design limit)
- i-tenant-newline: found nothing. the label and the number on two lines (design limit)
- i-domain-name-shape: found nothing. the number inside a platform domain name (design limit)
- i-twilio-ac: found token. AC plus 32 hex is reported as a token, another class by design
- n-cp437: found nothing. a DOS code page file is not decoded (design limit)
- n-pdf-hexstring: found opaque. a PDF is opaque by suffix, reported as opaque (by design)
- n-pdf-plain: found opaque. a PDF is opaque by suffix, reported as opaque (by design)
- n-rot13: found nothing. rot13 (design limit)
- s2-jwt-with-dash-control: found token. reported as a token, another class by design (a JWT)
- s2-env-get-default: found nothing. an os.environ.get default, a call and no assignment (design limit)
- s2-triple-quotes: found nothing. a triple-quoted value (design limit)
- s2-bearer-curl: found token. reported as a token, another class by design (a JWT)
- n2-python-octal-escape: found nothing. python octal escapes (an encoding the normaliser does not undo)
- n2-utf7: found nothing. utf-7 text (an encoding the normaliser does not undo)
- s3-placeholder-word-inside: found nothing. a placeholder word inside the value reads as a placeholder (design limit)
- s3-your-inside: found nothing. a placeholder word inside the value reads as a placeholder (design limit)
- n4-pdf-octal-escape: found opaque. a PDF is opaque by suffix, reported as opaque (by design)

## wipe-overview

The nine dimensions of wipe mode (calibration/redteam/wipe/, 405 cases, run against the built intake on
2026-10-08 with `calibration/redteam/wipe/harness.py`): 7 cases leak a value and 7 lose a term, no output is
withheld, 20 values carry another class than the case expects (the value is wiped, only the hint differs). The
prototype of the red team stood at 9 and 11; every case it marked fixable is clean. One line per leftover below,
each a limit, an artefact or a structured pattern; the record is presentations/names/REDTEAM.md outside the
repository.

## wipe-transcripts

- none: every case clean

## wipe-mail-chains

- none: every case clean

## wipe-decks

- none: every case clean

## wipe-code-files

- tf-resource-label-lower-surname: leak. a surname in lower case in a Terraform resource label with no capitalised form anywhere in the file (limit)
- tf-acr-vm-glued-hostname: leak. the registered acronym glued to vm and digits, vm is no glue affix (limit; adding vm to rules/glue-affixes.txt is a one-line change for his word)
- tf-comment-lower-surname: leak. a surname in lower case in a comment with no capitalised form anywhere in the file (limit)
- py-module-path-brand-surname: leak. a lower-case dotted module path: no shape tells a brand from a package name (limit)
- py-attr-access-read-as-host: loss. config.name and os.run read as bare hosts by the structured patterns (limit of patterns.py: a two-label host under a word-like top level)
- md-readme-julia-line-start: loss. Julia at a line start is a first name of the list (limit; the keep list repairs it)

## wipe-tables

- md-keyvalue-steckbrief: loss. a system name under Verantwortlich: whose rest is one unknown word, the shape of a surname (limit; keep list)
- md-sql-qualified-columns: loss. table.column of SQL read as a bare host by the structured patterns (limit of patterns.py)
- docx-sql-keyword-outside-stoplist: loss. SQL keywords the stop list does not carry, in a document (limit; keep list or stop words)
- docx-keyvalue-team-values: loss. a team under Name: in a key-value table whose rest is one unknown word (limit; keep list)

## wipe-german

- de-legal-footer-eml: loss. the commercial register number in the keep list is a structured token on purpose (case artefact)
- de-surname-klein: leak. the planted surname equals the adjective the case keeps on purpose (harness artefact, the output is right)
- de-lone-after-preposition: leak. a lone unknown capitalised word after a preposition in German prose (limit)

## wipe-russian

- ru-customer-translit-short-alone: leak. the registered customer transliterated into Cyrillic: the register decides forms (decisions 19 and 23)

## wipe-disguised

- none: every case clean

## wipe-losses

- none: every case clean
