export const meta = {
  name: 'awb-request-check',
  description: 'The final gate of a review: one agent reads only the contract, the claim list and the final text and says where each question is answered, what nobody asked for and which claim is stated more firmly than its grade allows; the result goes to reviews/<name>/lenses/request-check.json',
  whenToUse: 'Last step before awb review pass of a tier 3 deliverable, after every fix and after the cut to the budget. Args: {project: absolute project folder, deliverable: deliverables/<name>, date: "YYYY-MM-DD"}',
  phases: [
    { title: 'Request check', detail: 'one agent with the contract, the claim list and the final text, nothing else' },
    { title: 'Record', detail: 'write lenses/request-check.json' },
  ],
}

// The request check of the Architect Workbench (document 06, L6 and V-16).
//
// Input (args): project is the absolute path of the project folder, deliverable is the path of the deliverable
// relative to the project (deliverables/<name>) or absolute, date is the ISO date stamped into the result.
//
// Output: <project>/reviews/<name>/lenses/request-check.json in the lens format that `awb review pass` reads:
//   {lens: "request-check", kind: "request", platform: "request", deliverable, date, dropped: 0,
//    questions: [{question, answered, line, note}], findings: [...]}
// A question that is not answered is a blocking MISREAD finding, a row of the claim list stated more firmly than its
// grade allows is a blocking MISLEADING finding, content nobody asked for is a major EXTRA finding and a statement
// of fact that has no row in the claim list is a major UNSOURCED finding (the author adds the row; the calibration of
// 2026-09-25 showed that judging such sentences as overclaims blocks every clean text). He fixes the text and sets
// "confirmed-fixed", or "softened" for a claim he softened. The send gate of `awb bucket put` refuses a tier 3
// deliverable of a customer or partner whose lenses hold no request check.

const OUTCOME_OPEN = 'open'

function fail(message) {
  throw new Error('awb-request-check: ' + message)
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
  if (!a || typeof a !== 'object') fail('args must be an object: {project, deliverable, date}')
  const project = String(a.project || '').replace(/\/+$/, '')
  if (!project.startsWith('/')) fail('args.project must be the absolute path of the project folder')
  let deliverable = String(a.deliverable || '')
  if (deliverable.startsWith(project + '/')) deliverable = deliverable.slice(project.length + 1)
  if (!deliverable.startsWith('deliverables/') || deliverable.split('/').includes('..')) {
    fail('args.deliverable must lie in the deliverables folder of the project')
  }
  const name = deliverable.slice('deliverables/'.length)
  if (!name) fail('args.deliverable names no file')
  const date = typeof a.date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(a.date) ? a.date : ''
  return { project, deliverable, name, date }
}

const A = readArgs(args)
const DELIVERABLE = A.project + '/' + A.deliverable
const REVIEW = A.project + '/reviews/' + A.name
const CONTRACT = REVIEW + '/contract.md'
const CLAIMS = REVIEW + '/claims.tsv'
const TARGET = REVIEW + '/lenses/request-check.json'

const RULES = [
  'Rules for this task:',
  '- Open only the three files named above. No other file, no web page, no knowledge base, no other agent result.',
  '- Write codes only (a customer is CUST-XXXX, a project tcp-xxxx). Never write the name of a customer, a partner or a person into a result.',
  '- Quote the deliverable with at most five words at a time.',
  '- Cite a file by its path below its folder (docs mirror: <repo>/<path>, project: evidence/<file>), never with the home folder in front: the commit gate refuses home paths.',
  '- Plain English. No em-dash. No comma before "and" or "or".',
].join('\n')

const CHECK_SCHEMA = {
  type: 'object',
  properties: {
    questions: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          question: { type: 'string' },
          answered: { type: 'boolean' },
          line: { type: 'integer' },
          note: { type: 'string' },
        },
        required: ['question', 'answered'],
      },
    },
    extra: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          line: { type: 'integer' },
          quote: { type: 'string' },
          summary: { type: 'string' },
        },
        required: ['summary'],
      },
    },
    unlisted: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          line: { type: 'integer' },
          quote: { type: 'string' },
          summary: { type: 'string' },
        },
        required: ['summary'],
      },
    },
    overclaims: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          claim: { type: 'string' },
          line: { type: 'integer' },
          grade: { type: 'string' },
          quote: { type: 'string' },
          summary: { type: 'string' },
          replacement: { type: 'string' },
        },
        required: ['summary'],
      },
    },
  },
  required: ['questions', 'extra', 'overclaims'],
}

