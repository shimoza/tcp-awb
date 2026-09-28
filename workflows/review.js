export const meta = {
  name: 'awb-review',
  description: 'Tier 2 and 3 review of one deliverable: blind checks of its high-risk claims, one lens per platform, a partner lens with fixed yes or no questions, a refute agent for every blocking finding, JSON results in reviews/<name>/lenses/',
  whenToUse: 'After awb review claims and awb review l0 for a tier 2 or tier 3 deliverable, before awb review pass. Args: {project: absolute project folder, deliverable: deliverables/<name>, platforms: ["tcp", "azure"], date: "YYYY-MM-DD", mirrors: "/srv/tcp-mirrors"}',
  phases: [
    { title: 'Prepare', detail: 'read contract.md and claims.tsv, turn every high-risk claim into an open question without its answer' },
    { title: 'Blind verification', detail: 'answer each open question from sources without the draft, then compare the answer with the claim' },
    { title: 'Lenses', detail: 'one lens per platform and the partner lens with fixed yes or no questions, each with the blind results' },
    { title: 'Refute', detail: 'one refute agent for every blocking finding' },
    { title: 'Record', detail: 'write one JSON file per lens into reviews/<name>/lenses/' },
  ],
}

// The review workflow of the Architect Workbench (awb/review.py, INTERFACES.md "Release 2").
//
// Input (args): project is the absolute path of the project folder, deliverable is the path of the deliverable
// relative to the project (deliverables/<name>) or absolute, platforms lists one lens per platform (the target
// platform tcp or hcs and every source platform such as azure, aws, gcp, vmware, onprem; default ["tcp"]), date is
// the ISO date stamped into the results (the script cannot read a clock), mirrors is the folder of the read-only
// documentation mirrors (default /srv/tcp-mirrors, where the seal mounts them; before the seal the owner's own
// folder).
//
// Output: one JSON file per lens in <project>/reviews/<name>/lenses/, written by the agents of the Record phase:
//   verify.json            the blind verification: per high-risk claim a verdict, and a finding for each claim
//                          that is not supported (contradicted is blocking, unknown is major)
//   platform-<p>.json      one per platform
//   partner.json           the partner lens: fixed yes or no questions, a finding for each bad answer
// Every file: {lens, kind, platform, deliverable, date, dropped, findings: [{id, line, claim, class, severity,
// blocking, summary, quote, true, evidence, replacement, source, outcome, refute_reason}]}. verify.json also
// carries "run": {agents, tokens} for the record. A blocking finding leaves this workflow with the outcome "refuted"
// (a refute agent proved it wrong), "plausible" (the refute agent could not settle it: soften the sentence or tag
// it and set "softened") or "open" (the refute agent confirmed it: fix it and set "confirmed-fixed").
// `awb review pass` refuses while a blocking finding is open. The blind answers go to
// <project>/evidence/review/<name>/<claim id>.md, a path he can give as evidence in claims.tsv.
//
// Order (document 06): the blind verification runs first and the lenses read its results (V-11). A lens finding
// needs a quote (or a claim id) and evidence (or a source), else it is dropped and counted (V-09). The prepare
// step takes only the high-risk claims without a verdict, so after a rewrite only new and changed claims are
// checked again (V-14).

const OUTCOME_OPEN = 'open'
const OUTCOME_REFUTED = 'refuted'
const OUTCOME_PLAUSIBLE = 'plausible'
const TARGETS = ['tcp', 'hcs']
const GRADES = ['live', 'contract', 'docs', 'said', 'assumed']
const CLASSES = ['WRONG', 'MISLEADING', 'INVENTED', 'UNSOURCED', 'MISREAD', 'EXTRA', 'OTHER']

function fail(message) {
  throw new Error('awb-review: ' + message)
}

