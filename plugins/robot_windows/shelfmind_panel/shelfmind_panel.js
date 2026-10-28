/* ShelfMind control panel (Webots robot window).
 * Talks to the stockroom_brain controller over the Webots robot-window WebSocket.
 *   controller -> panel:  "state:<url-encoded JSON>"  and  "image:<base64 jpeg>"
 *   panel -> controller:  "cmd:<name>:<url-encoded argument>"
 * Open with ?demo=1 to see the layout with sample data, without Webots. */

'use strict';

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const robotName = params.get('name') || 'gantry_robot';
const DEMO = params.get('demo') === '1';

const EXAMPLES = [
  'Restock the shelves. Heavy boxes go on the low shelf and light boxes go on the high shelf. ' +
    'On each shelf, order them left to right: red, green, blue.',
  'Put every box back on the pickup table.',
  'On both shelves, swap the red box and the blue box.',
  'Stack the light red box on top of the heavy red box.',
  'Clear the low shelf: put its boxes in the dispatch bay.',
  'Reverse the order of the low shelf: left to right blue, green, red.',
  'The cleaners are coming: clear the high shelf and put its boxes in the dispatch bay.',
  'Send all the heavy boxes to the dispatch bay.',
];

const COLOURS = { red: '#c0392b', green: '#2e8b3a', blue: '#2f5bd3', yellow: '#c9a227',
                  glass: '#bfe3ff', parts: '#ff7a1a' };

// ---------------------------------------------------------------- link
class Link {
  constructor(onMessage) {
    this.onMessage = onMessage;
    this.pending = [];
    this.socket = null;
    this.lastData = 0;
    if (!DEMO) {
      this.connect();
      // Keep the link alive: re-register while no data arrives, reconnect if the socket closes.
      setInterval(() => this.watchdog(), 2000);
    }
  }

  watchdog() {
    if (!this.socket || this.socket.readyState === 3) {
      this.connect();
      return;
    }
    const silent = Date.now() - this.lastData > 3000;
    $('conn').classList.toggle('waiting', this.socket.readyState === 1 && silent);
    if (this.socket.readyState === 1 && silent) {
      this.socket.send('robot:' + robotName + ':init robot window');
      $('brain').textContent = 'Connected to Webots, waiting for the robot... (is the simulation ' +
        'running? If this stays, quit Webots with Cmd+Q and open the world again)';
    }
  }

  connect() {
    let base = location.href;
    const marker = base.includes('/~WEBOTS_HOME/') ? '/~WEBOTS_HOME/' : '/robot_windows/';
    base = base.substring(0, base.indexOf(marker) + 1);
    this.socket = new WebSocket(base.replace('https://', 'wss://').replace('http://', 'ws://'));
    this.socket.onopen = () => {
      $('conn').classList.add('on');
      while (this.pending.length)
        this.socket.send(this.pending.shift());
    };
    this.socket.onclose = () => $('conn').classList.remove('on');
    this.socket.onmessage = (event) => {
      for (const line of String(event.data).split(/\r?\n/)) {
        if (!line.startsWith('robot:'))
          continue;
        let message, robot;
        try {
          const obj = JSON.parse(line.substring(6));
          message = obj.message;
          robot = obj.name;
        } catch (e) { // same fallback as Webots' own RobotWindow.js
          const m = line.match('"message":"(.*)","name":"(.*)"}');
          if (!m)
            continue;
          message = m[1].replace(/\\/g, '');
          robot = m[2];
        }
        if (robot === robotName) {
          this.lastData = Date.now();
          this.onMessage(message);
        }
      }
    };
    this.send('init robot window');
  }

  send(message) {
    const text = 'robot:' + robotName + ':' + message;
    if (DEMO) {
      console.log('demo send:', message);
      return;
    }
    if (this.socket && this.socket.readyState === 1)
      this.socket.send(text);
    else
      this.pending.push(text);
  }

  command(name, arg = '') {
    this.send('cmd:' + name + ':' + encodeURIComponent(arg));
  }
}

// --------------------------------------------------------------- helpers
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class')
      node.className = v;
    else if (k === 'text')
      node.textContent = v;
    else if (k.startsWith('on'))
      node.addEventListener(k.substring(2), v);
    else
      node.setAttribute(k, v);
  }
  for (const c of children)
    node.append(c);
  return node;
}

