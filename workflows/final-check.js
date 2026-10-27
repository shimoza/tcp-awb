export const meta = {
  name: 'awb-final-check',
  description: 'The final pass of a high-stakes deliverable on his phrase: one strong agent reads the request, the project files, the evidence and the review record, writes the answer again from the request into deliverables/<stem>.final.md and the table into reviews/<name>/final-check.md; the run is recorded in reviews/<name>/final-check.json',
  whenToUse: 'Only on his phrase "final check" or "финальная вычитка", after he said yes to the cost. Never on its own and never as part of another step. Args: {project: absolute project folder, deliverable: deliverables/<name> or absolute, date: "YYYY-MM-DD"}',
  phases: [
    { title: 'Final check', detail: 'one agent on Fable 5.1 at effort max: request, caveats, the answer again from the request, the table', model: 'fable' },
    { title: 'Record', detail: 'write reviews/<name>/final-check.json' },
  ],
}

// The final check of the Architect Workbench (FX, 2026-10-27).
//
// A one-off run on Fable 5.1 at effort max over a project's files found exactly the known failures of the review
// pipeline: caveats of the research lost, a customer request dropped by contract.md, claims stronger than the
// evidence, answers not in the customer's order, noodle text. This workflow makes that run a step he starts by
// his phrase.
//
// Input (args): project is the absolute path of the project folder, deliverable is the path of the draft relative
// to the project (deliverables/<name>) or absolute, date is the ISO date stamped into the record.
//
// Output:
//   <project>/deliverables/<stem>.final.md     the final text only (stem: the name without its last suffix)
//   <project>/reviews/<name>/final-check.md    the table: questions, caveats, cuts, unsupported claims, contract
//                                              gaps, bytes, open points
//   <project>/reviews/<name>/final-check.json  the record of the run that `awb review show` lists: {kind:
//                                              "final-check", deliverable, date, model, effort, final, table,
//                                              draft_bytes, final_bytes, questions, dropped, invented, unsupported,
//                                              open_points, final_sha256}
// The record lies beside the review record, not in lenses/: it binds nothing of the draft and a put never asks for
// it (task GT). The final text is a new file of deliverables/. RV2 (his decision of 2026-10-27): final_sha256 is
// the hash of the final text as the agent wrote it; while the text on disk has that hash, the final check stands in
// for the request check and the blind verification, so its review runs workflows/review.js with after_fx: true
// (the platform lenses and the partner lens). After an edit or without a hash the full review applies.

const MODEL = 'fable'          // Fable 5.1, pinned: the run that found the failures was on this model
const EFFORT = 'max'
const MODEL_LABEL = 'Fable 5.1'

function fail(message) {
  throw new Error('awb-final-check: ' + message)
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
  if (/\.final\.md$/.test(name)) fail('args.deliverable is already a final text; name the draft')
  const date = typeof a.date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(a.date) ? a.date : ''
  const base = name.split('/').pop()
  const dot = base.lastIndexOf('.')
  const stem = name.slice(0, name.length - base.length) + (dot > 0 ? base.slice(0, dot) : base)
  return { project, deliverable, name, date, final: 'deliverables/' + stem + '.final.md' }
}

const A = readArgs(args)
const P = A.project
const DRAFT = P + '/' + A.deliverable
const FINAL = P + '/' + A.final
const REVIEW = P + '/reviews/' + A.name
const TABLE = REVIEW + '/final-check.md'
const RECORD = REVIEW + '/final-check.json'