function readArgs(raw) {
  let a = raw
  if (typeof a === 'string') {
    try {
      a = JSON.parse(a)
    } catch (e) {
      a = null
    }
  }
  if (!a || typeof a !== 'object') fail('args must be an object: {project, deliverable, platforms, date}')
  const project = String(a.project || '').replace(/\/+$/, '')
  if (!project.startsWith('/')) fail('args.project must be the absolute path of the project folder')
  let deliverable = String(a.deliverable || '')
  if (deliverable.startsWith(project + '/')) deliverable = deliverable.slice(project.length + 1)
  if (!deliverable.startsWith('deliverables/') || deliverable.split('/').includes('..')) {
    fail('args.deliverable must lie in the deliverables folder of the project')
  }
  const name = deliverable.slice('deliverables/'.length)
  if (!name) fail('args.deliverable names no file')
  const given = Array.isArray(a.platforms) && a.platforms.length ? a.platforms : ['tcp']
  const platforms = []
  for (const p of given) {
    const v = String(p).toLowerCase().trim()
    if (/^[a-z0-9][a-z0-9-]{0,30}$/.test(v) && !platforms.includes(v)) platforms.push(v)
  }
  if (!platforms.length) fail('args.platforms holds no usable platform')
  const date = typeof a.date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(a.date) ? a.date : ''
  const mirrors = typeof a.mirrors === 'string' && /^\/[A-Za-z0-9._\/-]+$/.test(a.mirrors) ? a.mirrors.replace(/\/+$/, '') : '/srv/tcp-mirrors'
  return { project, deliverable, name, platforms, date, mirrors }
}

const A = readArgs(args)
const DELIVERABLE = A.project + '/' + A.deliverable
const REVIEW = A.project + '/reviews/' + A.name
const CONTRACT = REVIEW + '/contract.md'
const CLAIMS = REVIEW + '/claims.tsv'
const LENSES_DIR = REVIEW + '/lenses/'
const SLUG = A.name.replace(/[^A-Za-z0-9._-]+/g, '-')
const EVIDENCE_REL = 'evidence/review/' + SLUG

const RULES = [
  'Rules for this task:',
  '- Work only inside the project folder ' + A.project + ', the knowledge base (awb kb find WORDS, awb kb show ID), the read-only documentation mirrors under ' + A.mirrors + '/ when they exist and public vendor documentation on the web. Open no other folder on this host.',
  '- Write codes only (a customer is CUST-XXXX, a project tcp-xxxx). Never write the name of a customer, a partner or a person into a result, even when you think you know it.',
  '- Quote the deliverable with at most five words at a time.',
  '- Cite a file by its path below its folder (docs mirror: <repo>/<path>, project: evidence/<file>), never with the home folder in front: the commit gate refuses home paths.',
  '- Plain English. No em-dash. No comma before "and" or "or".',
].join('\n')

// --------------------------------------------------------------------------- schemas

const PREP_SCHEMA = {
  type: 'object',
  properties: {
    tier: { type: 'integer', minimum: 0, maximum: 3 },
    questions: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          claim: { type: 'string' },
          line: { type: 'integer' },
          kind: { type: 'string' },
          platform: { type: 'string' },
          sentence: { type: 'string' },
          question: { type: 'string' },
        },
        required: ['claim', 'sentence', 'question'],
      },
    },
  },
  required: ['tier', 'questions'],
}

const BLIND_SCHEMA = {
  type: 'object',
  properties: {
    answer: { type: 'string' },
    sources: { type: 'array', items: { type: 'string' } },
    tried: { type: 'array', items: { type: 'string' } },
    grade: { type: 'string', enum: GRADES },
    evidence_file: { type: 'string' },
  },
  required: ['answer', 'sources', 'grade', 'evidence_file'],
}

const COMPARE_SCHEMA = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['supported', 'contradicted', 'unknown'] },
    reason: { type: 'string' },
  },
  required: ['verdict', 'reason'],
}

const FINDINGS_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          line: { type: 'integer' },
          claim: { type: 'string' },
          class: { type: 'string', enum: CLASSES },
          severity: { type: 'string', enum: ['blocking', 'major', 'minor'] },
          summary: { type: 'string' },
          quote: { type: 'string' },
          true: { type: 'string' },
          evidence: { type: 'string' },
          replacement: { type: 'string' },
          source: { type: 'string' },
        },
        required: ['class', 'severity', 'summary'],
      },
    },
  },
  required: ['findings'],
}

