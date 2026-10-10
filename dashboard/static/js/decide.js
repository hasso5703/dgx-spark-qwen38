"use strict";
/* Decide: System One from a browser. A situation, typed questions about it, and each
   answer drawn as the distribution it is. The serving key never reaches this page: the
   cockpit holds it and forwards the call. */

const S1_EXAMPLES = {
  'Support ticket': {
    state: 'Hi, I have been trying to connect my Stripe account for three days and it keeps failing.\nI am losing sales every hour. Please help as soon as you can.',
    questions: [
      {id: 'department', type: 'choice', instructions: 'Which team should handle this',
       criteria: [['billing', 'Payment or subscription issues'], ['technical', 'Bugs or integration problems'], ['sales', 'Pricing or account questions']]},
      {id: 'frustration', type: 'score', instructions: 'How frustrated the customer appears',
       levels: ['Calm, just stating facts', 'Frustrated but civil', 'Very angry, strong language']},
      {id: 'is_urgent', type: 'noul', instructions: 'The message conveys urgency or time-sensitivity'}]},
  'Routing an agent': {
    state: 'User asked: "read the last 200 lines of the proxy log and tell me why the stream cut"',
    questions: [
      {id: 'tool', type: 'choice', instructions: 'Which tool this turn needs first',
       criteria: [['read_file', 'open a file on disk'], ['run_command', 'execute something and read its output'], ['answer', 'no tool needed, answer from what is known']]},
      {id: 'needs_root', type: 'noul', instructions: 'Carrying this out requires root'}]},
  'Moderation': {
    state: 'Comment posted on the forum: "honestly this release is garbage and whoever shipped it should be fired"',
    questions: [
      {id: 'severity', type: 'score', instructions: 'How severe this is as a policy matter',
       levels: ['Harmless opinion', 'Rude but allowed', 'Personal attack', 'Requires removal']},
      {id: 'is_attack', type: 'noul', instructions: 'This targets a person rather than the work'}]},
  'Extraction': {
    state: 'Invoice 2026-0417, dated 12 September 2026, from Maurienne AI SARL, total 4,820.00 EUR, payable within 30 days, marked OVERDUE.',
    questions: [
      {id: 'currency', type: 'choice', instructions: 'The currency of the total',
       criteria: [['EUR', 'euro'], ['USD', 'US dollar'], ['GBP', 'pound sterling'], ['other', 'anything else']]},
      {id: 'overdue', type: 'noul', instructions: 'The invoice is past its due date'}]},
  'Eight options': {
    state: 'The engine log ends with: "RuntimeError: selected index k out of range" inside the sampler, then the scheduler exits.',
    questions: [
      {id: 'cause', type: 'choice', instructions: 'The most likely cause',
       criteria: [['bad_request', 'a request field the engine does not bound'], ['oom', 'out of memory'], ['driver', 'a GPU driver fault'],
                  ['disk', 'a full disk'], ['network', 'a network failure'], ['config', 'a misconfiguration at start-up'],
                  ['model', 'a corrupt checkpoint'], ['upstream_bug', 'a known upstream defect']]}]}};
const S1_TYPE = {noul: 'Yes or no', choice: 'Choice', score: 'Score'};
let s1Questions = [], s1Available = null, s1Running = false, s1LaneKey = null, s1Loaded = null;