function replace(parent, nodes) {
  parent.replaceChildren(...nodes);
}

// ----------------------------------------------------------------- state
let state = null;
let taskEdited = false;
const link = new Link(receive);

function receive(message) {
  if (message.startsWith('state:')) {
    try {
      state = JSON.parse(decodeURIComponent(message.substring(6)));
    } catch (e) {
      console.log('bad state message', e);
      return;
    }
    render();
  } else if (message.startsWith('image:')) {
    $('camera').src = 'data:image/jpeg;base64,' + message.substring(6);
    $('camera-card').classList.remove('hidden');
  }
}

function render() {
  const s = state;
  $('brain').textContent = s.uses_nemotron ? s.brain : s.brain + ' - add a Nebius key for Nemotron';
  $('simtime').textContent = s.sim_time.toFixed(1) + ' s';

  const model = $('model');
  const options = s.uses_nemotron ? s.models : ['backup planner'];
  if (model.options.length !== options.length || [...model.options].some((o, i) => o.value !== options[i]))
    replace(model, options.map((m) => el('option', { value: m, text: m.split('/').pop() })));
  model.value = s.uses_nemotron ? s.model : 'backup planner';
  model.disabled = !s.uses_nemotron || s.models.length < 2;

  if (!taskEdited && document.activeElement !== $('task'))
    $('task').value = s.task_file || s.task;
  $('current-task').textContent = 'Current job: ' + s.task;
  $('run').disabled = s.busy;
  $('status').textContent = s.status || '-';
  $('reasoning').textContent = s.reasoning || '';

  $('question-card').classList.toggle('hidden', !s.question);
  $('question').textContent = s.question || '';

  // plan
  const marks = { todo: '○', doing: '▶', done: '✓', failed: '✗' };
  const done = s.plan.filter((p) => p.state === 'done').length;
  $('plan-count').textContent = s.plan.length ? done + ' of ' + s.plan.length + ' done' : '';
  replace($('plan'), s.plan.length ? s.plan.map((p) => el('li', { class: p.state },
    el('span', { class: 'mark', text: marks[p.state] }),
    ...(s.duo ? [el('span', { class: 'robot ' + p.robot, text: p.robot })] : []),
    el('span', { text: p.text }))) : [el('li', { class: 'muted', text: 'No plan yet.' })]);

  // orders
  replace($('orders'), s.orders.length ? s.orders.map((o, i) => el('div', { class: 'order' },
    el('div', { class: 'what' },
      el('div', { class: 'id', text: o.id + ' (' + o.type + ')' }),
      el('div', { class: 'lines', text: o.lines.map((l) => l.item + ' → ' + l.to).join(', ') })),
    el('button', { class: 'btn', text: 'Run order', onclick: () => link.command('order', String(i)) })))
    : [el('span', { class: 'muted', text: 'No orders in the orders/ folder.' })]);
  for (const b of $('orders').querySelectorAll('button'))
    b.disabled = s.busy;

  // tiles
  const st = s.stats;
  const finished = st.done + st.gave_up;
  $('t-tasks').textContent = st.tasks;
  $('t-success').textContent = finished ? Math.round(100 * st.done / finished) + '%' : '-';
  $('t-attempts').textContent = finished ? (st.attempts / finished).toFixed(2) : '-';
  $('t-recov').textContent = st.recoveries;
  $('t-latency').textContent = st.nemo_calls ? (st.latency / st.nemo_calls).toFixed(1) + ' s' : '-';
  $('t-tokens').textContent = st.tokens.toLocaleString();

  // toggles and modes
  $('recovery').checked = s.recovery;
  $('perception').checked = s.perception === 'camera';
  $('perception').disabled = !s.camera;
  $('modes').textContent = 'Grip: ' + (s.grip === 'vacuum' ? 'vacuum gripper (real physics)' : 'lock') +
    (s.bin ? '  |  parts bin lid: ' + s.lid : '') + (s.duo ? '  |  two robots' : '');
  for (const b of document.querySelectorAll('[data-cmd]')) {
    const c = b.dataset.cmd;
    b.disabled = (s.busy && ['scatter', 'reset', 'vision', 'benchmark', 'shift'].includes(c)) ||
      (c === 'shift' && s.shift && s.shift.running) ||
      (c === 'vision' && !s.camera);
  }

  // robots and boxes
  const pills = s.robots.map((r) => el('span', { class: 'robot-pill',
    text: 'Robot ' + r.name + ': ' + (r.holding ? 'holding ' + r.holding : 'empty') +
      (r.waiting ? ' (waiting)' : '') }));
  if (s.inspector)
    pills.push(el('span', { class: 'robot-pill inspector', text: '\u{1F916} Inspector: ' + s.inspector.status +
      ' \u00B7 ' + s.inspector.reports + ' report' + (s.inspector.reports === 1 ? '' : 's') + ' \u00B7 ' +
      s.inspector.last }));
  replace($('robots'), pills);
  $('inspector-btn').classList.toggle('hidden', !s.inspector);
  replace($('boxes'), s.boxes.map((b) => el('tr', { class: b.broken ? 'broken' : '' },
    el('td', {}, el('span', { class: 'swatch', style: 'background:' + (COLOURS[b.colour] || '#888') }),
      b.name + (b.broken ? ' (broken)' : '')),
    el('td', { text: b.class }),
    el('td', { text: b.location }))));

  // full shift
  $('shift-card').classList.toggle('hidden', !s.shift);
  if (s.shift) {
    const sh = s.shift;
    $('shift-sum').textContent = sh.running ? 'step ' + Math.min(sh.done + 1, sh.total) + ' of ' + sh.total +
      ' running' : sh.passed + ' of ' + sh.total + ' steps passed' +
      (sh.sim_minutes ? ' in ' + sh.sim_minutes + ' sim minutes' : '');
    $('shift-bar').style.width = (100 * sh.done / sh.total) + '%';
    replace($('shift'), sh.results.map((r) => el('li', { class: r.passed ? 'pass' : 'fail' },
      el('span', { class: 'mark', text: r.passed ? '\u2713' : '\u2717' }),
      el('span', { class: 'say', text: r.say }),
      el('span', { class: 'note', text: r.note }))));
  }

  // benchmark
  $('bench-card').classList.toggle('hidden', !s.bench);
  if (s.bench) {
    $('bench-sum').textContent = s.bench.passed + ' of ' + s.bench.done + ' passed (' +
      s.bench.done + '/' + s.bench.total + ' run, ' + s.bench.model.split('/').pop() + ')';
    $('bench-bar').style.width = (100 * s.bench.done / s.bench.total) + '%';
    replace($('bench'), s.bench.results.map((r) => el('li', { class: r.passed ? 'pass' : 'fail',
      text: (r.passed ? 'PASS ' : 'FAIL ') + r.id + ' (' + r.outcome + ', ' + r.attempts + ' attempt' +
        (r.attempts === 1 ? '' : 's') + ')' })));
  }

  replace($('lessons'), s.lessons.length ? s.lessons.map((l) => el('li', { text: l }))
    : [el('li', { class: 'muted', text: 'No lessons yet.' })]);
  replace($('events'), s.events.slice().reverse().map((e) => el('li', { text: e })));
}