const PARTNER_QUESTIONS = [
  { id: 'P1', good: 'yes', blocking: true, cls: 'MISREAD', q: 'Does the deliverable answer the request in contract.md and every question listed there?' },
  { id: 'P2', good: 'yes', blocking: false, cls: 'EXTRA', q: 'Does the deliverable leave out what the contract puts out of scope and what nobody asked for?' },
  { id: 'P3', good: 'yes', blocking: true, cls: 'WRONG', q: 'Can the platform operator sell, contract and support every service, flavor and region the deliverable offers?' },
  { id: 'P4', good: 'no', blocking: true, cls: 'INVENTED', q: 'Does the deliverable promise a date, a service level, a discount or another commitment that nobody agreed to?' },
  { id: 'P5', good: 'yes', blocking: true, cls: 'UNSOURCED', q: 'Does every price come from the live price source or the contract, with the date it was checked?' },
  { id: 'P6', good: 'no', blocking: true, cls: 'UNSOURCED', q: 'Does the deliverable state a certification or a compliance status of the platform?' },
  { id: 'P7', good: 'no', blocking: true, cls: 'OTHER', q: 'Does the deliverable carry a name, a mail address, a phone number or an internal identifier where a code belongs?' },
  { id: 'P8', good: 'yes', blocking: false, cls: 'MISLEADING', q: 'Would the reader named in the contract understand who operates and who supports each part of the solution?' },
  { id: 'P9', good: 'yes', blocking: true, cls: 'UNSOURCED', q: 'Does every negative statement (not available, not supported, does not exist) say what was tried to reach it?' },
  { id: 'P10', good: 'yes', blocking: true, cls: 'INVENTED', q: 'Is the deliverable free of internal contradictions (a number, a region or a service that differs between two places)?' },
  { id: 'P11', good: 'yes', blocking: false, cls: 'EXTRA', q: 'Does the deliverable stay within the word budget of the contract?' },
]

const PARTNER_SCHEMA = {
  type: 'object',
  properties: {
    answers: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string', enum: PARTNER_QUESTIONS.map((x) => x.id) },
          answer: { type: 'string', enum: ['yes', 'no', 'n/a'] },
          line: { type: 'integer' },
          note: { type: 'string' },
        },
        required: ['id', 'answer', 'note'],
      },
    },
  },
  required: ['answers'],
}

const REFUTE_SCHEMA = {
  type: 'object',
  properties: {
    refuted: { type: 'boolean' },
    outcome: { type: 'string', enum: ['confirmed', 'refuted', 'plausible'] },
    reason: { type: 'string' },
    source: { type: 'string' },
  },
  required: ['refuted', 'reason'],
}

const WRITTEN_SCHEMA = {
  type: 'object',
  properties: {
    path: { type: 'string' },
    same: { type: 'boolean' },
  },
  required: ['path', 'same'],
}

// --------------------------------------------------------------------------- prompts

function prepPrompt() {
  return [
    'Prepare the blind verification of one deliverable.',
    'Read ' + CONTRACT + ' (the tier is on the line "- tier: N") and ' + CLAIMS + ' (tab separated: id, line, kind, risk, sentence, evidence, grade, verdict).',
    'Do not open the deliverable itself.',
    'For every row with risk high and an empty verdict column, write one open question that a person who never saw the draft can answer from primary sources.',
    'A row that already has a verdict was checked in an earlier round and keeps it: only new and changed claims are checked again.',
    'The question must not carry the answer or hint at it: no yes or no question that repeats the claim, no number or name of the sentence offered as a proposal.',
    'Example: for a sentence saying a flavor family is not offered in region eu-nl, ask "Which flavor families are offered in region eu-nl today?".',
    'Give the platform the question is about, one of: ' + A.platforms.join(', ') + '.',
    'Copy id, line, kind and sentence of the row as they are.',
    'Return the tier and the list. An empty list is right when no row has risk high.',
    '',
    RULES,
  ].join('\n')
}

function isTarget(platform) {
  return TARGETS.includes(platform)
}