const WRITTEN_SCHEMA = {
  type: 'object',
  properties: {
    path: { type: 'string' },
    same: { type: 'boolean' },
  },
  required: ['path', 'same'],
}

function checkPrompt() {
  return [
    'You are the last check before a text leaves. You see only what was asked and what is about to be sent.',
    'Read the contract ' + CONTRACT + ' (the request as given, the reader, the questions to answer, what is out of scope), the claim list ' + CLAIMS + ' (tab separated: id, line, kind, risk, sentence, evidence, grade, verdict) and the text ' + DELIVERABLE + '.',
    'Answer three things:',
    '1. questions: every question of the section "Questions to answer" and every question the request itself asks. For each say whether the text answers it and on which line.',
    '2. extra: content nobody asked for. A diagram block, a section or a statement that the request and the questions do not call for, a review note or a record of checks inside the text.',
    '3. overclaims: a row of the claim list stated in the text more firmly than its grade and evidence allow: grade said or assumed stated as a fact, a row without evidence stated as certain, a negative whose row has no evidence. Judge only sentences that have a row here.',
    '4. unlisted: a statement of fact in the text that has no row in the claim list. Name it so that the author adds a row; it is not an overclaim.',
    'Give line numbers. Leave a list empty when there is nothing. Do not invent findings to fill a list.',
    '',
    RULES,
  ].join('\n')
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

function lineOf(x) {
  return x && Number.isInteger(x.line) && x.line > 0 ? x.line : 0
}

function toLens(res) {
  const findings = []
  const questions = []
  if (!res) {
    findings.push({ class: 'OTHER', severity: 'blocking', summary: 'the request check returned nothing; run it again' })
  } else {
    for (const q of res.questions || []) {
      const row = { question: String(q.question || ''), answered: q.answered === true, line: lineOf(q), note: String(q.note || '') }
      questions.push(row)
      if (!row.answered) {
        findings.push({ line: row.line, class: 'MISREAD', severity: 'blocking', summary: 'not answered: ' + row.question })
      }
    }
    if (!questions.length) {
      findings.push({ class: 'MISREAD', severity: 'blocking', summary: 'the contract names no question to answer: fill the questions and run the check again' })
    }
    for (const x of res.overclaims || []) {
      findings.push({
        line: lineOf(x),
        claim: String(x.claim || ''),
        class: 'MISLEADING',
        severity: 'blocking',
        summary: String(x.summary || ''),
        quote: String(x.quote || ''),
        replacement: String(x.replacement || ''),
        grade: String(x.grade || ''),
      })
    }
    for (const x of res.unlisted || []) {
      findings.push({ line: lineOf(x), class: 'UNSOURCED', severity: 'major', summary: 'no row in the claim list: ' + String(x.summary || ''), quote: String(x.quote || '') })
    }
    for (const x of res.extra || []) {
      findings.push({ line: lineOf(x), class: 'EXTRA', severity: 'major', summary: String(x.summary || ''), quote: String(x.quote || '') })
    }
  }
  return {
    lens: 'request-check',
    kind: 'request',
    platform: 'request',
    deliverable: A.deliverable,
    date: A.date,
    dropped: 0,
    questions: questions,
    findings: findings.map((f, i) => Object.assign({ id: 'request-' + (i + 1), line: 0, claim: '', source: 'the contract and the claim list', outcome: OUTCOME_OPEN, blocking: f.severity === 'blocking' }, f)),
  }
}

phase('Request check')
const res = await agent(checkPrompt(), { label: 'request check', phase: 'Request check', schema: CHECK_SCHEMA })
const lens = toLens(res)

phase('Record')
const file = { path: TARGET, content: JSON.stringify(lens, null, 2) + '\n' }
const written = await agent(recordPrompt(file), { label: 'write request-check.json', phase: 'Record', schema: WRITTEN_SCHEMA, effort: 'low' })
const ok = Boolean(written && written.same === true)
if (!ok) log('request-check.json was not written or is not the same; run the Record phase again')
const open = lens.findings.filter((f) => f.blocking).length
log(open + ' blocking findings from the request check')
return {
  file: 'reviews/' + A.name + '/lenses/request-check.json',
  written: ok,
  questions: lens.questions.length,
  unanswered: lens.questions.filter((q) => !q.answered).length,
  findings: lens.findings.length,
  blocking: open,
  next: open ? 'fix the text, set confirmed-fixed or softened, then run awb review pass' : 'run awb review pass',
}