// ------------------------------------------------------------- controls
$('run').addEventListener('click', () => {
  const text = $('task').value.trim();
  if (!text)
    return;
  taskEdited = false;
  link.command('run', text);
});
$('task').addEventListener('input', () => { taskEdited = true; });
$('task').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey))
    $('run').click();
});
$('send-answer').addEventListener('click', () => {
  const text = $('answer').value.trim();
  if (text) {
    link.command('answer', text);
    $('answer').value = '';
  }
});
$('answer').addEventListener('keydown', (e) => {
  if (e.key === 'Enter')
    $('send-answer').click();
});
for (const b of document.querySelectorAll('[data-cmd]'))
  b.addEventListener('click', () => link.command(b.dataset.cmd));
$('recovery').addEventListener('change', () => link.command('recovery'));
$('perception').addEventListener('change', () => link.command('perception'));
$('model').addEventListener('change', () => link.command('model', $('model').value));
$('forget').addEventListener('click', () => link.command('forget'));
replace($('examples'), EXAMPLES.map((t) => el('button', { class: 'chip', text: t.length > 48 ? t.slice(0, 46) + '...' : t,
  title: t, onclick: () => { $('task').value = t; taskEdited = true; } })));

// ----------------------------------------------------------------- voice
const Speech = window.SpeechRecognition || window.webkitSpeechRecognition;
if (!Speech) {
  $('mic').disabled = true;
  $('mic-note').textContent = 'Voice needs Chrome, Edge or Safari.';
} else {
  const rec = new Speech();
  rec.lang = 'en-US';
  rec.interimResults = true;
  let listening = false;
  rec.onresult = (e) => {
    const text = [...e.results].map((r) => r[0].transcript).join(' ');
    $('task').value = text;
    taskEdited = true;
  };
  rec.onend = () => {
    listening = false;
    $('mic').classList.remove('listening');
    $('mic-note').textContent = $('task').value ? 'Check the text, then press Run job.' : '';
  };
  rec.onerror = (e) => { $('mic-note').textContent = 'Voice error: ' + e.error; };
  $('mic').addEventListener('click', () => {
    if (listening) {
      rec.stop();
      return;
    }
    listening = true;
    $('mic').classList.add('listening');
    $('mic-note').textContent = 'Listening... speak the job.';
    rec.start();
  });
}