function platformTitle(platform) {
  if (platform === 'tcp') return 'T Cloud Public (TCP)'
  if (platform === 'hcs') return 'the HCS private cloud stack'
  return platform
}

function sourcesFor(platform) {
  if (isTarget(platform)) {
    return 'the knowledge base first (awb kb find WORDS; note grade and checked date, an EXPIRED entry does not count alone), the documentation mirrors under ' + A.mirrors + '/ (docs/<service>/ and service-description/), the public help center of the platform and a cheap read-only live check when the project has access'
  }
  return 'the own primary documentation of ' + platform + ' (vendor documentation, service limits pages, pricing pages)'
}

function blindPrompt(q) {
  const platform = String(q.platform || A.platforms[0])
  const file = EVIDENCE_REL + '/' + q.claim + '.md'
  return [
    'You check one fact blind. You do not know what any draft says about it and you must not look:',
    'do not open the folders deliverables/ and reviews/ of the project.',
    '',
    'Platform: ' + platformTitle(platform),
    'Question: ' + q.question,
    '',
    'Answer from primary sources: ' + sourcesFor(platform) + '.',
    'A negative answer (not available, not supported, does not exist) needs at least two different ways you tried; list them.',
    'Write your answer, the sources and what you tried to ' + A.project + '/' + file + ' (create the folder when it is missing), codes only.',
    'Return the answer, the sources, what you tried, the grade (live, contract, docs, said or assumed) and evidence_file "' + file + '".',
    '',
    RULES,
  ].join('\n')
}

function comparePrompt(q, answer) {
  return [
    'Compare one claim of a draft with an answer that was found blind, without the draft.',
    'Claim ' + q.claim + ' (line ' + (q.line || 0) + '): ' + q.sentence,
    'Blind question: ' + q.question,
    'Blind answer: ' + answer.answer,
    'Sources of the answer: ' + (answer.sources || []).join('; '),
    '',
    'Verdict supported: the answer backs the claim as written.',
    'Verdict contradicted: the answer says otherwise in any part (a number, a region, a flavor, a version, a negative).',
    'Verdict unknown: the answer does not settle it.',
    'Do not search further. Judge only what is given. Return the verdict and a reason of one or two sentences.',
    '',
    RULES,
  ].join('\n')
}

function blindSummary(verify) {
  const rows = (verify && verify.claims) || []
  if (!rows.length) return 'The blind verification checked no claim.'
  return ['The blind verification ran before you, without the draft. Its verdicts per claim of claims.tsv:']
    .concat(rows.map((c) => '- ' + c.claim + ' (line ' + c.line + '): ' + c.verdict + (c.grade ? ', grade ' + c.grade : '') + (c.evidence ? ', evidence ' + c.evidence : '')))
    .concat(['Use them: a contradicted claim needs no second finding from you, an unknown one is worth your own check.'])
    .join('\n')
}

function platformPrompt(platform, verify) {
  const head = isTarget(platform)
    ? 'You are the target platform architect for ' + platformTitle(platform) + '. Check every statement about the target platform: services, flavors, regions, availability, limits, API paths, versions and prices. Walk every negative statement and look for the source that proves it; a negative without a source is UNSOURCED.'
    : 'You are a ' + platform + ' cloud architect. Check every statement and every analogy about ' + platform + '. A mapping from a ' + platform + ' service to a target service must hold in what matters to the reader (scope, limits, cost model); flag an analogy that misleads.'
  return [
    head,
    'Read the contract ' + CONTRACT + ', the claim list ' + CLAIMS + ' and then the deliverable ' + DELIVERABLE + '.',
    'Check against ' + sourcesFor(platform) + '.',
    'Classes: WRONG (a source says otherwise), MISLEADING (true in words, wrong in effect), INVENTED (no source has it: a service, an API, a flavor or a feature that does not exist), UNSOURCED (a negative or a number with no source), MISREAD (the deliverable answers another question than the contract asks), EXTRA (content nobody asked for), OTHER.',
    'Severity blocking for WRONG, INVENTED, MISREAD and for an UNSOURCED negative; major or minor for the rest.',
    'Every finding has one shape: the line, the claim id of claims.tsv when there is one, quote (the sentence, at most five words of it), true (what is true instead), evidence (a path in the project, a command or a URL that shows it), replacement (the sentence as it should read), a summary of one sentence and the source you used.',
    'A finding without a quote or a claim id, or without evidence or a source, is dropped by the script: give both or leave the finding out.',
    'Return an empty list when you find nothing. Do not invent findings to fill the list.',
    '',
    blindSummary(verify),
    '',
    RULES,
  ].join('\n')
}