function s1Input(value, placeholder, label, onInput){
  const i = el('input', 'input'); i.type = 'text'; i.value = value; i.placeholder = placeholder; i.setAttribute('aria-label', label);
  i.addEventListener('input', () => { onInput(i.value); s1Curl(); }); return i;
}
function s1Render(){
  const box = $('s1-questions'); clear(box);
  s1Questions.forEach((q, i) => {
    const card = el('div', 'qcard'); const top = el('div', 'qtop');
    const kind = el('span', 'tag gold', S1_TYPE[q.type] || q.type);
    const id = s1Input(q.id, 'question id', 'Question id', v => { q.id = v; }); id.classList.add('code');
    const del = el('button', 'btn sm ghost', 'Remove'); del.type = 'button';
    del.addEventListener('click', () => { s1Questions.splice(i, 1); s1Render(); s1Curl(); });
    top.append(kind, id, del);
    card.append(top, s1Input(q.instructions, 'what to judge', 'What to judge', v => { q.instructions = v; }));
    if (q.type === 'choice' || q.type === 'score'){
      const crit = el('div', 'crit');
      const rows = q.type === 'choice' ? q.criteria : q.levels.map(l => [null, l]);
      rows.forEach((row, j) => {
        const r = el('div', 'row');
        if (q.type === 'choice') r.append(s1Input(row[0], 'option', 'Option name', v => { q.criteria[j][0] = v; }));
        r.append(s1Input(row[1] || '', q.type === 'choice' ? 'what it means' : 'this level', q.type === 'choice' ? 'Option description' : 'Level',
          v => { if (q.type === 'choice') q.criteria[j][1] = v; else q.levels[j] = v; }));
        const x = el('button', 'btn sm ghost', '×'); x.type = 'button'; x.setAttribute('aria-label', 'Remove this ' + (q.type === 'choice' ? 'option' : 'level'));
        x.addEventListener('click', () => { if (q.type === 'choice') q.criteria.splice(j, 1); else q.levels.splice(j, 1); s1Render(); s1Curl(); });
        r.append(x); crit.append(r);
      });
      const add = el('button', 'btn sm ghost', q.type === 'choice' ? 'Add an option' : 'Add a level'); add.type = 'button'; add.style.alignSelf = 'flex-start';
      add.addEventListener('click', () => { if (q.type === 'choice') q.criteria.push(['option' + (q.criteria.length + 1), '']); else q.levels.push('level ' + (q.levels.length + 1)); s1Render(); s1Curl(); });
      crit.append(add); card.append(crit);
    }
    box.append(card);
  });
  if (!s1Questions.length) box.append(el('p', 'faint', 'No question yet: add a yes or no, a choice or a score.'));
  s1Sync();
}
function s1Payload(){
  const questions = {};
  s1Questions.forEach(q => {
    const id = (q.id || '').trim(); if (!id) return;
    const out = {type: q.type, instructions: q.instructions || ''};
    if (q.type === 'choice'){ out.criteria = {}; q.criteria.forEach(([n, d]) => { if ((n || '').trim()) out.criteria[n.trim()] = d || null; }); }
    else if (q.type === 'score') out.criteria = q.levels.filter(l => (l || '').trim());
    questions[id] = out;
  });
  return {state: $('s1-state').value, model: 'jev-latest', questions};
}
function s1Curl(){
  const body = JSON.stringify(s1Payload(), null, 2).split('\n').map((l, i) => i ? '  ' + l : l).join('\n');
  // the key reaches curl on its standard input (-H @-), never as an argument: any local
  // process reads a command line in /proc; printf is a builtin of sh, bash, zsh and fish
  setText('s1-curl', "printf 'Authorization: Bearer %s\\n' \"$(cat ~/.config/qwen38/api-key)\" | "
    + "curl -s http://127.0.0.1:30001/v1/systemone \\\n"
    + "  -H @- -H 'Content-Type: application/json' -d " + shq(body));
  s1Sync();
}
function s1Problem(){
  const p = s1Payload();
  if (!p.state.trim()) return 'Write the situation first: that is what the questions are asked about.';
  if (!Object.keys(p.questions).length) return 'Add at least one question.';
  const ids = s1Questions.map(q => (q.id || '').trim()).filter(Boolean);
  const twice = ids.find((id, i) => ids.indexOf(id) !== i);
  if (twice) return `Two questions are named "${twice}": answers are keyed by name, so rename one.`;
  return '';
}
function s1Sync(){
  const b = $('s1-run'); if (!b) return;
  const why = s1Available === false ? 'System One is not served right now: it needs a text lane.' : s1Running ? '' : s1Problem();
  b.disabled = s1Available === false || s1Running || !!s1Problem();
  show('s1-why', !!why && !s1Running); setText('s1-why', why);
}
function s1Load(name){
  const ex = S1_EXAMPLES[name]; if (!ex) return;
  $('s1-state').value = ex.state; s1Questions = JSON.parse(JSON.stringify(ex.questions)); s1Loaded = name;
  document.querySelectorAll('#s1-examples button').forEach(b => b.setAttribute('aria-pressed', String(b.textContent === name)));
  s1Render(); s1Curl();
}
function s1Answer(id, ans){
  const wrap = el('div', 'answer'); wrap.append(el('h4', null, id));
  const verdict = el('div', 'verdict'); let rows = [];
  if (ans.type === 'noul'){ const p = ans.noul; verdict.textContent = p >= 0.5 ? 'Yes' : 'No'; verdict.append(el('small', null, (p * 100).toFixed(1) + ' % yes')); rows = [['yes', p], ['no', 1 - p]]; }
  else if (ans.type === 'choice'){ verdict.textContent = ans.choice; verdict.append(el('small', null, 'confidence ' + (ans.confidence * 100).toFixed(0) + ' %')); rows = Object.entries(ans.probabilities || {}); }
  else { const lg = ans.legend || {}, n = Object.keys(lg).length; verdict.textContent = ans.score.toFixed(2) + ' of ' + Math.max(0, n - 1);
    verdict.append(el('small', null, (lg[String(Math.round(ans.score))] || '') + ', confidence ' + (ans.confidence * 100).toFixed(0) + ' %'));
    rows = Object.entries(ans.probabilities || {}).map(([k, v]) => [lg[k] || k, v]); }
  wrap.append(verdict);
  const top = Math.max(...rows.map(r => r[1]), 0);
  rows.forEach(([name, p]) => {
    const r = el('div', 'prob' + (p >= top && top > 0 ? ' top' : ''));
    r.append(el('span', 'nm', name === '' ? '(empty name)' : name));
    const bar = el('span', 'bar'); const i = el('i'); i.style.width = (p * 100).toFixed(2) + '%'; bar.append(i); r.append(bar);
    r.append(el('span', 'p', (p * 100).toFixed(1) + ' %')); wrap.append(r);
  });
  return wrap;
}
async function s1Run(){
  const problem = s1Problem(); if (problem) return toast(problem, 'warn');
  s1Running = true; s1Sync(); setText($('s1-run'), 'Asking…');
  const box = $('s1-answers'); clear(box); clear($('s1-meta')); setText('s1-time', '');
  try{
    const {status, ok, out} = await postJSON('/api/systemone', s1Payload());
    if (!ok){
      const why = out.refused ? JSON.stringify(out.refused.detail ?? out.refused) : (out.error || 'HTTP ' + status);
      box.append(el('p', 'err-t', `Refused with HTTP ${status}: ${why}`)); setText('s1-time', status + ' refused'); $('s1-time').className = 'tag err'; return;
    }
    const answers = (out.answer || {}).answers || {};
    Object.entries(answers).forEach(([id, a]) => box.append(s1Answer(id, a)));
    if (!Object.keys(answers).length) box.append(el('p', 'faint', 'The call was answered with no answers.'));
    setText('s1-time', out.seconds + ' s'); $('s1-time').className = 'tag gold';
    const usage = (out.answer || {}).usage || {}, h = out.headers || {};
    [['model', (out.answer || {}).model], ['input tokens', usage.input_tokens], ['output tokens', usage.output_tokens],
     ['branches', h['x-systemone-branches']], ['label mass', h['x-systemone-label-mass']], ['cached tokens', h['x-systemone-cached-tokens']]]
      .forEach(([k, v]) => { if (v !== undefined && v !== null && v !== '') $('s1-meta').append(el('span', 'tag', k + ' ' + v)); });
  }catch(e){ box.append(el('p', 'err-t', 'The cockpit could not reach the proxy: ' + e.message)); setText('s1-time', 'failed'); $('s1-time').className = 'tag err'; }
  finally{ s1Running = false; setText($('s1-run'), 'Ask'); s1Sync(); }
}
// A later probe gives Ask back once a lane answers again: a refusal used to disable it for
// the life of the page. The probe runs when the view opens and when the answering lane changes.
async function s1Probe(){
  try{
    const r = await fetch('/api/systemone'); if (r.status === 401) return login();
    const d = await r.json(); s1Available = !!d.available;
    cap('s1-cap', d.available ? `served by ${d.lane || 'the text lane'}` : 'not served here', d.available ? 'ok' : 'err');
    const note = $('s1-note'); note.hidden = !!d.available; clear(note);
    if (!d.available){
      const why = cap1(String(d.reason || 'the proxy in front of this box does not answer /v1/systemone').replace(/\.?\s*$/, '.'));
      note.style.display = 'flex'; note.style.alignItems = 'center'; note.style.gap = 'var(--s-3)'; note.style.flexWrap = 'wrap';
      const t = el('span', null, why); t.style.flex = '1'; t.style.minWidth = '240px';
      const b = el('button', 'btn primary sm', 'Load flash 176B'); b.type = 'button'; b.addEventListener('click', () => askJourney('flash'));
      note.append(t, b);
    }
    badge('decide', d.available ? '' : 'off', '');
  }catch{ cap('s1-cap', 'unknown', 'warn'); }
  s1Sync();
}
on('lifecycle', () => {
  const s = servingEngine(), k = s ? s[0] + ':' + s[1].state : '';
  if (k !== s1LaneKey){ s1LaneKey = k; if (activeView === 'decide') s1Probe(); }
});
onShow('decide', s1Probe);
function wireDecide(){
  Object.keys(S1_EXAMPLES).forEach(name => { const b = el('button', null, name); b.type = 'button'; b.setAttribute('aria-pressed', 'false'); b.addEventListener('click', () => s1Load(name)); $('s1-examples').append(b); });
  document.querySelectorAll('[data-addq]').forEach(b => b.addEventListener('click', () => {
    const t = b.dataset.addq, used = new Set(s1Questions.map(x => (x.id || '').trim()));
    let k = 1; while (used.has(t + k)) k++;     // a free id: the length plus one can be taken after a removal
    const q = {id: t + k, type: t, instructions: ''};
    if (t === 'choice') q.criteria = [['yes', ''], ['no', '']];
    if (t === 'score') q.levels = ['low', 'high'];
    s1Questions.push(q); s1Render(); s1Curl();
  }));
  $('s1-run').addEventListener('click', s1Run);
  $('s1-state').addEventListener('input', s1Curl);
  $('s1-copy').addEventListener('click', () => copyText($('s1-curl').textContent));
  s1Load(Object.keys(S1_EXAMPLES)[0]);
}