// ------------------------------------------------------------------ demo
if (DEMO) {
  receive('state:' + encodeURIComponent(JSON.stringify({
    brain: 'NVIDIA Nemotron via Nebius Token Factory', uses_nemotron: true,
    model: 'nvidia/nemotron-3-super-120b-a12b',
    models: ['nvidia/nemotron-3-super-120b-a12b', 'nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B'],
    task: EXAMPLES[0], status: 'Placing blue_box_1 on high_shelf_3',
    reasoning: 'Nemotron: Heavy boxes go to the low shelf and light boxes to the high shelf, red, green, blue.',
    plan: [
      { text: 'pick red_box_1', robot: 'A', state: 'done' },
      { text: 'place on low_shelf_1', robot: 'A', state: 'done' },
      { text: 'pick blue_box_1', robot: 'A', state: 'done' },
      { text: 'place on high_shelf_3', robot: 'A', state: 'doing' },
      { text: 'pick green_box_1', robot: 'A', state: 'todo' },
      { text: 'place on low_shelf_2', robot: 'A', state: 'todo' },
    ],
    question: null, busy: true, recovery: true, perception: 'simulator', grip: 'lock', duo: false,
    camera: true, bin: false, lid: 'closed',
    robots: [{ name: 'A', holding: 'blue_box_1', x: 0.31, waiting: false }],
    boxes: [
      { name: 'red_box_1', location: 'low_shelf_1', class: 'heavy', colour: 'red', broken: false },
      { name: 'blue_box_1', location: 'gripper', class: 'light', colour: 'blue', broken: false },
      { name: 'green_box_1', location: 'table_3', class: 'heavy', colour: 'green', broken: false },
    ],
    stats: { tasks: 3, done: 3, gave_up: 0, attempts: 4, recoveries: 2, nemo_calls: 4, latency: 21.6, tokens: 9120 },
    events: ['  12.1s  drop: blue_box_1 landed at floor', '  47.9s  knock: red_box_1 off low_shelf_1'],
    orders: [{ id: 'SO-1042', type: 'outbound', lines: [{ item: 'red_box_2', to: 'dispatch' }] }],
    lessons: ['Light boxes can slip when carried fast; re-check them after a move.'],
    bench: { total: 10, done: 4, passed: 3, model: 'nvidia/nemotron-3-super-120b-a12b',
             results: [{ id: 'restock_from_table', passed: true, outcome: 'done', attempts: 1 },
                       { id: 'swap_red_blue', passed: false, outcome: 'gave_up', attempts: 4 }] },
    shift: { name: 'Full shift', total: 12, done: 4, passed: 4, running: true, results: [
      { say: 'Morning restock (a box slips on the way)', passed: true, note: 'done in 2 attempts (recovered from a grip failure)' },
      { say: 'Someone bumps the low shelf', passed: true, note: 'found by the humanoid inspector and repaired' },
      { say: 'Outbound order SO-1042', passed: true, note: 'done in 1 attempt' },
      { say: 'Customer returns SO-1043', passed: true, note: 'done in 1 attempt' }] },
    inspector: { on: true, status: 'patrolling (lap 2)', reports: 1, last: 'ALERT! green_box_1 is lying on the floor near the floor gap' },
    sim_time: 48.2,
  })));
}