function partnerPrompt(verify) {
  return [
    'You read the deliverable as the platform operator and its sales partner: the party that has to sell it, contract it and support it.',
    'Read the contract ' + CONTRACT + ' first, then the deliverable ' + DELIVERABLE + '.',
    'Answer each question with yes, no or n/a. Use n/a only when the question does not apply at all, for example when there is no price in the text.',
    'For each answer give the line that decides it (0 when there is none) and a note of one sentence.',
    '',
    PARTNER_QUESTIONS.map((x) => x.id + ' ' + x.q).join('\n'),
    '',
    blindSummary(verify),
    '',
    RULES,
  ].join('\n')
}

function refutePrompt(lens, f) {
  const lines = [
    'Try to refute one blocking finding of a review. You are a skeptic of the finding, not of the deliverable: show that the finding is wrong and the deliverable is right as written.',
    'Deliverable: ' + DELIVERABLE + ', around line ' + (f.line || 0) + '.',
    'Lens: ' + lens + '. Finding ' + f.id + ' (' + f.class + ', ' + f.severity + '): ' + f.summary,
    'Source given by the lens: ' + (f.source || 'none'),
  ]
  if (f.context) {
    lines.push('Blind question: ' + f.context.question)
    lines.push('Blind answer: ' + f.context.answer)
  }
  lines.push(
    'Check the primary sources yourself. Refuted means you found a source that proves the finding wrong. When you are not sure, it is not refuted.',
    'Also give the outcome: confirmed when a source backs the finding, refuted when a source proves it wrong, plausible when no source settles it either way.',
    'Return refuted, the outcome, a reason of one or two sentences and the source.',
    '',
    RULES,
  )
  return lines.join('\n')
}

function recordPrompt(file) {
  return [
    'Write one file with the Write tool. Create its folder when it is missing.',
    'The content is everything between the two marker lines, without the marker lines. Change nothing in it.',
    'Then read the file back and check that it is the same text. Return the path and whether it is the same.',
    'path: ' + file.path,
    '----- begin -----',
    file.content + '----- end -----',
  ].join('\n')
}

// --------------------------------------------------------------------------- steps

function cleanFinding(f) {
  const out = {
    id: f.id,
    line: Number.isInteger(f.line) ? f.line : 0,
    claim: f.claim || '',
    class: CLASSES.includes(f.class) ? f.class : 'OTHER',
    severity: f.severity || 'major',
    blocking: f.severity === 'blocking',
    summary: f.summary || '',
    source: f.source || '',
    outcome: f.outcome || OUTCOME_OPEN,
  }
  for (const key of ['quote', 'true', 'evidence', 'replacement', 'question', 'refute_reason']) {
    if (f[key]) out[key] = String(f[key])
  }
  return out
}

// V-09: a finding of a lens needs a quote (or the claim id that points at the sentence) and evidence (or a source)
function shaped(f) {
  return Boolean((f.quote || f.claim) && (f.evidence || f.source))
}

function lensFile(id, kind, platform, findings, claims, dropped) {
  const out = {
    lens: id,
    kind: kind,
    platform: platform,
    deliverable: A.deliverable,
    date: A.date,
    dropped: dropped || 0,
    findings: findings.map(cleanFinding),
  }
  if (claims) out.claims = claims
  return out
}

let AGENT_RUNS = 0
let TOKENS_AT_START = null

function ask(prompt, opts) {
  AGENT_RUNS += 1
  return agent(prompt, opts)
}