const RESULT_SCHEMA = {
  type: 'object',
  properties: {
    final_written: { type: 'boolean' },
    table_written: { type: 'boolean' },
    draft_bytes: { type: 'integer' },
    final_bytes: { type: 'integer' },
    questions: { type: 'integer' },
    answered: { type: 'integer' },
    dropped_by_contract: { type: 'array', items: { type: 'string' } },
    invented_by_contract: { type: 'array', items: { type: 'string' } },
    caveats: { type: 'integer' },
    unsupported_claims: { type: 'integer' },
    open_points: { type: 'array', items: { type: 'string' } },
    final_sha256: { type: 'string' },
  },
  required: ['final_written', 'table_written', 'draft_bytes', 'final_bytes', 'questions', 'open_points'],
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
    'You make the final pass over a high-stakes deliverable of the project ' + P + ' before it leaves.',
    '',
    'Read these files, nothing outside the project:',
    '- ' + P + '/input/ : the customer request. It is the source of truth.',
    '- ' + P + '/SCOPE.md, ' + P + '/STATE.md, ' + P + '/OPEN.md',
    '- ' + P + '/evidence/ : what the research found and how.',
    '- ' + REVIEW + '/ : contract.md, claims.tsv, lenses/*.json, before-voice.md (each one when it is there).',
    '- the draft ' + DRAFT,
    '',
    'Do this:',
    '1. List every customer question or request verbatim from input/. Compare the list with contract.md and note any question it dropped or invented.',
    '2. Collect every caveat and limit of the research with its source: tested live and how, only from the docs, not tested, not known, failed, gaps.',
    '3. Write the answer again from the request, never by patching the draft. Answer each question in the customer\'s order. Every fact traces to evidence/ or claims.tsv. State each caveat plainly where it applies ("I tested this on our tenant", "I did not test X", "the docs say X, I did not verify it"). Nothing nobody asked for. Keep the useful concrete details.',
    '4. Style of the final text: never longer than the draft. Plain English of a Russian native architect in Germany for German non-native readers. First person singular. Short sentences, plain verbs, the answer first. No em-dash. No comma before "and" or "or". No bullet list where every line starts with a bold label. None of these words: delve, leverage, utilize, robust, seamless, tapestry, navigate, streamline, realm, intricate, comprehensive overview, garner, boast, meticulous, surpass, showcasing, myriad, furthermore, moreover, in conclusion, it\'s important to note. Write "T Cloud Public (TCP)" at the first mention, then TCP. Never the word Huawei. Codes only (CUST-XXXX, tcp-xxxx), never a real name. No promise in the author\'s name. Keep a greeting and a sign-off only when the draft has them.',
    '5. Write two files with the Write tool:',
    '   - ' + FINAL + ' : the final text only, nothing about the check.',
    '   - ' + TABLE + ' : a table of each question -> where it is answered -> verdict; a table of each caveat -> its source -> where it is kept; what was cut and why; the draft claims the evidence does not support; the questions contract.md dropped or invented; the bytes of the draft and of the final text; the points you could not decide.',
    '6. Never change any other file. Never run a command that changes anything.',
    '7. When both files are written, run sha256sum ' + FINAL + ' and return its 64 hex digits as final_sha256. Change nothing in the final text after that.',
    '',
    'Cite a file by its path inside the project (evidence/<file>), never with the home folder in front.',
    'Return the counts of what you did. open_points are the points you could not decide, one short line each, codes only.',
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

function count(x) {
  return Number.isInteger(x) && x >= 0 ? x : 0
}

function lines(x) {
  return Array.isArray(x) ? x.map((s) => String(s)).filter(Boolean) : []
}

phase('Final check')
log('final check on ' + MODEL_LABEL + ' at effort ' + EFFORT + ': ' + A.deliverable)
const res = await agent(checkPrompt(), { label: 'final check', phase: 'Final check', schema: RESULT_SCHEMA, model: MODEL, effort: EFFORT })
if (!res) fail('the final check returned nothing; run it again')
const record = {
  kind: 'final-check',
  deliverable: A.deliverable,
  date: A.date,
  model: MODEL_LABEL,
  effort: EFFORT,
  final: A.final,
  table: 'reviews/' + A.name + '/final-check.md',
  final_written: res.final_written === true,
  table_written: res.table_written === true,
  draft_bytes: count(res.draft_bytes),
  final_bytes: count(res.final_bytes),
  questions: count(res.questions),
  answered: count(res.answered),
  dropped: lines(res.dropped_by_contract),
  invented: lines(res.invented_by_contract),
  caveats: count(res.caveats),
  unsupported: count(res.unsupported_claims),
  open_points: lines(res.open_points),
  final_sha256: typeof res.final_sha256 === 'string' && /^[0-9a-f]{64}$/.test(res.final_sha256) ? res.final_sha256 : '',
}
if (!record.final_sha256) log('no hash of the final text: its review is the full review, with the request check and the blind verification')
if (record.final_bytes > record.draft_bytes) log('the final text is longer than the draft: look at it before it goes on')

phase('Record')
const file = { path: RECORD, content: JSON.stringify(record, null, 2) + '\n' }
const written = await agent(recordPrompt(file), { label: 'write final-check.json', phase: 'Record', schema: WRITTEN_SCHEMA, effort: 'low' })
const ok = Boolean(written && written.same === true)
if (!ok) log('final-check.json was not written or is not the same; run the Record phase again')
return {
  final: A.final,
  table: record.table,
  record: 'reviews/' + A.name + '/final-check.json',
  written: ok && record.final_written && record.table_written,
  model: MODEL_LABEL,
  effort: EFFORT,
  questions: record.questions,
  answered: record.answered,
  dropped: record.dropped,
  invented: record.invented,
  unsupported: record.unsupported,
  draft_bytes: record.draft_bytes,
  final_bytes: record.final_bytes,
  open_points: record.open_points,
  final_sha256: record.final_sha256,
  next: 'show him the final text, the table summary and the open points; add the open points to OPEN.md under "Final check (FX, ' + A.date + ')"' + (record.final_sha256
    ? '; review the final text with workflows/review.js and after_fx: true, then awb review pass'
    : '; the final text gets the full review'),
}