// budget.spent() counts the output tokens of the whole turn, all workflows together: the record keeps the difference
// over this run, which includes whatever else ran at the same time.
function spentNow() {
  try {
    return typeof budget !== 'undefined' && budget && typeof budget.spent === 'function' ? Math.max(0, Math.round(budget.spent() || 0)) : 0
  } catch (e) {
    return 0
  }
}

function tokensSpent() {
  return Math.max(0, spentNow() - (TOKENS_AT_START || 0))
}

function failedLens(id, kind, platform, what) {
  return lensFile(id, kind, platform, [
    { id: id + '-1', class: 'OTHER', severity: 'blocking', summary: what + ' returned nothing; run the review again', outcome: OUTCOME_OPEN },
  ])
}

async function refuteFindings(lens, findings) {
  const numbered = findings.map((f, i) => Object.assign({}, f, { id: lens + '-' + (i + 1), outcome: OUTCOME_OPEN }))
  const done = await parallel(numbered.map((f) => async () => {
    if (f.severity !== 'blocking') return f
    const r = await ask(refutePrompt(lens, f), { label: 'refute ' + f.id, phase: 'Refute', schema: REFUTE_SCHEMA })
    if (r && (r.outcome === 'refuted' || (r.refuted === true && r.outcome !== 'plausible' && r.outcome !== 'confirmed'))) {
      return Object.assign({}, f, { outcome: OUTCOME_REFUTED, refute_reason: r.reason })
    }
    if (r && r.outcome === 'plausible') return Object.assign({}, f, { outcome: OUTCOME_PLAUSIBLE, refute_reason: r.reason })
    return Object.assign({}, f, { refute_reason: r ? r.reason : 'the refute agent returned nothing' })
  }))
  return done.map((x, i) => x || numbered[i])
}

async function verifyClaims(questions) {
  if (!questions.length) {
    log('no high-risk claims: the blind verification has nothing to check')
    return lensFile('verify', 'verify', 'all', [], [])
  }
  const checked = await pipeline(
    questions,
    (q) => ask(blindPrompt(q), { label: 'blind ' + q.claim, phase: 'Blind verification', schema: BLIND_SCHEMA }),
    async (answer, q) => {
      if (!answer) return { q: q, answer: null, v: null }
      const v = await ask(comparePrompt(q, answer), { label: 'compare ' + q.claim, phase: 'Blind verification', schema: COMPARE_SCHEMA })
      return { q: q, answer: answer, v: v }
    },
  )
  const rows = checked.map((r, i) => r || { q: questions[i], answer: null, v: null })
  const claims = rows.map((r) => ({
    claim: r.q.claim,
    line: Number.isInteger(r.q.line) ? r.q.line : 0,
    verdict: r.v ? r.v.verdict : 'unknown',
    grade: r.answer ? r.answer.grade : '',
    evidence: r.answer ? r.answer.evidence_file : '',
    reason: r.v ? r.v.reason : 'the blind check returned nothing',
  }))
  const findings = rows
    .filter((r) => !r.v || r.v.verdict !== 'supported')
    .map((r) => {
      const contradicted = Boolean(r.v && r.v.verdict === 'contradicted')
      return {
        claim: r.q.claim,
        line: r.q.line,
        class: contradicted ? 'WRONG' : 'UNSOURCED',
        severity: contradicted ? 'blocking' : 'major',
        summary: r.v ? r.v.reason : 'the blind check returned no answer',
        source: r.answer ? (r.answer.sources || []).join('; ') : '',
        context: { question: r.q.question, answer: r.answer ? r.answer.answer : '' },
      }
    })
  const refuted = await refuteFindings('verify', findings)
  return lensFile('verify', 'verify', 'all', refuted, claims)
}

async function runLens(job, verify) {
  if (job.kind === 'partner') {
    const res = await ask(partnerPrompt(verify), { label: job.id, phase: 'Lenses', schema: PARTNER_SCHEMA })
    if (!res) return null
    const byId = {}
    for (const a of res.answers || []) byId[a.id] = a
    const findings = []
    for (const x of PARTNER_QUESTIONS) {
      const a = byId[x.id]
      if (a && (a.answer === x.good || a.answer === 'n/a')) continue
      findings.push({
        question: x.id,
        line: a && Number.isInteger(a.line) ? a.line : 0,
        class: x.cls,
        severity: x.blocking ? 'blocking' : 'major',
        summary: a ? x.id + ': ' + a.note : x.id + ': no answer',
        source: '',
      })
    }
    return { findings: findings, dropped: 0 }
  }
  const res = await ask(platformPrompt(job.platform, verify), { label: job.id, phase: 'Lenses', schema: FINDINGS_SCHEMA })
  if (!res) return null
  const all = res.findings || []
  const kept = all.filter(shaped)
  return { findings: kept, dropped: all.length - kept.length }
}

// --------------------------------------------------------------------------- the run

TOKENS_AT_START = spentNow()
phase('Prepare')
const prep = await ask(prepPrompt(), { label: 'prepare', phase: 'Prepare', schema: PREP_SCHEMA })
if (!prep) fail('the prepare step returned nothing')
if (prep.tier < 2) {
  log('tier ' + prep.tier + ': this workflow is for tier 2 and 3, nothing was written')
  return { skipped: true, tier: prep.tier, lenses: [] }
}
const questions = (prep.questions || []).filter((q) => q && q.claim && q.question)
log(questions.length + ' high-risk claims go to the blind verification, ' + (A.platforms.length + 1) + ' lenses read the deliverable')

const LENS_JOBS = A.platforms
  .map((p) => ({ id: 'platform-' + p, kind: 'platform', platform: p }))
  .concat([{ id: 'partner', kind: 'partner', platform: 'partner' }])

phase('Blind verification')
const verify = (await verifyClaims(questions)) || failedLens('verify', 'verify', 'all', 'the blind verification')

phase('Lenses')
const lensRuns = await pipeline(
  LENS_JOBS,
  (job) => runLens(job, verify),
  async (res, job) => {
    if (!res) return failedLens(job.id, job.kind, job.platform, 'the lens ' + job.id)
    const refuted = await refuteFindings(job.id, res.findings || [])
    return lensFile(job.id, job.kind, job.platform, refuted, null, res.dropped)
  },
)
const lensResults = (lensRuns || []).map((l, i) => l || failedLens(LENS_JOBS[i].id, LENS_JOBS[i].kind, LENS_JOBS[i].platform, 'the lens ' + LENS_JOBS[i].id))
verify.run = { agents: AGENT_RUNS, tokens: tokensSpent() }
const lenses = [verify].concat(lensResults)

phase('Record')
const files = lenses.map((l) => ({ path: LENSES_DIR + l.lens + '.json', content: JSON.stringify(l, null, 2) + '\n' }))
const written = await parallel(files.map((f) => () => agent(recordPrompt(f), {
  label: 'write ' + f.path.split('/').pop(),
  phase: 'Record',
  schema: WRITTEN_SCHEMA,
  effort: 'low',
})))
const unwritten = files.filter((f, i) => !written[i] || written[i].same !== true).map((f) => f.path.split('/').pop())
if (unwritten.length) log('not written or not the same: ' + unwritten.join(', ') + '; run the Record phase again')

const summary = lenses.map((l) => ({
  file: 'reviews/' + A.name + '/lenses/' + l.lens + '.json',
  findings: l.findings.length,
  dropped: l.dropped || 0,
  blocking: l.findings.filter((f) => f.blocking).length,
  open_blocking: l.findings.filter((f) => f.blocking && f.outcome !== OUTCOME_REFUTED).length,
  plausible: l.findings.filter((f) => f.outcome === OUTCOME_PLAUSIBLE).length,
}))
const open = summary.reduce((n, s) => n + s.open_blocking, 0)
log(open + ' blocking findings are open')
return {
  lenses: summary,
  unwritten: unwritten,
  next: open
    ? 'fix the confirmed findings and set confirmed-fixed, soften or tag the plausible ones and set softened, then run the request check and awb review pass'
    : 'fill claims.tsv from verify.json where it helps, run the request check (workflows/request-check.js), then awb review pass',
}
