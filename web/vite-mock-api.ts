import type { Plugin } from 'vite';
import type { AdminStats } from './src/lib/api';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ROOM_COLORS } from './src/lib/roomColors';

interface MockReq {
  url: string;
  method: string;
  body: any;
}
type MockHandler = (req: MockReq) => unknown | undefined;

const __mockDir = dirname(fileURLToPath(import.meta.url));
const IMMUNIZATION_REFS: Array<{
  name: string;
  display_name: string;
  category: string;
  schedule: string;
  interval_days: number | null;
  primary_series_doses: number | null;
  aliases: string[];
  description: string | null;
  typical_age_range: string | null;
}> = (() => {
  try {
    const path = resolve(__mockDir, '../src/istota/health/data/immunization_refs.json');
    const raw = JSON.parse(readFileSync(path, 'utf-8')) as any[];
    return raw.map((r) => ({
      name: r.name,
      display_name: r.display_name,
      category: r.category,
      schedule: r.schedule,
      interval_days: r.interval_days ?? null,
      primary_series_doses: r.primary_series_doses ?? null,
      aliases: r.aliases ?? [],
      description: r.description ?? null,
      typical_age_range: r.typical_age_range ?? null,
    }));
  } catch {
    return [];
  }
})();
const IMMUNIZATION_EXPLAINERS: Record<string, { summary: string; why_it_matters: string[] }> =
  (() => {
    try {
      const path = resolve(__mockDir, '../src/istota/health/data/immunization_explainers.json');
      const raw = JSON.parse(readFileSync(path, 'utf-8')) as any[];
      const out: Record<string, { summary: string; why_it_matters: string[] }> = {};
      for (const e of raw) {
        if (!e || typeof e.name !== 'string') continue;
        out[e.name] = {
          summary: typeof e.summary === 'string' ? e.summary : '',
          why_it_matters: Array.isArray(e.why_it_matters)
            ? e.why_it_matters.filter(
                (w: unknown): w is string => typeof w === 'string' && w.trim().length > 0,
              )
            : [],
        };
      }
      return out;
    } catch {
      return {};
    }
  })();
const BIOMARKER_REFS: Array<{
  name: string;
  display_name: string;
  category: string;
  default_unit: string;
  ref_range_low: number | null;
  ref_range_high: number | null;
  ref_range_low_m: number | null;
  ref_range_high_m: number | null;
  ref_range_low_f: number | null;
  ref_range_high_f: number | null;
  aliases: string[];
  description: string | null;
}> = (() => {
  try {
    const path = resolve(__mockDir, '../src/istota/health/data/biomarker_refs.json');
    const raw = JSON.parse(readFileSync(path, 'utf-8')) as any[];
    return raw.map((r) => ({
      name: r.name,
      display_name: r.display_name,
      category: r.category,
      default_unit: r.default_unit,
      ref_range_low: r.ref_range_low ?? null,
      ref_range_high: r.ref_range_high ?? null,
      ref_range_low_m: r.ref_range_low_m ?? null,
      ref_range_high_m: r.ref_range_high_m ?? null,
      ref_range_low_f: r.ref_range_low_f ?? null,
      ref_range_high_f: r.ref_range_high_f ?? null,
      aliases: r.aliases ?? [],
      description: r.description ?? null,
    }));
  } catch {
    return [];
  }
})();

const user = {
  username: 'carol',
  display_name: 'Carol',
  bot_name: 'Istota',
  is_admin: true,
  // How the bot is reachable outside the web UI. `email` is null on an
  // instance with email switched off, which drops the dashboard's email tip.
  contact: {
    email: 'istota+carol@bot.example.com',
    talk: true,
  } as { email: string | null; talk: boolean },
  // Present only when the instance runs [web] token_storage = "encrypted";
  // set to null to preview the "Not connected" card.
  nextcloud_token: {
    connected: true,
    expires_at: new Date(Date.now() + 3600_000).toISOString(),
  } as { connected: boolean; expires_at: string | null } | null,
  // Content hashes for the two identities the client renders. Both start null,
  // so the dev server draws the fallback chips until an upload lands; the bot
  // one stays null until the admin routes exist.
  avatars: { user: null, bot: null } as { user: string | null; bot: string | null },
  features: {
    chat: true,
    briefings: true,
    feeds: true,
    location: true,
    money: true,
    health: true,
    google_workspace: false,
    google_workspace_enabled: false,
    admin: true,
  },
};

// ---- Web chat mock state ----
interface MockChatRoom {
  id: number;
  token: string;
  name: string;
  archived: boolean;
  created_at: string;
  updated_at: string;
  model?: string | null;
  effort?: string | null;
  origin?: string;
  color?: string | null;
}
interface MockChatTask {
  id: number;
  roomToken: string;
  prompt: string;
  createdAt: number;
  variant?: 'simple' | 'multiround' | 'table';
  /** Milliseconds to stall after `task_started`, before any output.
   *
   * The send queue is only reachable while a turn is running, and an ordinary
   * mock turn settles in a few seconds — long enough to watch, too short to
   * type a second message into by hand. A prompt containing `slow` holds the
   * turn open here instead, with Stop up and the composer live, which is the
   * state the queue exists for. */
  holdMs?: number;
  /** Attachment chip labels, persisted the way the backend persists them, so
   * a chip in dev survives leaving the room and coming back. */
  attachments?: string[];
  /** Positional workspace paths for those chips (null = not linkable). */
  attachmentPaths?: (string | null)[];
  /** Canonical msg_id this turn's question replies to, if it cites one. */
  replyToMsgId?: number;
  /** Surface the turn entered from, when it is not one the room lives on —
   * `'email'` for mail mirrored into the thread it continues. Set together with
   * `author` and `subject`, matching the server, which emits `origin` only for a
   * row whose author is an external sender. */
  origin?: string;
  subject?: string;
  author?: string;
}

/** Stored upload path → the workspace path `/chat/files` would take, or null
 * for one that isn't under the user's own workspace. */
function mockWorkspacePath(stored: string): string | null {
  return stored.startsWith('inbox/') ? `/Users/carol/${stored}` : null;
}
const mockNotifications = [
  {
    id: 91,
    source: 'confirmation',
    severity: 'warning' as const,
    actionable: true,
    title: 'email from scheduling@partner.example — Availability next week',
    body: 'An unknown sender emailed you. Nothing has been run, and the message body is not shown until you confirm.',
    link: null,
    occurrences: 1,
    created_at: new Date(Date.now() - 6 * 60_000).toISOString(),
    updated_at: new Date(Date.now() - 6 * 60_000).toISOString(),
    seen_at: null,
    object_type: 'task',
    object_id: '40122',
    actions: [
      {
        id: 'confirm',
        label: 'Confirm',
        kind: 'primary' as const,
        method: 'POST' as const,
        endpoint: '/chat/tasks/40122/confirm',
        href: null,
      },
      {
        id: 'discard',
        label: 'Discard',
        kind: 'danger' as const,
        method: 'POST' as const,
        endpoint: '/chat/tasks/40122/cancel',
        href: null,
      },
    ],
    status_note: null,
    dismissed: false,
  },
  {
    id: 92,
    source: 'cron_job',
    severity: 'danger' as const,
    actionable: true,
    title: 'Scheduled job "morning-briefing" was switched off',
    body: 'It failed five times running. The last error was: connection refused.',
    link: null,
    occurrences: 4,
    created_at: new Date(Date.now() - 4 * 24 * 3600_000).toISOString(),
    updated_at: new Date(Date.now() - 40 * 60_000).toISOString(),
    seen_at: null,
    object_type: 'scheduled_job',
    object_id: 'morning-briefing',
    actions: [
      {
        id: 'enable',
        label: 'Re-enable',
        kind: 'primary' as const,
        method: 'POST' as const,
        endpoint: '/chat/cron/morning-briefing/enable',
        href: null,
      },
    ],
    status_note: null,
    dismissed: false,
  },
  {
    id: 93,
    source: 'task_alert',
    severity: 'info' as const,
    actionable: false,
    title: 'Your emailed request could not be answered',
    body: 'The task timed out before it finished. Nothing was sent.',
    link: null,
    occurrences: 1,
    created_at: new Date(Date.now() - 2 * 3600_000).toISOString(),
    updated_at: new Date(Date.now() - 2 * 3600_000).toISOString(),
    seen_at: null,
    object_type: null,
    object_id: null,
    actions: [],
    status_note: 'Nothing to do — this is a record of something that already happened.',
    dismissed: false,
  },
];

const mockChatRooms: MockChatRoom[] = [
  {
    id: 1,
    token: 'web-carol-general',
    name: 'general',
    archived: false,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    model: 'claude-opus-4-8',
    effort: 'high',
    color: 'teal',
  },
  // A Talk-origin room, so the room-memory pane's shared notice and its empty
  // state are both reachable in dev (`general` below is seeded with content).
  {
    id: 2,
    token: 'talk-design-review',
    name: 'design review',
    archived: false,
    origin: 'talk',
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    color: 'indigo',
  },
  // Enough rooms, tinted and untinted, that the sidebar is worth scanning in
  // dev — which is the thing ISSUE-433 is about and which two rooms cannot show.
  {
    id: 3,
    token: 'web-carol-invoices',
    name: 'invoices',
    archived: false,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    color: 'citron',
  },
  {
    id: 4,
    token: 'web-carol-reading',
    name: 'reading list',
    archived: false,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    color: 'plum',
  },
  {
    id: 5,
    token: 'web-carol-errands',
    name: 'errands',
    archived: false,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  },
];
// Room memory (`CHANNEL.md`), keyed by room token. `general` starts with
// content and `design review` starts empty, so the editor and the
// offer-the-template empty state are both reachable without editing this file.
const CHANNEL_MEMORY_TEMPLATE = `# Channel Memory

This file contains remembered information about this channel/room.
The bot can append to this file to remember things relevant to all participants.

## Notes

`;
const mockChannelMemory = new Map<string, string>([
  [
    'web-carol-general',
    `# Channel Memory

## Notes

- Standing style: answer in British English, no bullet lists unless asked.
- The quarterly figures live in the shared drive, not in the repo.
- When a deploy is mentioned, always name the release tag.
`,
  ],
]);
// Flip to true in dev to exercise the 409 conflict branch: the next save is
// refused as though an agent had written the file in the meantime.
let mockMemoryConflictNext = false;
// Hashes what a *reader* sees, matching `_channel_memory_revision`: the server
// collapses a whitespace-only file to empty, so hashing the raw bytes would
// hand back a tag the next read can never reproduce.
const mockMemoryRevision = (content: string) =>
  createHash('sha256')
    .update(content.trim() ? content : '', 'utf8')
    .digest('hex');

const mockChatTasks = new Map<number, MockChatTask>();
let mockChatRoomSeq = 1;
let mockChatTaskSeq = 1000;

// Seed a large past-dated backlog in the `general` room so scroll-up paging +
// virtualization (ISSUE-131) and day-dividers are exercisable on the dev server
// (VITE_MOCK_API=1 npm run dev → /chat). Tasks are stamped well in the past so
// the history endpoint renders them as finished turns (not in-flight), spread
// every ~5h across ~25 days so several day boundaries fall inside the transcript.
// ids 1..N stay below the live-send sequence (1000) so they never collide.
(() => {
  const SEED_COUNT = 120;
  const STEP_MS = 5 * 60 * 60 * 1000; // 5h between turns
  const SEED_PROMPTS = [
    "today's headlines",
    'summarize my unread email',
    "what's on my calendar tomorrow",
    'draft a reply to the landlord',
    'how do I rebase onto main',
    'remind me to call the dentist',
    'weather this weekend',
    'explain keyset pagination',
    'add milk to the shopping list',
    'status of the deploy',
    'convert 20 miles to km',
    'what did we decide about the schema',
  ];
  const newest = Date.now() - 60_000; // 60s ago — safely past the done threshold
  for (let i = 0; i < SEED_COUNT; i++) {
    const id = i + 1; // 1..120
    // i=0 is the OLDEST; the last is the newest, just under `newest`.
    const createdAt = newest - (SEED_COUNT - 1 - i) * STEP_MS;
    mockChatTasks.set(id, {
      id,
      roomToken: 'web-carol-general',
      prompt: `${SEED_PROMPTS[i % SEED_PROMPTS.length]} (#${id})`,
      createdAt,
    });
  }
})();

// Seed a couple of finished MULTI-ROUND turns at the very bottom of `general`
// (newer than the backlog above, but well past the done threshold so they render
// as history). These exercise the render-group layout: activity chip →
// substantial intermediate prose → another activity chip → final answer. ids
// 200/201 sit between the backlog (1..120) and the live-send sequence (1000).
(() => {
  const base = Date.now() - 40_000; // 40s ago, finished
  mockChatTasks.set(200, {
    id: 200,
    roomToken: 'web-carol-general',
    prompt: 'tighten the moving-plan note',
    createdAt: base,
    variant: 'multiround',
  });
  mockChatTasks.set(201, {
    id: 201,
    roomToken: 'web-carol-general',
    prompt: 'fix the near-expiry 401s in auth',
    createdAt: base + 12_000,
    variant: 'multiround',
  });
  // One turn from outside the room, so the external treatment and the three
  // `external_turn_display` modes are reachable under VITE_MOCK_API=1. The
  // prompt is the mail's own text: the server strips the wrapper before it
  // reaches a reader, and the mock produces the payload, not the stored row.
  // A turn whose answer is a wide markdown table (ISSUE-413). The simple
  // variant's reply is the markdown sampler — headings, a list, a blockquote,
  // a fenced block — and a table was the one block it never carried, which is
  // why columns squashed to a character each in production without ever
  // showing up on the dev server. The shape matters as much as the presence:
  // a short label column beside a long prose one is what auto table layout
  // starves, so a table of uniformly short cells would render fine and prove
  // nothing. Newest turn in `general`, so /chat lands on it.
  mockChatTasks.set(203, {
    id: 203,
    roomToken: 'web-carol-general',
    prompt: 'what did the deployment check find',
    createdAt: base + 36_000,
    variant: 'table',
  });
  mockChatTasks.set(202, {
    id: 202,
    roomToken: 'web-carol-general',
    prompt:
      'Does the west branch work for you? I need about 30 minutes.\n\nHappy to come to you instead if that is easier — let me know either way.',
    createdAt: base + 24_000,
    origin: 'email',
    subject: 'Re: Scheduling',
    author: 'contact@example.com',
  });
})();

// A canned event timeline for a mock task (ms offsets from creation). Models the
// target UX: the model's work (inter-tool narration + tool calls) collapses into
// the single ActivityTrace chip (its "current step" updates live), and the FINAL
// ANSWER streams token-by-token, prominent, after the last tool. Tweak the
// timings / chunking here to eyeball the streaming behaviour in the dev frontend
// (VITE_MOCK_API=1 npm run dev → /chat) without a live backend.
// A MULTI-ROUND turn: the model does some work (a chip), writes a substantial
// intermediate analysis block (prose — kept because it's ≥ the substance bar),
// does more work (a second chip), then gives the final answer (prose). Exercises
// the render-group layout chip → text → chip → text. The intermediate block must
// stay over SUBSTANTIAL_TEXT_CHARS (280) so renderGroups renders it instead of
// folding it into the chip.
function mockMultiRoundTaskEvents(task: MockChatTask) {
  const SCENARIOS: Record<
    number,
    { tools1: [string, string][]; intermediate: string; tools2: [string, string][]; final: string }
  > = {
    200: {
      tools1: [
        ['c1', '⚙️ find the note'],
        ['c2', '📄 read "moving plan.md"'],
      ],
      intermediate:
        'Two things worth making explicit before I edit. First, the keep/decide step is the real bottleneck — sorting, listing, and scheduling are all blocked until it is done, so it belongs at the very top as the gating milestone rather than buried in the middle of the list. Second, the three vendor tracks are independent once that gate clears, so they should be grouped under one heading and marked as running in parallel, not sequentially.',
      tools2: [['c3', '✏️ edit "moving plan.md"']],
      final:
        'Done — restructured the note so the keep/decide step is the top gating milestone, with the three vendor tracks grouped under a single "parallel once unblocked" heading below it. I also removed the duplicated reframe line further down so it is not stated twice.',
    },
    201: {
      tools1: [
        ['c1', '🔎 grep verify_session'],
        ['c2', '📄 read auth/middleware.py'],
      ],
      intermediate:
        'Before changing anything, the core constraint: the session-token check lives in two places — the middleware and the login handler — and they have drifted. The middleware accepts an expired token inside a short grace window while the handler rejects it outright, which is the source of the intermittent 401s near expiry. I will consolidate both onto a single `verify_session` helper so the grace logic exists in exactly one place, then document the window, which currently is not written down anywhere.',
      tools2: [
        ['c3', '✏️ edit auth/session.py'],
        ['c4', '✏️ edit docs/auth.md'],
      ],
      final:
        'Refactored both call sites onto a shared `verify_session` helper and documented the five-minute grace window in the auth README. The middleware and handler now agree on expiry, so the intermittent 401s on near-expiry tokens should stop.',
    },
  };
  const s = SCENARIOS[task.id] ?? SCENARIOS[200];
  const events: { seq: number; kind: string; payload: Record<string, unknown>; at: number }[] = [];
  let seq = 1;
  let at = 0;
  const push = (kind: string, payload: Record<string, unknown>, dt: number) => {
    at += dt;
    events.push({ seq: seq++, kind, payload, at });
  };
  const stream = (text: string) => {
    for (const chunk of text.match(/.{1,14}/gs) ?? [text]) push('text_delta', { text: chunk }, 55);
  };
  push('task_started', { text: 'On it...' }, 0);
  push('thinking', { text: 'Let me look at the current state first. ' }, 200);
  for (const [id, desc] of s.tools1) {
    push('tool_start', { tool_name: 'Tool', description: desc, tool_call_id: id }, 300);
    push('tool_end', { tool_name: 'Tool', tool_call_id: id, success: true, duration_ms: 700 }, 700);
  }
  stream(s.intermediate); // intermediate analysis — prominent prose, then a tool follows
  for (const [id, desc] of s.tools2) {
    push('tool_start', { tool_name: 'Edit', description: desc, tool_call_id: id }, 300);
    push('tool_end', { tool_name: 'Edit', tool_call_id: id, success: true, duration_ms: 700 }, 700);
  }
  stream(s.final); // final answer
  push('result', { text: s.final, truncated: false }, 100);
  push(
    'done',
    { stop_reason: 'completed', duration_seconds: at / 1000, model: 'claude-opus-4-8' },
    200,
  );
  return events;
}

/** How long a `slow` prompt holds its turn open. Long enough to type a second
 *  message and watch it queue, short enough to sit through twice. */
const SLOW_HOLD_MS = 45_000;

/**
 * The turn's event timeline, with any `holdMs` applied.
 *
 * The hold lands *after* `task_started` and before everything else, so the
 * client goes busy immediately — Stop up, queue reachable — and simply
 * produces nothing for a while. Shifting `task_started` too would leave the
 * composer idle for the length of the hold, which is the opposite of what the
 * knob is for.
 */
function mockTaskEvents(task: MockChatTask) {
  const events = mockTaskEventsRaw(task);
  if (!task.holdMs) return events;
  return events.map((e) => (e.kind === 'task_started' ? e : { ...e, at: e.at + task.holdMs! }));
}

// A turn whose answer is a wide table (ISSUE-413). Deliberately the shape that
// broke: a short label column against a long prose one, with a couple of cells
// carrying an unbroken token (a hash, a path) so the break mode is exercised
// too. Two rows would do to see the layout; there are five because the
// starvation gets worse as the prose column's longest row grows.
function mockTableTaskEvents(task: MockChatTask) {
  const answer =
    'Deployment check finished. Everything passed except the egress allowlist, which is behaving as configured.\n\n' +
    '| Check | Observed |\n' +
    '| --- | --- |\n' +
    '| Source parity | 390 files under `src/` + `config/`, **0 diffs**, 0 not in repo |\n' +
    '| Egress allowlist | gitlab 301, pypi 200, npm 200; example.com and news.example.org **000** (correct — blocked) |\n' +
    '| Browse | render/extract/links/screenshot/close all ok; 99,925 chars from the first source, 16,193 from the second; screenshot verified on disk, 166 KB PNG 1439x812 |\n' +
    '| Devbox | running, restart_count 0, transport 6 ms; metadata endpoint 000 (blocked), open web 200 |\n' +
    '| Image digest | `sha256:a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90` |\n\n' +
    'Nothing here needs action.';
  const events: { seq: number; kind: string; payload: Record<string, unknown>; at: number }[] = [];
  let seq = 1;
  let at = 0;
  const push = (kind: string, payload: Record<string, unknown>, dt: number) => {
    at += dt;
    events.push({ seq: seq++, kind, payload, at });
  };
  push('task_started', { text: 'On it...' }, 0);
  push(
    'tool_start',
    { tool_name: 'Bash', description: '🔧 run deployment check', tool_call_id: 'c1' },
    300,
  );
  push(
    'tool_end',
    { tool_name: 'Bash', tool_call_id: 'c1', success: true, duration_ms: 1400 },
    1400,
  );
  for (const chunk of answer.match(/.{1,14}/gs) ?? [answer])
    push('text_delta', { text: chunk }, 40);
  push('result', { text: answer, truncated: false }, 100);
  push(
    'done',
    { stop_reason: 'completed', duration_seconds: at / 1000, model: 'claude-opus-4-8' },
    200,
  );
  return events;
}

function mockTaskEventsRaw(task: MockChatTask) {
  if (task.variant === 'multiround') return mockMultiRoundTaskEvents(task);
  if (task.variant === 'table') return mockTableTaskEvents(task);
  // A multi-paragraph markdown answer, chunked into small deltas so the live
  // prominent streaming (and incremental markdown) is visible.
  const reply =
    `Here are today's headlines for **${task.prompt.slice(0, 48)}**:\n\n` +
    '## Top stories\n\n' +
    '1. **Markets** rallied as inflation cooled for a third month.\n' +
    '2. **Tech** — a new on-device model shipped with `tool use` baked in.\n' +
    '3. **Sports** — the tournament bracket is set for the weekend.\n\n' +
    '> Streaming, tools, and `markdown` all render here in real time.\n\n' +
    // A fenced block, so the preview covers a reply whose copy carries code:
    // the block copy is the markdown source, fences included.
    '```bash\n' +
    'istota task "summarise today" -u carol --output-target both\n' +
    'istota list --status completed\n' +
    '```\n\n' +
    'Ask a follow-up, or try `!help` for commands.';
  const answerChunks = reply.match(/.{1,14}/gs) ?? [reply];

  const events: { seq: number; kind: string; payload: Record<string, unknown>; at: number }[] = [
    { seq: 1, kind: 'task_started', payload: { text: 'On it...' }, at: 0 },
    // Reasoning is still emitted by the brain (and exercised here), but the
    // web UI no longer renders it — the chip is tool-actions-only. These rows
    // verify the client correctly ignores `thinking`; they should NOT appear.
    {
      seq: 2,
      kind: 'thinking',
      payload: { text: "The user is asking for today's headlines. " },
      at: 250,
    },
    {
      seq: 3,
      kind: 'thinking',
      payload: { text: 'I should search the web for recent news first.' },
      at: 450,
    },
    {
      seq: 4,
      kind: 'tool_start',
      payload: {
        tool_name: 'WebSearch',
        description: '🔎 web_search "today\'s news"',
        tool_call_id: 'c1',
      },
      at: 800,
    },
    { seq: 5, kind: 'tool_progress', payload: { tool_call_id: 'c1', text: '7 results' }, at: 1600 },
    {
      seq: 6,
      kind: 'tool_end',
      payload: { tool_name: 'WebSearch', tool_call_id: 'c1', success: true, duration_ms: 1800 },
      at: 2600,
    },
    // Reasoning between tools — also ignored by the UI.
    {
      seq: 7,
      kind: 'thinking',
      payload: { text: 'Good results. Let me fetch the top source for detail.' },
      at: 2900,
    },
    {
      seq: 8,
      kind: 'tool_start',
      payload: {
        tool_name: 'WebFetch',
        description: '🌐 browse get justsecurity.org',
        tool_call_id: 'c2',
      },
      at: 3200,
    },
    {
      seq: 9,
      kind: 'tool_end',
      payload: { tool_name: 'WebFetch', tool_call_id: 'c2', success: true, duration_ms: 1900 },
      at: 5100,
    },
    // A final beat of reasoning before the answer streams.
    {
      seq: 10,
      kind: 'thinking',
      payload: { text: 'I have enough to summarize the top stories now.' },
      at: 5250,
    },
  ];

  // The final answer streams in, chunked, after the last tool — prominent and
  // live — then the canonical result reconciles it.
  let seq = 11;
  const answerStart = 5500;
  const perChunk = 70; // ms between chunks → visibly streaming markdown
  answerChunks.forEach((chunk, i) => {
    events.push({
      seq: seq++,
      kind: 'text_delta',
      payload: { text: chunk },
      at: answerStart + i * perChunk,
    });
  });
  const answerEnd = answerStart + answerChunks.length * perChunk;
  events.push({
    seq: seq++,
    kind: 'result',
    payload: { text: reply, truncated: false },
    at: answerEnd + 100,
  });
  events.push({
    seq: seq++,
    kind: 'done',
    payload: {
      stop_reason: 'completed',
      duration_seconds: (answerEnd + 200) / 1000,
      model: 'claude-opus-4-8',
    },
    at: answerEnd + 200,
  });
  return events;
}
// Safely past the timeline's terminal `done` (answer streams ~5.5s→~7.5s); the
// history endpoint uses this to decide whether a task has finished streaming.
const MOCK_TASK_DONE_MS = 8000;
/** When a task counts as finished, which a `slow` hold pushes out with it.
 *
 * Three call sites read this and the event timeline reads `holdMs` separately,
 * so a flat constant would have the message list calling a turn durable while
 * its own stream was still holding — the room would show a finished answer
 * with Stop still up beside it. */
function mockTaskDoneMs(t: MockChatTask): number {
  return MOCK_TASK_DONE_MS + (t.holdMs ?? 0);
}

// Mock !command output so the command rendering (lists, code, tables) can be
// previewed without a live backend. Returns the inline markdown for a command,
// or null when the input is a `!model <alias> <prompt>` prefix that should
// create a real task instead (mirrors the server: unknown alias → usage).
// Base names only — effort is the orthogonal :effort modifier, not a separate alias.
const MOCK_MODEL_ALIASES = ['default', 'fast', 'general', 'smart', 'opus', 'sonnet', 'haiku'];
const MOCK_HELP = [
  '**Available commands:**',
  '',
  '- `!check` -- Run Claude Code health check',
  '- `!cron` -- List/enable/disable scheduled jobs',
  '- `!export` -- Export conversation history to a file: `!export [markdown|text]`',
  '- `!help` -- List available commands',
  '- `!memory` -- Show memory: `!memory user`, `!memory channel`, `!memory facts`',
  '- `!models` -- List available model aliases (and what they resolve to)',
  '- `!more` -- Show execution trace for a task: `!more #123`',
  '- `!search` -- Search conversation history: `!search <query>`',
  '- `!skills` -- List available skills and their triggers',
  '- `!status` -- Show your running/pending tasks and system status',
  '- `!stop` -- Cancel your currently running task',
  '',
  '**Per-task model override:**',
  '',
  '- `!model <alias> <prompt>` — one-shot. Aliases: ' +
    MOCK_MODEL_ALIASES.map((a) => `\`${a}\``).join(', ') +
    '.',
].join('\n');
const MOCK_MODELS = [
  '**Model aliases**',
  '',
  'Use `!model <alias> <prompt>` to override the model for a single task.',
  '',
  '- `default` → (no override — use default)',
  '- `fast` → `claude-haiku-4-5`',
  '- `general` → `claude-sonnet-4-6`',
  '- `smart` → `claude-opus-4-8`',
  '- `opus` → `claude-opus-4-8`',
  '- `opus-high` → `claude-opus-4-8` + effort `high`',
].join('\n');

function mockCommandResult(text: string): string | null {
  const lower = text.toLowerCase();
  const name = lower.slice(1).split(/\s/)[0];
  if (name === 'model') {
    const alias = lower.split(/\s+/)[1] || '';
    if (alias && MOCK_MODEL_ALIASES.includes(alias) && text.split(/\s+/).length > 2) {
      return null; // valid prefix with a prompt → real task
    }
    return (
      'Usage: `!model <alias> <prompt>`. Aliases: ' +
      MOCK_MODEL_ALIASES.map((a) => `\`${a}\``).join(', ') +
      '.'
    );
  }
  if (name === 'help') return MOCK_HELP;
  if (name === 'models') return MOCK_MODELS;
  if (name === 'status') return 'No active or pending tasks.\n\n**System:** 0 running, 0 queued';
  return `Mock command result for \`${text}\`.`;
}

// ---- Cross-room views + starring mock state ----
// Stars are keyed on the synthetic per-message ids below (user = id*10+1,
// assistant = id*10+2) so both room history and the aggregate views agree.
const mockStars = new Set<number>();
// Per-message delete. Rows here are derived from tasks rather than stored, so
// a delete is a suppression set; the ordered log stands in for the backend's
// `message_deletions` ledger, which is what the deletion tail cursors on.
const mockDeletedMsgIds = new Set<number>();
const mockDeletionLog: { id: number; msg_id: number; room_token: string }[] = [];
const mockUserMsgId = (t: MockChatTask) => t.id * 10 + 1;
const mockAsstMsgId = (t: MockChatTask) => t.id * 10 + 2;

// ---- Held outbound drafts ----
// The approval gate's cards had no mock at all, so the one surface whose whole
// job is to be read before it is answered could not be looked at in dev — which
// is how a banner card came to sit 134px narrower than the confirmation card
// above it, and how it took 360px with its own Send button clipped below a
// scrollbar, without either being noticed.
//
// The set covers all four shapes the card renders, because they are checked in
// order and the later ones assume content the earlier ones lack: a `pending`
// draft carrying a `task_id` that matches a seeded turn (so it renders *inline*
// under that turn), a `pending` one with no task at all (the loose list above
// the transcript — a scheduled job mailing an external address), a `sending`
// row stuck between the claim and the finalize, an `unreadable` row whose
// stored JSON does not parse, and a `truncated` stub standing in for a stream
// frame that spent its byte budget.
//
// Bodies are fabricated and every address is in a reserved example domain. This
// file is committed to a public repo, and a held email is exactly the shape of
// thing whose realistic version would be somebody's actual correspondence.
type MockDraft = Record<string, unknown> & { id: number };
const mockDrafts = new Map<number, MockDraft>([
  [
    61,
    {
      id: 61,
      status: 'pending',
      room_token: 'web-carol-general',
      // Matches the seeded multi-round turn, so this one renders under it.
      task_id: 201,
      subject: 'Re: the near-expiry 401s',
      body: 'Thanks for the report — I found it.\n\nThe middleware and the handler disagreed about when a token expires, so anything inside the five-minute grace window was accepted by one and refused by the other. Both call sites now go through one helper and the README says which window applies.\n\nI have not touched the refresh path; if you are still seeing 401s after this goes out, that is where I would look next.',
      html: false,
      to: ['contact@example.com'],
      cc: [],
      bcc: [],
      attachments: [],
      hold_reason: 'untrusted_recipient',
      created_at: new Date(Date.now() - 30_000).toISOString(),
      actions_taken: [],
    },
  ],
  [
    62,
    {
      id: 62,
      status: 'pending',
      // No task and no room: a scheduled job mailing an address of its own. This
      // is the fallback placement — the card the page's own list has to carry,
      // and the reason that list exists at all.
      room_token: null,
      task_id: null,
      subject: 'Re: Scheduling — next week',
      body: 'Thanks for the note — the west branch works fine for me.\n\nTuesday at two or Wednesday morning are both open, so pick whichever suits. I can come to you instead if that is easier; it is a short walk either way and I have no strong preference.\n\nOne thing worth flagging before we lock it in: the upstairs room has no projector, so if you were planning to show slides we should take the other one.',
      html: false,
      to: ['contact@example.com'],
      cc: ['assistant@example.com'],
      bcc: [],
      attachments: [],
      hold_reason: 'untrusted_recipient',
      created_at: new Date(Date.now() - 90_000).toISOString(),
      actions_taken: ['Created calendar event: Coffee, Wed 14:00'],
    },
  ],
  [
    63,
    {
      id: 63,
      // Claimed, then the process died before it could finalize. The card
      // offers nothing here on purpose — nobody can know whether it went out,
      // and one of the two actions would send it twice.
      status: 'sending',
      room_token: null,
      task_id: null,
      subject: 'Invoice 0042',
      body: 'Attached, as discussed.',
      html: false,
      to: ['billing@example.com'],
      cc: [],
      bcc: [],
      attachments: ['invoice-0042.pdf'],
      hold_reason: 'all_mode',
      created_at: new Date(Date.now() - 6 * 60 * 60 * 1000).toISOString(),
      actions_taken: [],
    },
  ],
  [
    64,
    {
      id: 64,
      // A stored column that does not parse. Named rather than dropped: held
      // mail that silently disappears is mail the user never hears about.
      status: 'pending',
      room_token: null,
      task_id: null,
      unreadable: true,
      created_at: new Date(Date.now() - 26 * 60 * 60 * 1000).toISOString(),
    },
  ],
  [
    65,
    {
      id: 65,
      // The stub a byte-budgeted stream frame carries. The card asks for the
      // full row on arrival, which `GET /chat/drafts` answers — so in dev this
      // one flips to a full card a moment after it appears, exactly as it does
      // against the real backend.
      status: 'pending',
      room_token: null,
      task_id: null,
      truncated: true,
      created_at: new Date(Date.now() - 4 * 60 * 1000).toISOString(),
    },
  ],
]);
// The stub's full row, handed over when the card asks for it. Kept apart from
// the map so the first read really is a stub and the second really is not —
// collapsing them would make the stub path untestable by eye, which is the one
// thing this entry is here for.
let mockStubFilled = false;
const mockDraft65Full: MockDraft = {
  id: 65,
  status: 'pending',
  room_token: null,
  task_id: null,
  subject: 'Re: the archive migration',
  body: 'Yes — Friday works. I will have the export ready by Thursday evening so there is a day of slack.',
  html: false,
  to: ['contact@example.com'],
  cc: [],
  bcc: [],
  attachments: [],
  hold_reason: 'untrusted_recipient',
  created_at: new Date(Date.now() - 4 * 60 * 1000).toISOString(),
  actions_taken: [],
};
// Everything newer than this reads as unread (assistant rows only); read-all
// advances it to now. Seeded ~16h back so the last few turns light up.
let mockReadCursorMs = Date.now() - 16 * 60 * 60 * 1000;

function mockRoomFor(token: string): MockChatRoom | undefined {
  return mockChatRooms.find((r) => r.token === token);
}

// The cited parent, in the payload shape the backend's LEFT JOIN produces: a
// live parent carries its role and a display excerpt, a deleted one carries
// only the id it used to name.
function mockReplyTo(msgId: number): Record<string, unknown> {
  if (mockDeletedMsgIds.has(msgId)) return { msg_id: msgId, deleted: true };
  const taskId = Math.floor(msgId / 10);
  const t = mockChatTasks.get(taskId);
  if (!t) return { msg_id: msgId, deleted: true };
  const isUser = msgId % 10 === 1;
  const body = isUser
    ? t.prompt
    : ((mockTaskEvents(t).find((e) => e.kind === 'result')?.payload as any)?.text ?? '');
  return {
    msg_id: msgId,
    role: isUser ? 'user' : 'assistant',
    excerpt: String(body).slice(0, 200),
    deleted: false,
  };
}

// A finished task's (user, assistant) message pair, in the history payload
// shape — shared by the per-room endpoint and the aggregate views.
function mockFinishedTurn(t: MockChatTask): { user: any; assistant: any } {
  const evs = mockTaskEvents(t);
  const ev = evs.find((e) => e.kind === 'result');
  const result = (ev?.payload as any).text as string;
  // Mirror the backend: a finished turn carries its tool trace + duration so
  // the action strip + timing persist on reload (ISSUE-122).
  const tools = evs
    .filter((e) => e.kind === 'tool_start')
    .map((e) => (e.payload as any).description as string);
  // Ordered segments rebuilt from the event timeline (mirrors the backend
  // `_trace_segments`): consecutive text_deltas collapse into one text
  // segment, tool_starts become tool segments, and the canonical result
  // reconciles the trailing answer.
  const segments: { kind: string; text: string }[] = [];
  for (const e of evs) {
    if (e.kind === 'text_delta') {
      const last = segments[segments.length - 1];
      if (last && last.kind === 'text') last.text += (e.payload as any).text;
      else segments.push({ kind: 'text', text: (e.payload as any).text });
    } else if (e.kind === 'thinking') {
      const last = segments[segments.length - 1];
      if (last && last.kind === 'thinking') last.text += (e.payload as any).text;
      else segments.push({ kind: 'thinking', text: (e.payload as any).text });
    } else if (e.kind === 'tool_start') {
      segments.push({ kind: 'tool', text: (e.payload as any).description });
    }
  }
  if (result) {
    const last = segments[segments.length - 1];
    if (last && last.kind === 'text') last.text = result;
    else segments.push({ kind: 'text', text: result });
  }
  const done = evs.find((e) => e.kind === 'done');
  return {
    user: {
      role: 'user',
      text: t.prompt,
      task_id: t.id,
      created_at: new Date(t.createdAt).toISOString(),
      msg_id: mockUserMsgId(t),
      starred: mockStars.has(mockUserMsgId(t)),
      ...(t.attachments?.length ? { attachments: t.attachments } : {}),
      ...(t.attachmentPaths?.length ? { attachment_paths: t.attachmentPaths } : {}),
      ...(t.replyToMsgId ? { reply_to: mockReplyTo(t.replyToMsgId) } : {}),
      // Provenance for a turn from outside the room. Emitted here for the same
      // reason `reply_to` is: a turn that renders as external in one view and
      // ordinary in another is worse than one that renders external in neither,
      // and the mock is the third producer of this shape.
      ...(t.origin ? { origin: t.origin } : {}),
      ...(t.subject ? { subject: t.subject } : {}),
      ...(t.author ? { author: t.author } : {}),
    },
    assistant: {
      role: 'assistant',
      text: result,
      task_id: t.id,
      status: 'completed',
      created_at: new Date(t.createdAt).toISOString(),
      tools,
      segments,
      duration_seconds: (done?.payload as any)?.duration_seconds ?? null,
      model: (done?.payload as any)?.model ?? null,
      msg_id: mockAsstMsgId(t),
      starred: mockStars.has(mockAsstMsgId(t)),
    },
  };
}

// Flattened durable message rows across every room, for the aggregate views.
function mockAggregateRows(): any[] {
  const now = Date.now();
  const rows: { msg: any; createdAt: number }[] = [];
  for (const t of mockChatTasks.values()) {
    if (now - t.createdAt < mockTaskDoneMs(t)) continue; // durable turns only
    const room = mockRoomFor(t.roomToken);
    if (!room || room.archived) continue;
    const turn = mockFinishedTurn(t);
    rows.push({
      msg: { ...turn.user, room_token: room.token, room_name: room.name },
      createdAt: t.createdAt,
    });
    rows.push({
      msg: { ...turn.assistant, room_token: room.token, room_name: room.name },
      createdAt: t.createdAt,
    });
  }
  rows.sort((a, b) => a.createdAt - b.createdAt || a.msg.msg_id - b.msg.msg_id);
  return rows
    .filter((r) => !mockDeletedMsgIds.has(r.msg.msg_id))
    .map((r) => ({ ...r.msg, _createdAtMs: r.createdAt }));
}

function mockUnreadCount(token: string): number {
  const now = Date.now();
  let n = 0;
  for (const t of mockChatTasks.values()) {
    if (t.roomToken !== token) continue;
    if (now - t.createdAt < mockTaskDoneMs(t)) continue;
    if (t.createdAt > mockReadCursorMs) n++; // one unread assistant row per turn
  }
  return n;
}

const chatHandler: MockHandler = ({ url, method, body }) => {
  if (!url.startsWith('/istota/api/chat/')) return undefined;
  const path = url.split('?')[0];

  if (path === '/istota/api/chat/config') {
    return {
      // The send queue's storage key is `<user>:room:<token>` and the store
      // takes the user half from here (ISSUE-238), so without it the whole
      // persistence half — restore-as-held, and the bubbles that go with it —
      // is unreachable in dev while the deployed app has it.
      user_id: user.username,
      max_prompt_chars: 32000,
      max_attachment_mb: 25,
      attachment_extensions: ['pdf', 'png', 'jpg'],
      client_poll_interval_ms: 600,
      external_turn_display: 'collapsed',
    };
  }

  if (path === '/istota/api/chat/commands') {
    return {
      commands: [
        { name: 'check', help: 'Run Claude Code health check' },
        {
          name: 'confirm',
          help: 'Answer a held task: `!confirm`, `!confirm <task-id>`, `!confirm <id> no`',
        },
        {
          name: 'cron',
          help: 'List/enable/disable scheduled jobs: `!cron`, `!cron enable <name>`',
        },
        {
          name: 'export',
          help: 'Export conversation history to a file: `!export [markdown|text]`',
        },
        { name: 'help', help: 'List available commands' },
        { name: 'memory', help: 'Show memory: `!memory user`, `!memory channel`, `!memory facts`' },
        { name: 'models', help: 'List available model aliases (and what they resolve to)' },
        { name: 'more', help: 'Show execution trace for a task: `!more #31875` or `!more 31875`' },
        { name: 'search', help: 'Search conversation history: `!search <query>`' },
        { name: 'skills', help: 'List available skills and their triggers' },
        { name: 'status', help: 'Show your running/pending tasks and system status' },
        { name: 'stop', help: 'Cancel your currently running task' },
        { name: 'trust', help: 'Trust an email sender: `!trust sender@example.com`' },
        { name: 'untrust', help: 'Remove a trusted email sender: `!untrust sender@example.com`' },
        { name: 'usage', help: 'Show token usage, and plan limits on a subscription' },
      ],
      // The hidden alias table, mirroring `commands._COMMAND_ALIASES` in full.
      // Without it a mid-turn `!inject` queues on a dev server and answers
      // inline in the deployed app, which is the divergence ISSUE-350 was
      // about — and a partial list reproduces that divergence for the names it
      // leaves out, in the environment the frontend is developed in. Nothing
      // pins this to the server table, so a new alias has to be added here by
      // hand; an honest empty list beats a stale subset.
      command_aliases: [
        { alias: 'approve', target: 'confirm' },
        { alias: 'decline', target: 'confirm' },
        { alias: 'inject', target: 'steer' },
        { alias: 'limits', target: 'usage' },
        { alias: 'n', target: 'confirm' },
        { alias: 'no', target: 'confirm' },
        { alias: 'reject', target: 'confirm' },
        { alias: 'y', target: 'confirm' },
        { alias: 'yes', target: 'confirm' },
      ],
      model_aliases: [
        { alias: 'fast', target: 'claude-haiku-4-5', effort: null },
        { alias: 'general', target: 'claude-sonnet-4-6', effort: null },
        { alias: 'smart', target: 'claude-opus-4-8', effort: null },
        { alias: 'opus', target: 'claude-opus-4-8', effort: null },
        { alias: 'opus-high', target: 'claude-opus-4-8', effort: 'high' },
        { alias: 'sonnet', target: 'claude-sonnet-4-6', effort: null },
        { alias: 'haiku', target: 'claude-haiku-4-5', effort: null },
      ],
    };
  }

  if (path === '/istota/api/chat/rooms' && method === 'GET') {
    return {
      rooms: mockChatRooms
        .filter((r) => !r.archived)
        .map((r) => ({ ...r, unread_count: mockUnreadCount(r.token) })),
    };
  }
  if (path === '/istota/api/chat/rooms/read-all' && method === 'POST') {
    const moved = mockChatRooms.filter((r) => !r.archived && mockUnreadCount(r.token) > 0).length;
    mockReadCursorMs = Date.now();
    return { ok: true, updated: moved };
  }
  // Cross-room aggregate views (All / Unread / Starred).
  // Held outbound mail. The card reads this on mount and again whenever a stub
  // arrives, so the truncated entry resolves itself on the second read.
  if (path === '/istota/api/chat/drafts' && method === 'GET') {
    if (mockStubFilled) mockDrafts.set(65, mockDraft65Full);
    mockStubFilled = true;
    return { drafts: [...mockDrafts.values()] };
  }
  {
    // Approve / discard / edit. Answering removes the row, which is what lets
    // the optimistic removal and the re-read that follows it be watched in dev.
    // A `sending` row refuses either action with the 409 + `state` the client
    // reads against the action it attempted — the same status means opposite
    // things to Send and to Discard, and that branch is only reachable here.
    const m = path.match(/^\/istota\/api\/chat\/drafts\/(\d+)(\/approve|\/discard)?$/);
    if (m && (method === 'POST' || method === 'PATCH')) {
      const id = Number(m[1]);
      const row = mockDrafts.get(id);
      if (!row) return { __status: 404, error: 'no such draft', state: 'gone' };
      if (row.status === 'sending') {
        return { __status: 409, error: 'already going out', state: 'sending' };
      }
      if (m[2] === '/approve' && row.unreadable) {
        return { __status: 409, error: 'cannot be read', state: 'unreadable' };
      }
      if (method === 'PATCH') {
        mockDrafts.set(id, { ...row, body: String((body as any)?.body ?? row.body ?? '') });
        return { ok: true };
      }
      mockDrafts.delete(id);
      return { ok: true };
    }
  }
  if (path === '/istota/api/chat/messages' && method === 'GET') {
    const q = new URL(`http://x${url}`).searchParams;
    const viewName = q.get('view') || 'all';
    if (!['all', 'unread', 'starred'].includes(viewName)) return { error: 'unknown view' };
    const limit = Math.max(1, Math.min(Number(q.get('limit') || '50'), 200));
    const beforeTs = q.get('before_ts');
    const beforeId = q.get('before_id');
    const before =
      beforeTs != null && beforeId != null
        ? { ts: Date.parse(beforeTs), id: Number(beforeId) }
        : null;
    let rows = mockAggregateRows(); // oldest-first
    if (viewName === 'unread') {
      rows = rows.filter((m) => m.role !== 'user' && m._createdAtMs > mockReadCursorMs);
    } else if (viewName === 'starred') {
      rows = rows.filter((m) => mockStars.has(m.msg_id));
    }
    const older = before
      ? rows.filter(
          (m) =>
            m._createdAtMs < before.ts || (m._createdAtMs === before.ts && m.msg_id < before.id),
        )
      : rows;
    const page = older.slice(Math.max(0, older.length - limit));
    const oldest = page[0];
    const hasMore = oldest ? older.length > page.length : false;
    return {
      messages: page.map(({ _createdAtMs, ...m }) => m),
      has_more: hasMore,
      oldest_cursor: oldest
        ? { ts: new Date(oldest._createdAtMs).toISOString(), id: oldest.msg_id }
        : null,
    };
  }
  // Room-event tail. The mock server can't hold an SSE connection, so the
  // client's EventSource fails over to polling this — which is exactly the
  // degradation path the real deployment uses behind a buffering proxy, so dev
  // exercises it by default.
  if (path === '/istota/api/chat/events' && method === 'GET') {
    const q = new URL(`http://x${url}`).searchParams;
    const sinceId = Math.max(0, Number(q.get('since_id') || '0'));
    const sinceDelId = Math.max(0, Number(q.get('since_deletion_id') || '0'));
    const limitParam = Number(q.get('limit') || '0');
    const rows = mockAggregateRows();
    const maxId = rows.reduce((n, m) => Math.max(n, m.msg_id), 0);
    const fresh = rows.filter((m) => m.msg_id > sinceId);
    const want = limitParam > 0 ? Math.min(limitParam, 500) : 500;
    // The deletion tail rides the same response as on the real backend, so the
    // polling fallback isn't a downgrade — and it is emitted even on a `gap`,
    // whose reload would otherwise leave the cursor stuck resending it.
    const deletions = mockDeletionLog.filter((d) => d.id > sinceDelId);
    const delCursor = mockDeletionLog.reduce((n, d) => Math.max(n, d.id), sinceDelId);
    if (fresh.length > want) {
      return {
        events: [],
        cursor: maxId,
        gap: true,
        deletions: deletions.map(({ msg_id, room_token }) => ({ msg_id, room_token })),
        deletion_cursor: delCursor,
      };
    }
    return {
      events: fresh.map(({ _createdAtMs, ...m }) => m),
      cursor: Math.max(maxId, sinceId),
      gap: false,
      deletions: deletions.map(({ msg_id, room_token }) => ({ msg_id, room_token })),
      deletion_cursor: delCursor,
    };
  }
  const starMatch = path.match(/^\/istota\/api\/chat\/messages\/(\d+)\/star$/);
  if (starMatch && method === 'PUT') {
    const msgId = Number(starMatch[1]);
    const starred = !!body?.starred;
    if (starred) mockStars.add(msgId);
    else mockStars.delete(msgId);
    return { ok: true, starred };
  }
  const msgDelete = path.match(/^\/istota\/api\/chat\/messages\/(\d+)$/);
  if (msgDelete && method === 'DELETE') {
    const msgId = Number(msgDelete[1]);
    if (mockDeletedMsgIds.has(msgId)) return { status: 'gone' };
    const row = mockAggregateRows().find((m) => m.msg_id === msgId);
    if (!row) return { __status: 404, error: 'message not found' };
    mockDeletedMsgIds.add(msgId);
    mockStars.delete(msgId);
    mockDeletionLog.push({
      id: mockDeletionLog.length + 1,
      msg_id: msgId,
      room_token: row.room_token,
    });
    return { ok: true, message_id: msgId };
  }
  if (path === '/istota/api/chat/rooms' && method === 'POST') {
    const room: MockChatRoom = {
      id: ++mockChatRoomSeq,
      token: `web-carol-${mockChatRoomSeq}`,
      name: (body?.name || 'room').slice(0, 80),
      archived: false,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    mockChatRooms.push(room);
    return room;
  }
  // Room memory (`CHANNEL.md`) — GET returns the file plus an opaque revision
  // the PUT must hand back; a mismatch is the 409 the editor recovers from.
  const roomMemory = path.match(/^\/istota\/api\/chat\/rooms\/(\d+)\/memory$/);
  if (roomMemory && (method === 'GET' || method === 'PUT')) {
    const room = mockChatRooms.find((r) => r.id === Number(roomMemory[1]));
    if (!room) return { error: 'room not found', __status: 404 };
    const stored = mockChannelMemory.get(room.token) ?? '';
    if (method === 'GET') {
      return {
        room_id: room.id,
        token: room.token,
        content: stored,
        exists: stored.trim().length > 0,
        shared: room.origin === 'talk',
        template: CHANNEL_MEMORY_TEMPLATE,
        revision: mockMemoryRevision(stored),
      };
    }
    const content = typeof body?.content === 'string' ? body.content : null;
    if (content === null) return { error: 'content required', __status: 400 };
    if (typeof body?.revision !== 'string') return { error: 'revision required', __status: 400 };
    if (Buffer.byteLength(content, 'utf8') > 256 * 1024)
      return {
        error: 'channel memory too large',
        code: 'too_large',
        max_bytes: 256 * 1024,
        __status: 413,
      };
    if (mockMemoryConflictNext || body.revision !== mockMemoryRevision(stored)) {
      mockMemoryConflictNext = false;
      return {
        error: 'channel memory changed since it was loaded',
        code: 'conflict',
        __status: 409,
      };
    }
    mockChannelMemory.set(room.token, content);
    return { status: 'ok', revision: mockMemoryRevision(content) };
  }

  const roomPatch = path.match(/^\/istota\/api\/chat\/rooms\/(\d+)$/);
  if (roomPatch && method === 'PATCH') {
    const room = mockChatRooms.find((r) => r.id === Number(roomPatch[1]));
    if (!room) return { error: 'room not found' };
    if (body?.name != null) room.name = String(body.name).slice(0, 80);
    if (body?.archived != null) room.archived = !!body.archived;
    if ('model' in (body ?? {}))
      room.model = (String(body.model || '').trim() || null) as string | null;
    if ('effort' in (body ?? {}))
      room.effort = (String(body.effort || '').trim() || null) as string | null;
    // Same key-presence contract the server applies, and validated against the
    // same palette — a mock that accepted anything would let a name the real
    // backend 400s look like it worked (ISSUE-433).
    if ('color' in (body ?? {})) {
      const c = String(body.color || '')
        .trim()
        .toLowerCase();
      if (c && !(ROOM_COLORS as readonly string[]).includes(c)) return { error: 'unknown color' };
      room.color = c || null;
    }
    return room;
  }
  if (roomPatch && method === 'DELETE') {
    const idx = mockChatRooms.findIndex((r) => r.id === Number(roomPatch[1]));
    if (idx < 0) return { error: 'room not found' };
    mockChatRooms.splice(idx, 1);
    return { status: 'ok' };
  }

  const msgMatch = path.match(/^\/istota\/api\/chat\/rooms\/(\d+)\/messages$/);
  if (msgMatch && method === 'GET') {
    const room = mockChatRooms.find((r) => r.id === Number(msgMatch[1]));
    if (!room) return { error: 'room not found' };
    const q = new URL(`http://x${url}`).searchParams;
    const limit = Math.max(1, Math.min(Number(q.get('limit') || '50'), 200));
    const beforeTs = q.get('before_ts');
    const beforeId = q.get('before_id');
    const before =
      beforeTs != null && beforeId != null
        ? { ts: Date.parse(beforeTs), id: Number(beforeId) }
        : null;
    const now = Date.now();
    // Keyset over (createdAt, id): newest-first window, optional `before` cut.
    // The mock treats each task as one spine unit; the cursor is the oldest
    // task in the page. (Faithful enough to exercise the client's scroll-up.)
    const all = [...mockChatTasks.values()]
      .filter((t) => t.roomToken === room.token)
      .sort((a, b) => a.createdAt - b.createdAt || a.id - b.id);
    const older = before
      ? all.filter(
          (t) => t.createdAt < before.ts || (t.createdAt === before.ts && t.id < before.id),
        )
      : all;
    const page = older.slice(Math.max(0, older.length - limit));
    const oldest = page[0];
    const hasMore = oldest
      ? older.some(
          (t) =>
            t.createdAt < oldest.createdAt ||
            (t.createdAt === oldest.createdAt && t.id < oldest.id),
        )
      : false;
    const tasks = page;
    const messages: any[] = [];
    let active: any = null;
    for (const t of tasks) {
      if (now - t.createdAt >= mockTaskDoneMs(t)) {
        // Finished turn: the shared builder carries msg_id/starred (and the
        // full segments/tools shape) so history matches the backend payload.
        const turn = mockFinishedTurn(t);
        for (const m of [turn.user, turn.assistant]) {
          if (!mockDeletedMsgIds.has(m.msg_id)) messages.push(m);
        }
      } else {
        // In-flight: aux-only on the backend too — no msg_id, not starrable.
        messages.push({
          role: 'user',
          text: t.prompt,
          task_id: t.id,
          created_at: new Date(t.createdAt).toISOString(),
          ...(t.attachments?.length ? { attachments: t.attachments } : {}),
          ...(t.attachmentPaths?.length ? { attachment_paths: t.attachmentPaths } : {}),
        });
        active = { id: t.id, status: 'running' };
      }
    }
    // active_tasks resume only on the first load; an older page carries none.
    return {
      messages,
      active_task: before ? null : active,
      active_tasks: before || !active ? [] : [active],
      has_more: hasMore,
      oldest_cursor: oldest
        ? { ts: new Date(oldest.createdAt).toISOString(), id: oldest.id }
        : null,
    };
  }
  if (msgMatch && method === 'POST') {
    const room = mockChatRooms.find((r) => r.id === Number(msgMatch[1]));
    if (!room) return { error: 'room not found' };
    const attachments: string[] = Array.isArray(body?.attachments) ? body.attachments : [];
    const rawNames: string[] = Array.isArray(body?.attachment_names) ? body.attachment_names : [];
    // Same rule as the server: positional display labels, discarded wholesale
    // on a count mismatch rather than landing on the wrong file.
    const attachmentNames = attachments.length
      ? rawNames.length === attachments.length
        ? rawNames.map(String)
        : attachments.map((p) => p.split('/').pop() || p)
      : [];
    // Mirrors the server's ingest-time translation: an upload under the user's
    // own workspace becomes a linkable path, anything else stays inert.
    const attachmentPaths = attachments.map(mockWorkspacePath);
    // An attachment-only send (a composer voice memo) is a real message: the
    // recording is the content. Mirrors the server's descriptor stand-in.
    let text = String(body?.text || '').trim();
    if (!text) {
      if (!attachments.length) return { error: 'text or attachment required' };
      text = 'Voice message (see attached audio).';
    }
    if (text.startsWith('!')) {
      const inline = mockCommandResult(text);
      if (inline !== null) return { task_id: null, inline_result: inline };
      // !model <alias> <prompt> falls through to a real task (override carried).
    }
    // A cited parent must live in the room being posted into; an unknown id
    // and a foreign one answer alike, so neither becomes an oracle for the
    // other. Mirrors the server's refusal rather than dropping the citation.
    const replyToMsgId =
      typeof body?.reply_to_msg_id === 'number' && body.reply_to_msg_id > 0
        ? body.reply_to_msg_id
        : undefined;
    if (replyToMsgId !== undefined) {
      const parentTask = mockChatTasks.get(Math.floor(replyToMsgId / 10));
      if (
        !parentTask ||
        parentTask.roomToken !== room.token ||
        mockDeletedMsgIds.has(replyToMsgId)
      ) {
        return {
          __status: 404,
          error: 'the message you replied to is no longer available',
        };
      }
    }
    const id = ++mockChatTaskSeq;
    // Type a message containing "multiround" to stream the multi-step shape
    // (chip → intermediate prose → chip → final) live, not just in history.
    const variant: 'simple' | 'multiround' = /multiround/i.test(text) ? 'multiround' : 'simple';
    // Type a message containing "slow" to hold the turn open long enough to
    // queue a second one behind it (see MockChatTask.holdMs).
    const holdMs = /\bslow\b/i.test(text) ? SLOW_HOLD_MS : 0;
    mockChatTasks.set(id, {
      id,
      roomToken: room.token,
      prompt: text,
      createdAt: Date.now(),
      variant,
      holdMs,
      attachments: attachmentNames,
      attachmentPaths: attachmentPaths.some(Boolean) ? attachmentPaths : undefined,
      replyToMsgId,
    });
    return {
      task_id: id,
      status: 'pending',
      stream_url: `/istota/api/chat/tasks/${id}/stream`,
      snapshot_url: `/istota/api/chat/tasks/${id}/events`,
    };
  }

  const evMatch = path.match(/^\/istota\/api\/chat\/tasks\/(\d+)\/events$/);
  if (evMatch && method === 'GET') {
    const id = Number(evMatch[1]);
    const sinceSeq = Number(new URL(`http://x${url}`).searchParams.get('since_seq') || '0');
    const task = mockChatTasks.get(id);
    if (!task) return { events: [] };
    const elapsed = Date.now() - task.createdAt;
    const events = mockTaskEvents(task)
      .filter((e) => e.at <= elapsed && e.seq > sinceSeq)
      .map((e) => ({
        seq: e.seq,
        kind: e.kind,
        payload: e.payload,
        created_at: new Date().toISOString(),
      }));
    return { events };
  }

  if (path.match(/^\/istota\/api\/chat\/tasks\/\d+\/(confirm|cancel)$/) && method === 'POST') {
    return { status: 'ok' };
  }

  if (path === '/istota/api/chat/attachments' && method === 'POST') {
    // Echo the uploaded file's own name, as the real endpoint does — a fixed
    // 'upload' here made every attachment chip in dev show the wrong label.
    // The body arrives as the raw multipart payload; the part headers are
    // ASCII at the front, so the filename survives the utf8 decode.
    const name =
      (typeof body === 'string' ? body : '').match(/filename="([^"]*)"/)?.[1] || 'upload';
    const stored = `inbox/web-chat/mock/${Date.now()}-${name}`;
    return { path: stored, name, size: 0, workspace_path: mockWorkspacePath(stored) };
  }

  return undefined;
};

/**
 * One usage aggregate, in the shape `_admin_usage_section` returns
 * (`src/istota/web_app.py`). The dashboard reads every field, so a fixture has
 * to carry all of them; the totals and the cache-hit rate are derived here
 * rather than restated, so a hand-edited row cannot show a total that
 * disagrees with its own parts.
 */
function mockUsageTotals(o: {
  rows: number;
  measured?: number;
  input: number;
  cacheRead: number;
  cacheWrite: number;
  output: number;
  cost: number;
  initialContext?: number | null;
  peakContext?: number | null;
  contextRows?: number;
}) {
  const total = o.input + o.cacheRead + o.cacheWrite + o.output;
  return {
    rows: o.rows,
    measured_rows: o.measured ?? o.rows,
    billed_input_tokens: o.input,
    cache_read_tokens: o.cacheRead,
    cache_write_tokens: o.cacheWrite,
    output_tokens: o.output,
    total_tokens: total,
    cache_hit_rate: Math.round((o.cacheRead / total) * 10_000) / 10_000,
    // Keyed by basis, never a scalar. This deployment bills through the API,
    // so there is one key.
    cost_by_basis: { api: o.cost },
    avg_initial_context_tokens: o.initialContext ?? null,
    avg_peak_context_tokens: o.peakContext ?? null,
    context_rows: o.contextRows ?? 0,
  };
}

const mockAdminStats = {
  system: {
    version: '0.40.1',
    uptime_seconds: 345600,
    db_size_bytes: 119447552,
    python_version: '3.12.3',
    last_scheduler_run: new Date(Date.now() - 30_000).toISOString(),
    scheduler_healthy: true,
  },
  users: [
    {
      username: 'carol',
      display_name: 'Carol',
      is_admin: true,
      tasks_total: 1_284_507,
      tasks_last_24h: 1734,
      tasks_avg_per_day: 373.7,
      tasks_by_source_24h: {
        talk: { count: 28, failed: 1, avg_duration_seconds: 18.4 },
        email: { count: 4, failed: 0, avg_duration_seconds: 22.1 },
        scheduled: { count: 1700, failed: 3, avg_duration_seconds: 1.2 },
        briefing: { count: 2, failed: 0, avg_duration_seconds: 35.0 },
      },
      tasks_interactive_24h: 32,
      tasks_automated_24h: 1702,
      tasks_failed_24h: 4,
      last_active: new Date(Date.now() - 60_000).toISOString(),
      usage_tokens_24h: 4_182_663,
      usage_tokens_30d: 96_405_118,
      usage_cost_24h: { api: 12.47 },
      usage_cost_30d: { api: 288.9 },
      usage_by_origin_24h: {
        task: { rows: 1734, tokens: 3_901_204 },
        sleep_cycle: { rows: 1, tokens: 221_459 },
        health_ocr: { rows: 9, tokens: 60_000 },
      },
      usage_avg_initial_context: 41_820,
      usage_avg_peak_context: 96_140,
      usage_cache_hit_rate_24h: 0.81,
      usage_rows_24h: 1744,
      usage_unmeasured_24h: 3,
    },
    {
      username: 'kasia',
      display_name: 'Kasia',
      is_admin: false,
      tasks_total: 891,
      tasks_last_24h: 77,
      tasks_avg_per_day: 6.2,
      tasks_by_source_24h: {
        talk: { count: 4, failed: 0, avg_duration_seconds: 19.0 },
        scheduled: { count: 73, failed: 0, avg_duration_seconds: 1.1 },
      },
      tasks_interactive_24h: 4,
      tasks_automated_24h: 73,
      tasks_failed_24h: 0,
      last_active: new Date(Date.now() - 3600_000).toISOString(),
      usage_tokens_24h: 188_204,
      usage_tokens_30d: 5_112_880,
      usage_cost_24h: { api: 0.31 },
      usage_cost_30d: { api: 7.44 },
      usage_by_origin_24h: {
        task: { rows: 77, tokens: 188_204 },
      },
      usage_avg_initial_context: 22_410,
      usage_avg_peak_context: 48_900,
      usage_cache_hit_rate_24h: 0.64,
      usage_rows_24h: 77,
      usage_unmeasured_24h: 0,
    },
  ],
  usage: {
    // Sums to the two users above: 4_182_663 + 188_204 tokens over 24h,
    // 96_405_118 + 5_112_880 over 30 days.
    totals_24h: mockUsageTotals({
      rows: 1821,
      measured: 1818,
      input: 812_440,
      cacheRead: 3_101_006,
      cacheWrite: 218_004,
      output: 239_417,
      cost: 12.78,
      initialContext: 39_120,
      peakContext: 91_004,
      contextRows: 1760,
    }),
    totals_30d: mockUsageTotals({
      rows: 52_140,
      measured: 52_002,
      input: 18_880_400,
      cacheRead: 72_004_112,
      cacheWrite: 5_120_440,
      output: 5_513_046,
      cost: 296.34,
      initialContext: 37_880,
      peakContext: 88_412,
      contextRows: 50_990,
    }),
    by_model_30d: [
      {
        key: 'claude-opus-4-8',
        ...mockUsageTotals({
          rows: 21_004,
          input: 9_440_100,
          cacheRead: 38_002_006,
          cacheWrite: 2_610_220,
          output: 3_014_552,
          cost: 214.9,
          initialContext: 44_120,
          peakContext: 99_408,
          contextRows: 20_880,
        }),
      },
      {
        key: 'claude-sonnet-4-6',
        ...mockUsageTotals({
          rows: 24_880,
          input: 7_112_300,
          cacheRead: 28_440_006,
          cacheWrite: 2_004_120,
          output: 2_006_444,
          cost: 68.2,
          initialContext: 33_004,
          peakContext: 79_220,
          contextRows: 24_440,
        }),
      },
      {
        key: 'claude-haiku-4-5',
        ...mockUsageTotals({
          rows: 6256,
          input: 2_328_000,
          cacheRead: 5_562_100,
          cacheWrite: 506_100,
          output: 492_050,
          cost: 13.24,
          initialContext: 21_440,
          peakContext: 48_006,
          contextRows: 5670,
        }),
      },
    ],
    // Nothing omitted: the pane caps the list at five and there are three.
    by_model_30d_omitted: 0,
    by_brain_30d: [
      {
        key: 'native',
        ...mockUsageTotals({
          rows: 44_120,
          input: 16_004_400,
          cacheRead: 61_220_112,
          cacheWrite: 4_340_440,
          output: 4_712_046,
          cost: 251.8,
          initialContext: 38_440,
          peakContext: 90_112,
          contextRows: 43_990,
        }),
      },
      {
        key: 'claude_code',
        ...mockUsageTotals({
          rows: 8020,
          input: 2_876_000,
          cacheRead: 10_784_000,
          cacheWrite: 780_000,
          output: 801_000,
          cost: 44.54,
          // The CLI's non-streaming path emits no context frames.
          initialContext: null,
          peakContext: null,
        }),
      },
    ],
    by_origin_24h: [
      {
        key: 'task',
        ...mockUsageTotals({
          rows: 1811,
          input: 780_440,
          cacheRead: 2_990_006,
          cacheWrite: 210_004,
          output: 229_417,
          cost: 12.11,
          initialContext: 39_120,
          peakContext: 91_004,
          contextRows: 1760,
        }),
      },
      {
        key: 'sleep_cycle',
        ...mockUsageTotals({
          rows: 1,
          input: 22_000,
          cacheRead: 88_000,
          cacheWrite: 6000,
          output: 6459,
          cost: 0.41,
        }),
      },
      {
        key: 'health_ocr',
        ...mockUsageTotals({
          rows: 9,
          input: 10_000,
          cacheRead: 23_000,
          cacheWrite: 2000,
          output: 3541,
          cost: 0.26,
        }),
      },
    ],
    // The two honesty counters: tasks that spent tokens and wrote no row, and
    // task-origin rows that recorded no context size.
    unmeasured_tasks_24h: 3,
    context_unmeasured_rows_30d: 1150,
  },
  // Claude Code plan utilization. Three windows spanning the three tints the
  // card can draw against the warn/high pair below it — green, amber, red — so
  // `VITE_MOCK_API=1 npm run dev` exercises all of them at once rather than
  // whichever one the day's real reading happens to fall in. `spend.enabled` is
  // true for the same reason: the extra-usage line is otherwise unreachable in
  // dev, and the capture it is modelled on has it off. The card's other three
  // states are reachable through `VITE_MOCK_SUBSCRIPTION` — see
  // `mockSubscriptionState` below.
  subscription: {
    available: true,
    windows: [
      {
        key: 'session',
        label: '5-hour',
        percent: 40,
        resets_at: new Date(Date.now() + 3847_000).toISOString(),
        resets_in_seconds: 3847,
        severity: 'normal',
        is_active: true,
      },
      {
        key: 'weekly_all',
        label: 'Weekly (all models)',
        percent: 86.4,
        resets_at: new Date(Date.now() + 6 * 86400_000).toISOString(),
        resets_in_seconds: 6 * 86400,
        severity: 'normal',
        is_active: false,
      },
      {
        key: 'weekly_scoped:fable',
        label: 'Weekly (Fable)',
        percent: 97,
        // A live window with no scheduled reset — the sub-line says so rather
        // than rendering an empty second row.
        resets_at: null,
        resets_in_seconds: null,
        severity: 'normal',
        is_active: false,
      },
    ],
    spend: {
      enabled: true,
      used_minor: 465,
      limit_minor: 2000,
      currency: 'USD',
      exponent: 2,
      percent: 23.25,
    },
    fetched_at: new Date(Date.now() - 40_000).toISOString(),
    stale: false,
    token_source: 'env',
    // The operator's own thresholds, which the card tints by. Left at the
    // shipping defaults here; changing them in the TOML must change the colours
    // without a frontend edit, which is why they are on the wire at all.
    warn_percent: 80,
    high_percent: 95,
    error: '',
  },
  scheduler: {
    jobs_total: 6,
    jobs_active: 4,
    jobs_paused: 2,
    jobs: [
      {
        id: 1,
        user_id: 'carol',
        name: 'morning briefing',
        cron: '0 7 * * *',
        enabled: true,
        auto_disabled_at: null,
        last_run_at: new Date(Date.now() - 6 * 3600_000).toISOString(),
        last_success_at: new Date(Date.now() - 6 * 3600_000).toISOString(),
        consecutive_failures: 0,
        last_error: null,
      },
      {
        id: 2,
        user_id: 'carol',
        name: '_module.feeds.run_scheduled',
        cron: '*/5 * * * *',
        enabled: true,
        auto_disabled_at: null,
        last_run_at: new Date(Date.now() - 3 * 60_000).toISOString(),
        last_success_at: new Date(Date.now() - 3 * 60_000).toISOString(),
        consecutive_failures: 0,
        last_error: null,
      },
      {
        id: 3,
        user_id: 'kasia',
        name: '_module.feeds.run_scheduled',
        cron: '*/5 * * * *',
        enabled: true,
        auto_disabled_at: null,
        last_run_at: new Date(Date.now() - 3 * 60_000).toISOString(),
        last_success_at: new Date(Date.now() - 3 * 60_000).toISOString(),
        consecutive_failures: 0,
        last_error: null,
      },
      {
        id: 4,
        user_id: 'carol',
        name: '_module.money.run_scheduled',
        cron: '*/30 * * * *',
        enabled: true,
        auto_disabled_at: null,
        last_run_at: new Date(Date.now() - 12 * 60_000).toISOString(),
        last_success_at: new Date(Date.now() - 27 * 60_000).toISOString(),
        consecutive_failures: 1,
        last_error: 'timeout after 30s',
      },
      {
        id: 6,
        user_id: 'carol',
        name: 'weekly roundup',
        cron: '0 18 * * 5',
        // Switched off by the user in CRON.md, which reads differently from
        // the suspended row below and must stay in the mock as its own case.
        enabled: false,
        auto_disabled_at: null,
        last_run_at: new Date(Date.now() - 5 * 24 * 3600_000).toISOString(),
        last_success_at: new Date(Date.now() - 5 * 24 * 3600_000).toISOString(),
        consecutive_failures: 0,
        last_error: null,
      },
      {
        id: 5,
        user_id: 'carol',
        name: 'evening recap',
        cron: '0 21 * * *',
        enabled: true,
        auto_disabled_at: new Date(Date.now() - 30 * 3600_000).toISOString(),
        last_run_at: new Date(Date.now() - 36 * 3600_000).toISOString(),
        last_success_at: null,
        consecutive_failures: 5,
        last_error: 'API Error: 429 rate_limited',
      },
    ],
    last_errors: [
      {
        job_name: 'carol/feeds.poll',
        error: 'timeout after 30s',
        timestamp: new Date(Date.now() - 12 * 60_000).toISOString(),
      },
    ],
  },
  modules: {
    feeds: {
      backend: 'native',
      users_configured: 1,
      users_resolved: 1,
      feeds_total: 129,
      entries_total: 48201,
      entries_unread: 342,
      last_poll: new Date(Date.now() - 5 * 60_000).toISOString(),
      poll_errors_24h: 2,
    },
    money: { users_configured: 1 },
    location: {
      visits_total: 1204,
      places_total: 47,
      last_update: new Date(Date.now() - 90 * 60_000).toISOString(),
    },
  },
  tasks: {
    total: 12172,
    last_24h: 1811,
    avg_per_day_30d: 379.8,
    by_source: { talk: 32, email: 4, scheduled: 1773, briefing: 2 },
    failed_by_source_24h: { talk: 1, scheduled: 3 },
    avg_duration_seconds: 4.79,
    error_rate_24h: 0.0022,
    failed_24h: 4,
    interactive_24h: 36,
    automated_24h: 1775,
    interactive_avg_per_day_30d: 38.1,
    automated_avg_per_day_30d: 341.7,
  },
  storage: {
    db_size_bytes: 119447552,
    backups_count: 14,
    last_backup: new Date(Date.now() - 18 * 3600_000).toISOString(),
    // Mock represents a standalone (local) install — no Nextcloud, so the
    // mount row is hidden in the UI.
    nextcloud_configured: false,
    nextcloud_mount_healthy: false,
  },
  runtime: {
    mode: 'standalone',
    caveats: [
      {
        title: 'No sandbox isolation',
        detail:
          "The agent runs with your user account's full privileges. Only give this instance content and instructions you trust.",
      },
      {
        title: 'No Nextcloud',
        detail:
          'The workspace is a local folder (~/.istota); file sharing and CalDAV-from-Nextcloud are unavailable.',
      },
      {
        title: 'Email polling is off',
        detail: 'Inbound/outbound email is disabled.',
      },
    ],
  },
  models: {
    brain_kind: 'native',
    default_model: 'claude-sonnet-4-6',
    default_effort: 'high',
    roles: [
      { role: 'fast', resolved: 'claude-haiku-4-5' },
      { role: 'general', resolved: 'claude-sonnet-4-6' },
      { role: 'smart', resolved: 'claude-opus-4-8' },
    ],
    endpoint: 'https://api.anthropic.com/v1',
    provider: 'openai_compat',
    source_type_overrides: { scheduled: 'native', heartbeat: 'native' },
  },
} satisfies AdminStats;

// Mock reader dataset — populated below so the dev UI has scrollable content.

interface MockFeedSource {
  id: number;
  title: string;
  site_url: string;
  category: { id: number; title: string };
}

interface MockEntry {
  id: number;
  title: string;
  url: string;
  content: string;
  images: string[];
  duplicate_image_count: number;
  embed_url: string;
  file_url: string;
  media_url: string;
  media_type: string;
  feed: MockFeedSource;
  status: 'read' | 'unread';
  starred: boolean;
  starred_at: string;
  published_at: string;
  created_at: string;
}

const DEFAULT_READER_FEEDS: MockFeedSource[] = [
  {
    id: 1,
    title: 'Hacker News',
    site_url: 'https://news.ycombinator.com',
    category: { id: 1, title: 'Blogs' },
  },
  {
    id: 2,
    title: 'The Verge',
    site_url: 'https://www.theverge.com',
    category: { id: 1, title: 'Blogs' },
  },
  {
    id: 3,
    title: 'Daring Fireball',
    site_url: 'https://daringfireball.net',
    category: { id: 1, title: 'Blogs' },
  },
  {
    id: 4,
    title: 'Nemfrog',
    site_url: 'https://nemfrog.tumblr.com',
    category: { id: 2, title: 'Tumblr' },
  },
  {
    id: 5,
    title: 'Cats in a channel',
    site_url: 'https://are.na/cats',
    category: { id: 3, title: 'Are.na' },
  },
];

// When ``scripts/dev/seed_feed_preview.py`` has run, it drops real, freshly
// polled feed data next to this file. Prefer it so the dev UI renders actual
// entries (exercising the poller's image-dedup / hero-strip) instead of the
// synthetic filler below. Absent/invalid → fall back to the generator.
function loadSeededReaderData(): { feeds: MockFeedSource[]; entries: MockEntry[] } | null {
  try {
    const raw = readFileSync(resolve(__mockDir, 'dev-feed-data.json'), 'utf8');
    const data = JSON.parse(raw);
    if (Array.isArray(data?.feeds) && Array.isArray(data?.entries) && data.entries.length) {
      return data;
    }
  } catch {
    // not seeded — fine, use the synthetic dataset
  }
  return null;
}

const _seededReaderData = loadSeededReaderData();
const mockReaderFeeds: MockFeedSource[] = _seededReaderData?.feeds ?? DEFAULT_READER_FEEDS;

const sampleTitles = [
  'A small note on cache invalidation',
  'The unreasonable effectiveness of plain text',
  'Why list views still matter in 2026',
  'Notes from a week of dogfooding',
  'On the quiet joy of finishing things',
  'A case against premature abstraction',
  'Latency budgets, revisited',
  'How I learned to stop worrying and love SQLite',
  'Tiny tools beat platforms',
  'The room where it scrolls',
  'Mid-year reading list',
  'On naming things',
  'Drafts: an underrated feature',
  'Calm software in an anxious year',
  'The browser is the OS',
  'Three weeks with the new keyboard',
  'A short rant about modal dialogs',
  'Re-reading old code',
  'The case for progressive enhancement',
  'Sundays are for refactoring',
];

const sampleSnippets = [
  'A few thoughts I jotted down on the train this morning. Nothing groundbreaking, just a small observation that turned into something I keep thinking about.',
  'I have been reorganizing my notes and noticed a pattern I had not seen before. Sharing it here in case it is useful to someone else doing the same thing.',
  'There is a particular kind of mistake I keep making, and I want to write it down so I stop making it. Maybe writing helps. Maybe it does not.',
  'After a year of using this tool every day, here is what I would change. None of it is dramatic. Most of it is small. That is sort of the point.',
  'Quick demo of a thing I built last weekend. Probably not useful for anyone else, but it scratched an itch I had had for a while.',
];

function pad(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

function generateMockEntries(): MockEntry[] {
  const entries: MockEntry[] = [];
  const baseTime = Date.now();
  // 30 days back, two entries per hour-ish on average — enough to scroll.
  const total = 180;
  for (let i = 0; i < total; i++) {
    const feed = mockReaderFeeds[i % mockReaderFeeds.length];
    // Spread over ~30 days; published earlier than created by a small jitter
    // so the two sort orders produce visibly different results.
    const publishedAt = new Date(baseTime - i * 3.5 * 60 * 60 * 1000);
    const createdAt = new Date(publishedAt.getTime() + ((i * 17) % 41) * 60 * 1000);

    // Mix: every 3rd entry has 1 image, every 7th has a gallery, rest are text.
    const isGallery = i % 7 === 3;
    const isImage = !isGallery && i % 3 === 0;
    const images: string[] = [];
    if (isGallery) {
      // Vary gallery size: 2, 3, 4, 6 — covers all layout branches.
      const sizes = [2, 3, 4, 6];
      const galSize = sizes[Math.floor(i / 7) % sizes.length];
      for (let g = 0; g < galSize; g++) {
        images.push(`https://picsum.photos/seed/feed-${i}-${g}/600/600`);
      }
    } else if (isImage) {
      images.push(`https://picsum.photos/seed/feed-${i}/800/500`);
    }

    // Every 8th image post stands in for a reblog whose picture the server
    // suppressed as a recent repeat — one of them loses its only image, so
    // both the badge and the image-less-image-post case are visible in dev.
    const duplicateImageCount = images.length > 0 && i % 8 === 4 ? 1 : 0;
    if (duplicateImageCount && images.length === 1) {
      images.length = 0;
    }

    const title = `${sampleTitles[i % sampleTitles.length]} (#${total - i})`;
    const snippet = sampleSnippets[i % sampleSnippets.length];

    let mediaUrl = '';
    let mediaType = '';
    if (i % 10 === 5) {
      mediaUrl =
        i % 20 === 5
          ? 'https://download.samplelib.com/mp4/sample-5s.mp4'
          : 'https://download.samplelib.com/mp4/sample-10s.mp4';
      mediaType = 'video/mp4';
    } else if (i % 17 === 8) {
      mediaUrl = 'https://download.samplelib.com/mp3/sample-3s.mp3';
      mediaType = 'audio/mpeg';
    }
    // A clip that also came with a still exercises the poster path.
    if (mediaType.startsWith('video/') && i % 30 === 5) {
      images.length = 0;
      images.push(`https://picsum.photos/seed/feed-${i}-poster/800/500`);
    }

    entries.push({
      id: i + 1,
      title,
      url: `${feed.site_url}/posts/${i + 1}`,
      content: `<p>${snippet}</p><p>This is mock content number ${i + 1}, served by the dev mock API.</p>`,
      images,
      duplicate_image_count: duplicateImageCount,
      // Every 9th post stands in for an Are.na Embed block, so the play
      // affordance and the inline player are reachable in dev.
      embed_url: i % 9 === 3 ? 'https://www.youtube.com/watch?v=B0sO1wdBhMY' : '',
      // Every 13th post stands in for an Are.na Attachment, so the document
      // card and its format badge are reachable in dev.
      file_url: i % 13 === 6 ? 'https://attachments.are.na/1/essay.pdf' : '',
      // Every 10th post stands in for a Mastodon video attachment and every
      // 17th for a podcast enclosure, so both native players are reachable in
      // dev (ISSUE-356). The clips are third-party samples, so they need a
      // working connection and their aspect ratios are whatever the host
      // serves — the sizing rules themselves are pinned by CSS assertions in
      // FeedCard.video.test.ts rather than by looking at these.
      media_url: mediaUrl,
      media_type: mediaType,
      feed,
      // First ~25% unread, rest read — gives the Unseen filter something to do.
      status: i < total * 0.25 ? 'unread' : 'read',
      starred: i % 11 === 0,
      starred_at: i % 11 === 0 ? createdAt.toISOString() : '',
      published_at: publishedAt.toISOString(),
      created_at: createdAt.toISOString(),
    });
  }
  return entries;
}

const mockReaderEntries: MockEntry[] = _seededReaderData?.entries ?? generateMockEntries();

function feedsListResponse(params: URLSearchParams): {
  feeds: MockFeedSource[];
  entries: MockEntry[];
  total: number;
} {
  const limit = Math.max(1, Math.min(500, Number(params.get('limit')) || 50));
  const offset = Math.max(0, Number(params.get('offset')) || 0);
  const before = params.get('before');
  const order = params.get('order') === 'created_at' ? 'created_at' : 'published_at';
  const feedId = params.get('feed_id') ? Number(params.get('feed_id')) : 0;
  const categoryId = params.get('category_id') ? Number(params.get('category_id')) : 0;
  const statusFilter = params.get('status'); // 'unread' | null
  const starredOnly = params.get('starred') === '1';

  let pool = mockReaderEntries;
  if (feedId) pool = pool.filter((e) => e.feed.id === feedId);
  if (categoryId) pool = pool.filter((e) => e.feed.category.id === categoryId);
  if (statusFilter === 'unread') pool = pool.filter((e) => e.status !== 'read');
  if (starredOnly) pool = pool.filter((e) => e.starred);

  pool = [...pool].sort((a, b) => {
    const av = order === 'created_at' ? a.created_at : a.published_at;
    const bv = order === 'created_at' ? b.created_at : b.published_at;
    return bv.localeCompare(av); // desc
  });

  const total = pool.length;

  if (before) {
    const cutoffSec = Number(before);
    pool = pool.filter((e) => {
      const v = order === 'created_at' ? e.created_at : e.published_at;
      return Math.floor(new Date(v).getTime() / 1000) < cutoffSec;
    });
  }

  const slice = pool.slice(offset, offset + limit);
  return { feeds: mockReaderFeeds, entries: slice, total };
}

interface MockFeed {
  url: string;
  title?: string;
  category?: string;
  poll_interval_minutes?: number;
}
interface MockCategory {
  slug: string;
  title?: string;
}
const mockFeedsConfig: {
  settings: { default_poll_interval_minutes?: number };
  categories: MockCategory[];
  feeds: MockFeed[];
} = {
  settings: { default_poll_interval_minutes: 30 },
  categories: [
    { slug: 'blogs', title: 'Blogs' },
    { slug: 'tumblr', title: 'Tumblr' },
    { slug: 'arena', title: 'Are.na' },
  ],
  feeds: [
    { url: 'https://example.com/feed.xml', title: 'Example Blog', category: 'blogs' },
    { url: 'tumblr:nemfrog', title: 'Nemfrog', category: 'tumblr' },
    { url: 'arena:cats-in-a-channel', category: 'arena', poll_interval_minutes: 60 },
  ],
};

function feedsConfigResponse() {
  const now = new Date().toISOString();
  return {
    config: mockFeedsConfig,
    diagnostics: {
      total_feeds: mockFeedsConfig.feeds.length,
      total_entries: 42,
      unread_entries: 7,
      error_feeds: 0,
      last_poll_at: now,
    },
    feed_state: mockFeedsConfig.feeds.map((f) => ({
      url: f.url,
      last_fetched_at: now,
      last_error: null,
      error_count: 0,
    })),
  };
}

interface MockPlace {
  id: number;
  name: string;
  lat: number;
  lon: number;
  radius_meters: number;
  category: string;
  notes: string;
}

const mockPlaces: { places: MockPlace[] } = {
  places: [
    {
      id: 1,
      name: 'Home',
      lat: 52.52,
      lon: 13.405,
      radius_meters: 80,
      category: 'home',
      notes: '',
    },
    {
      id: 2,
      name: 'Office',
      lat: 52.5074,
      lon: 13.3904,
      radius_meters: 60,
      category: 'work',
      notes: '',
    },
    {
      id: 3,
      name: 'Berghain Boiler Room (Side Entrance)',
      lat: 52.5111,
      lon: 13.443,
      radius_meters: 50,
      category: 'social',
      notes: '',
    },
    {
      id: 4,
      name: 'Climbing Gym',
      lat: 52.53,
      lon: 13.415,
      radius_meters: 40,
      category: 'gym',
      notes: '',
    },
    {
      id: 5,
      name: 'Sunday Farmers Market on Maybachufer',
      lat: 52.492,
      lon: 13.428,
      radius_meters: 75,
      category: 'shopping',
      notes: '',
    },
    {
      id: 6,
      name: 'Pizza Place',
      lat: 52.518,
      lon: 13.41,
      radius_meters: 30,
      category: 'food',
      notes: '',
    },
    {
      id: 7,
      name: "Mom's",
      lat: 52.54,
      lon: 13.45,
      radius_meters: 100,
      category: 'family',
      notes: '',
    },
    {
      id: 8,
      name: 'Co-working Spot',
      lat: 52.505,
      lon: 13.385,
      radius_meters: 45,
      category: 'work',
      notes: '',
    },
    {
      id: 9,
      name: 'Dentist',
      lat: 52.526,
      lon: 13.402,
      radius_meters: 35,
      category: 'medical',
      notes: '',
    },
    {
      id: 10,
      name: 'Café around the corner with the wifi password on the wall',
      lat: 52.521,
      lon: 13.408,
      radius_meters: 30,
      category: 'food',
      notes: '',
    },
    {
      id: 11,
      name: 'Hotel Adlon',
      lat: 52.5163,
      lon: 13.3789,
      radius_meters: 50,
      category: 'hotel',
      notes: '',
    },
    {
      id: 12,
      name: 'Friend Anna',
      lat: 52.535,
      lon: 13.42,
      radius_meters: 80,
      category: 'friend',
      notes: '',
    },
  ],
};

interface MockDismissed {
  id: number;
  lat: number;
  lon: number;
  radius_meters: number;
  dismissed_at: string;
}
const mockDismissed: { dismissed: MockDismissed[] } = {
  dismissed: [
    { id: 1, lat: 52.5, lon: 13.45, radius_meters: 120, dismissed_at: '2026-04-10T00:00:00Z' },
  ],
};

interface MockCluster {
  lat: number;
  lon: number;
  radius_meters: number;
  total_pings: number;
  first_seen: string;
  last_seen: string;
}
const mockDiscover: { clusters: MockCluster[] } = {
  clusters: [
    {
      lat: 52.5235,
      lon: 13.4115,
      radius_meters: 60,
      total_pings: 42,
      first_seen: '2026-04-15T08:00:00Z',
      last_seen: '2026-04-25T19:30:00Z',
    },
    {
      lat: 52.498,
      lon: 13.438,
      radius_meters: 90,
      total_pings: 18,
      first_seen: '2026-04-20T12:00:00Z',
      last_seen: '2026-04-26T11:00:00Z',
    },
    {
      lat: 52.532,
      lon: 13.395,
      radius_meters: 45,
      total_pings: 11,
      first_seen: '2026-04-22T17:00:00Z',
      last_seen: '2026-04-25T22:00:00Z',
    },
  ],
};

const today = new Date().toISOString().slice(0, 10);
const mockPings = (() => {
  const pings: any[] = [];
  // Berlin morning, continuous tracking: 60 pings 1 min apart, 08:00-08:59.
  // Tight spacing keeps each edge under the dwell-gap threshold so this
  // stretch renders as the solid speed-coloured activity line.
  const berlinLat = 52.52;
  const berlinLon = 13.405;
  for (let i = 0; i < 60; i++) {
    const t = new Date();
    t.setHours(8, i, 0, 0);
    const stationary = i < 15;
    pings.push({
      timestamp: t.toISOString(),
      lat: berlinLat + Math.sin(i / 18) * 0.004 + i * 0.00012,
      lon: berlinLon + Math.cos(i / 18) * 0.004 + i * 0.00018,
      horizontal_accuracy: 15,
      // Berlin is flat: metres of GPS wander, well under the strip's floor, so
      // this stretch alone draws no elevation profile.
      altitude: 38 + Math.sin(i / 5) * 4,
      activity_type: stationary ? 'stationary' : i < 35 ? 'walking' : 'in_vehicle',
      speed: stationary ? 0 : i < 35 ? 1.2 : 8.5,
      place: stationary ? 'Home' : null,
      place_id: stationary ? 1 : null,
    });
  }
  // ~14h transatlantic flight gap: next ping is in LA at 23:00 UTC.
  // Berlin → LAX is ~9,300 km; the implied speed easily exceeds the gap
  // threshold so this edge renders as the coral great-circle arc.
  // LA pings are 6 min apart, matching Overland's significant-location-change
  // mode, so each LA→LA edge crosses the dwell threshold and renders as the
  // muted sparse-sample dash.
  const laxLat = 33.9425;
  const laxLon = -118.4081;
  for (let i = 0; i < 30; i++) {
    const t = new Date();
    t.setHours(23 + Math.floor(i / 10), (i % 10) * 6, 0, 0);
    pings.push({
      timestamp: t.toISOString(),
      lat: laxLat + Math.sin(i / 6) * 0.008 + i * 0.0003,
      lon: laxLon + Math.cos(i / 6) * 0.008 + i * 0.0004,
      horizontal_accuracy: 18,
      // A drive up into the hills and back down, so the elevation strip has
      // something real to draw. Every fifth point has no vertical fix, which is
      // roughly the rate iOS produces and exercises the null path.
      altitude: i % 5 === 4 ? null : 30 + Math.sin((i / 29) * Math.PI) * 420,
      activity_type: i < 5 ? 'stationary' : i < 20 ? 'in_vehicle' : 'walking',
      speed: i < 5 ? 0 : i < 20 ? 12.5 : 1.4,
      place: null,
      place_id: null,
    });
  }
  return { pings, count: pings.length };
})();
const mockDay = {
  date: today,
  timezone: 'Europe/Berlin',
  ping_count: 50,
  transit_pings: 20,
  stops: [
    {
      lat: 52.52,
      lon: 13.405,
      name: 'Home',
      start_time: `${today}T07:00:00Z`,
      end_time: `${today}T08:30:00Z`,
      duration_min: 90,
      ping_count: 10,
    },
    {
      lat: 52.5074,
      lon: 13.3904,
      name: 'Office',
      start_time: `${today}T09:00:00Z`,
      end_time: `${today}T17:00:00Z`,
      duration_min: 480,
      ping_count: 30,
    },
  ],
};
// Field names follow the real `/location/current` payload — this block used to
// invent `recorded_at` / `horizontal_accuracy`, which match no LocationPing, so
// nothing on this path could be developed against the mock.
const mockCurrent = {
  last_ping: {
    timestamp: new Date().toISOString(),
    lat: 52.52,
    lon: 13.405,
    altitude: 38,
    accuracy: 12,
  },
  current_visit: { place: 'Home', place_id: 1, started_at: `${today}T07:00:00Z` },
};

const ledgers = { ledgers: ['main', 'business'] };
const checkResp = { error_count: 0, errors: [] };
const accountsResp = {
  accounts: [
    { account: 'Assets:Checking', balance: '0.00 USD' },
    { account: 'Assets:Savings', balance: '0.00 USD' },
    { account: 'Expenses:Food', balance: '0.00 USD' },
    { account: 'Income:Salary', balance: '0.00 USD' },
  ],
};

let nextPlaceId = mockPlaces.places.length + 1;
let nextDismissedId = mockDismissed.dismissed.length + 1;

// Approximate distance between two coords in meters (sufficient for nearby clustering checks).
function distMeters(a: { lat: number; lon: number }, b: { lat: number; lon: number }): number {
  const R = 6371000;
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(b.lat - a.lat);
  const dLon = toRad(b.lon - a.lon);
  const lat1 = toRad(a.lat);
  const lat2 = toRad(b.lat);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

function dropClusterNear(point: { lat: number; lon: number }, radius: number): void {
  mockDiscover.clusters = mockDiscover.clusters.filter(
    (c) => distMeters(c, point) > Math.max(radius, c.radius_meters),
  );
}

// Phase 5 — settings/secrets mock state. Plaintext values stay only in memory;
// the real backend never returns them.
const mockSecrets: Record<string, Record<string, string>> = {
  karakeep: {},
  google_workspace: {},
  // Seeded half-configured, because that is the state the ntfy card is
  // actually met in — a topic set, the optional auth fields empty — and it
  // is the only state in which a row with a clear button and a row without
  // one appear together, which is what the field widths have to survive.
  ntfy: { topic: 'mock-topic' },
  monarch: {},
  feeds: {},
  overland: {},
};

interface ServiceSchema {
  service: string;
  label: string;
  fields: { key: string; label: string; type: string }[];
  used_by?: string[];
  oauth?: boolean;
  custom_ui?: boolean;
}

const _CONNECTED_SCHEMAS: ServiceSchema[] = [
  {
    service: 'karakeep',
    label: 'Karakeep',
    used_by: ['bookmarks'],
    fields: [
      { key: 'base_url', label: 'Base URL', type: 'url' },
      { key: 'api_key', label: 'API key', type: 'password' },
    ],
  },
  {
    service: 'google_workspace',
    label: 'Google Workspace',
    used_by: ['google_workspace'],
    oauth: true,
    // Bespoke card (GoogleWorkspaceCard): granted scopes + a per-service
    // read-only/full picker bounded by the operator's ceiling.
    custom_ui: true,
    fields: [],
  },
  // Mirrors secret_schema.py's `ntfy` entry, which the mock had been missing
  // altogether — so the card with the most fields, and the only one mixing
  // required and optional ones, could not be seen in dev at all.
  {
    service: 'ntfy',
    label: 'ntfy push',
    used_by: ['heartbeat', 'scheduler'],
    fields: [
      { key: 'server_url', label: 'Server URL', type: 'url' },
      { key: 'topic', label: 'Default topic', type: 'text' },
      { key: 'token', label: 'Access token (optional)', type: 'password' },
      { key: 'username', label: 'Username (optional)', type: 'text' },
      { key: 'password', label: 'Password (optional)', type: 'password' },
    ],
  },
];

// --- Google Workspace scope picker (ISSUE-240) ---
//
// Mirrors istota/google_scopes.py. The instance ceiling below is deliberately
// mixed — Drive full, Gmail/Calendar read-only, Sheets/Docs/Chat absent — so
// the dev card exercises all three states the real one has to keep apart.

const GOOGLE_SERVICES = [
  { service: 'drive', label: 'Drive', max_level: 'full' },
  { service: 'gmail', label: 'Gmail', max_level: 'readonly' },
  { service: 'calendar', label: 'Calendar', max_level: 'readonly' },
  { service: 'sheets', label: 'Sheets', max_level: 'off' },
  { service: 'docs', label: 'Docs', max_level: 'off' },
  { service: 'chat', label: 'Chat', max_level: 'off' },
] as const;

const GOOGLE_SCOPES: Record<string, { readonly: string[]; full: string[] }> = {
  drive: {
    readonly: ['https://www.googleapis.com/auth/drive.readonly'],
    full: ['https://www.googleapis.com/auth/drive'],
  },
  gmail: {
    readonly: ['https://www.googleapis.com/auth/gmail.readonly'],
    full: ['https://www.googleapis.com/auth/gmail.modify'],
  },
  calendar: {
    readonly: ['https://www.googleapis.com/auth/calendar.readonly'],
    full: ['https://www.googleapis.com/auth/calendar'],
  },
  sheets: {
    readonly: ['https://www.googleapis.com/auth/spreadsheets.readonly'],
    full: ['https://www.googleapis.com/auth/spreadsheets'],
  },
  docs: {
    readonly: ['https://www.googleapis.com/auth/documents.readonly'],
    full: ['https://www.googleapis.com/auth/documents'],
  },
  chat: {
    readonly: [
      'https://www.googleapis.com/auth/chat.spaces.readonly',
      'https://www.googleapis.com/auth/chat.messages.readonly',
    ],
    full: [
      'https://www.googleapis.com/auth/chat.spaces',
      'https://www.googleapis.com/auth/chat.messages',
    ],
  },
};

const LEVEL_RANK: Record<string, number> = { off: 0, readonly: 1, full: 2 };

const GOOGLE_SCOPE_OWNER: Record<string, string> = Object.fromEntries(
  Object.entries(GOOGLE_SCOPES).flatMap(([svc, m]) =>
    [...m.readonly, ...m.full].map((scope) => [scope, svc]),
  ),
);

// The user's stored selection ({} = unset = the whole ceiling), and the scopes
// Google "granted" — seeded narrower than the ceiling so the reconnect-needed
// banner is reachable without touching anything.
const mockGoogle: { selection: Record<string, string>; granted: string[] | null } = {
  selection: {},
  granted: ['https://www.googleapis.com/auth/drive.readonly'],
};

function googleDefaultSelection(): Record<string, string> {
  return Object.fromEntries(GOOGLE_SERVICES.map((s) => [s.service, s.max_level]));
}

function googleResolveScopes(selection: Record<string, string>): string[] {
  const unset = Object.keys(selection).length === 0;
  const out: string[] = [];
  for (const svc of GOOGLE_SERVICES) {
    if (svc.max_level === 'off') continue;
    let want = unset ? svc.max_level : (selection[svc.service] ?? 'off');
    if (LEVEL_RANK[want] > LEVEL_RANK[svc.max_level]) want = svc.max_level;
    if (want !== 'readonly' && want !== 'full') continue;
    out.push(...GOOGLE_SCOPES[svc.service][want]);
  }
  return out;
}

function googleLevels(scopes: string[]): Record<string, string> {
  const levels: Record<string, string> = {};
  for (const svc of GOOGLE_SERVICES) {
    const map = GOOGLE_SCOPES[svc.service];
    if (map.full.some((s) => scopes.includes(s))) levels[svc.service] = 'full';
    else if (map.readonly.some((s) => scopes.includes(s))) levels[svc.service] = 'readonly';
  }
  return levels;
}

// Mirrors google_scopes.missing_scopes: only a STRICTLY higher granted level
// counts as cover, so a partly granted multi-scope service (Chat) still
// reports its shortfall instead of reading as satisfied.
function googleMissing(requested: string[], granted: string[]): string[] {
  const levels = googleLevels(granted);
  return requested.filter((scope) => {
    if (granted.includes(scope)) return false;
    for (const svc of GOOGLE_SERVICES) {
      const map = GOOGLE_SCOPES[svc.service];
      if (map.readonly.includes(scope))
        return LEVEL_RANK[levels[svc.service] ?? 'off'] <= LEVEL_RANK.readonly;
      if (map.full.includes(scope))
        return LEVEL_RANK[levels[svc.service] ?? 'off'] <= LEVEL_RANK.full;
    }
    return true;
  });
}

function mockGoogleStatus() {
  const selection = mockGoogle.selection;
  const granted = mockGoogle.granted;
  const requested = googleResolveScopes(selection);
  const levels = granted ? googleLevels(granted) : {};
  const known = new Set(Object.values(GOOGLE_SCOPES).flatMap((m) => [...m.readonly, ...m.full]));
  return {
    enabled: true,
    connected: granted !== null,
    offered: GOOGLE_SERVICES.map((s) => ({ ...s })),
    granted: GOOGLE_SERVICES.filter((s) => levels[s.service]).map((s) => {
      const level = levels[s.service] as 'readonly' | 'full';
      const wanted = GOOGLE_SCOPES[s.service][level];
      const held = wanted.filter((x) => (granted ?? []).includes(x));
      return {
        service: s.service,
        label: s.label,
        level,
        scopes: held,
        complete: held.length === wanted.length,
        also: (granted ?? []).filter(
          (x) => !wanted.includes(x) && GOOGLE_SCOPE_OWNER[x] === s.service,
        ),
      };
    }),
    unrecognized_scopes: (granted ?? []).filter((s) => !known.has(s)),
    // The mock's ceiling is fully mapped, so this is always empty here; it is
    // emitted anyway so the client renders the same shape either way.
    unoffered_scopes: [],
    selection: Object.keys(selection).length ? selection : googleDefaultSelection(),
    selection_set: Object.keys(selection).length > 0,
    requested_scopes: requested,
    missing_scopes: granted ? googleMissing(requested, granted) : [],
    extra_scopes: granted ? googleMissing(granted, requested) : [],
  };
}

const _MODULE_SCHEMAS: Record<string, ServiceSchema[]> = {
  feeds: [
    {
      service: 'feeds',
      label: 'Feeds (Tumblr)',
      used_by: ['feeds'],
      fields: [{ key: 'tumblr_api_key', label: 'Tumblr API key (optional)', type: 'password' }],
    },
  ],
  money: [
    {
      service: 'monarch',
      label: 'Monarch Money',
      used_by: ['money'],
      fields: [
        { key: 'session_id', label: 'session_id cookie', type: 'password' },
        { key: 'csrftoken', label: 'csrftoken cookie', type: 'password' },
      ],
    },
  ],
  location: [
    {
      service: 'overland',
      label: 'Overland GPS',
      used_by: ['location'],
      fields: [{ key: 'ingest_token', label: 'Ingest token', type: 'password' }],
    },
  ],
};

const MODULE_NAMES = ['feeds', 'money', 'location', 'health', 'briefings'];

function buildServiceCard(s: ServiceSchema) {
  const stored = mockSecrets[s.service] || {};
  const configured_keys = Object.keys(stored).filter((k) => stored[k]);
  if (s.oauth) {
    // OAuth state comes from the token store, not from `secrets` — the real
    // backend reads google_oauth_tokens for exactly this reason.
    const connected = s.service === 'google_workspace' && mockGoogle.granted !== null;
    return {
      ...s,
      status: connected ? 'configured' : 'missing',
      configured_keys,
      last_updated: null,
      connected,
      enabled: true,
    };
  }
  const required = s.fields
    .filter((f) => !f.label.toLowerCase().includes('optional'))
    .map((f) => f.key);
  let status: 'configured' | 'partial' | 'missing' = 'missing';
  if (required.length === 0) {
    status = configured_keys.length > 0 ? 'configured' : 'missing';
  } else if (required.every((k) => configured_keys.includes(k))) {
    status = 'configured';
  } else if (required.some((k) => configured_keys.includes(k))) {
    status = 'partial';
  }
  return { ...s, status, configured_keys, last_updated: null };
}

function mockSettingsServices(): { services: unknown[] } {
  return { services: _CONNECTED_SCHEMAS.map(buildServiceCard) };
}

// disabled_modules lives on the user profile and gates per-module status.
const mockDisabledModules = new Set<string>();

function mockModulesResponse() {
  return {
    modules: MODULE_NAMES,
    disabled: [...mockDisabledModules],
    enabled_for_user: Object.fromEntries(MODULE_NAMES.map((m) => [m, !mockDisabledModules.has(m)])),
  };
}

function mockModuleServices(module: string) {
  const schemas = _MODULE_SCHEMAS[module];
  if (!schemas) return undefined;
  return {
    module,
    module_enabled: !mockDisabledModules.has(module),
    services: schemas.map(buildServiceCard),
  };
}

// ---- Admin logs + configuration (ISSUE-203) ----

const mockLogSources = {
  sources: [
    {
      id: 'app',
      label: 'Application log',
      kind: 'file',
      description:
        'Scheduler, pollers, brain and web output — the rotating file every istota process writes to.',
      available: true,
      detail: '3 files in the rotation chain',
      time_basis: 'server-local',
      path: '/var/log/istota/istota.log',
      bytes: 4_812_004,
      files: 3,
    },
    {
      id: 'tasks',
      label: 'Task lifecycle',
      kind: 'db',
      description:
        'Per-task lifecycle written by the scheduler — claimed, completed, retrying, failed. Includes truncated task output.',
      available: true,
      detail: 'From the task_logs table; pruned with the task retention sweep.',
      time_basis: 'utc',
      path: null,
      bytes: 0,
      files: 0,
    },
  ],
};

const _MOCK_APP_LOG = [
  ['2026-07-31T09:14:02', 'INFO', 'istota.scheduler', 'Task claimed by worker-a1-4411-alice'],
  ['2026-07-31T09:14:03', 'DEBUG', 'istota.executor', 'skills: eager=7 menu=26'],
  ['2026-07-31T09:14:29', 'INFO', 'istota.executor', 'native cache hit_rate=0.81'],
  [
    '2026-07-31T09:14:31',
    'WARNING',
    'istota.brain.tmux_claude',
    'tmux_brain session=t-9f21 outcome=fallback ready_ms=30012 dialogs=0 retries=2',
  ],
  [
    '2026-07-31T09:14:31',
    'ERROR',
    'istota.transport.email',
    'IMAP poll failed\nTraceback (most recent call last):\n  File "inbound.py", line 210, in poll_emails\nTimeoutError: timed out',
  ],
  [
    '2026-07-31T09:15:00',
    'INFO',
    'istota.scheduler',
    'scheduler_stats threads=14 fds=98 rss_mb=412',
  ],
];

function mockLogPage(source: string, params: URLSearchParams) {
  const limit = Number(params.get('limit') ?? 200);
  const level = params.get('level') ?? '';
  const q = (params.get('q') ?? '').toLowerCase();
  const ORDER = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];

  interface MockLogRecord {
    cursor: string;
    timestamp: string;
    level: string;
    logger: string | null;
    message: string;
    task_id: number | null;
    user_id: string | null;
    source_type: string | null;
  }

  let records: MockLogRecord[] =
    source === 'tasks'
      ? [
          {
            cursor: '1',
            timestamp: '2026-07-31T09:14:02',
            level: 'INFO',
            logger: null,
            message: 'Task claimed by worker-a1-4411-alice',
            task_id: 8821,
            user_id: 'alice',
            source_type: 'talk',
          },
          {
            cursor: '2',
            timestamp: '2026-07-31T09:14:44',
            level: 'INFO',
            logger: null,
            message: 'Task completed successfully',
            task_id: 8821,
            user_id: 'alice',
            source_type: 'talk',
          },
          {
            cursor: '3',
            timestamp: '2026-07-31T09:16:10',
            level: 'WARNING',
            logger: null,
            message: 'Task failed, will retry in 4 minutes: API Error: 529 Overloaded',
            task_id: 8822,
            user_id: 'bob',
            source_type: 'briefing',
          },
        ]
      : _MOCK_APP_LOG.map(([timestamp, lvl, logger, message], i) => ({
          cursor: `istota.log:${i * 120}`,
          timestamp,
          level: lvl,
          logger,
          message,
          task_id: null,
          user_id: null,
          source_type: null,
        }));

  if (level) {
    const floor = ORDER.indexOf(level);
    records = records.filter((r) => ORDER.indexOf(r.level) >= floor);
  }
  if (q) {
    records = records.filter(
      (r) => r.message.toLowerCase().includes(q) || (r.logger ?? '').toLowerCase().includes(q),
    );
  }
  const loggerPrefix = params.get('logger');
  if (loggerPrefix) {
    records = records.filter((r) => (r.logger ?? '').startsWith(loggerPrefix));
  }
  const userFilter = params.get('user_id');
  if (userFilter) {
    records = records.filter((r) => r.user_id === userFilter);
  }
  const taskFilter = params.get('task_id');
  if (taskFilter) {
    records = records.filter((r) => String(r.task_id) === taskFilter);
  }

  return {
    records: records.slice(-limit),
    next_before: null,
    tail_cursor: source === 'tasks' ? '3' : 'istota.log:720',
    truncated: false,
  };
}

function mockAdminConfig() {
  const f = (
    key: string,
    value: unknown,
    type: string,
    extra: { secret?: boolean; set?: boolean } = {},
  ) => ({
    key,
    name: key.split('.').pop() as string,
    value: extra.secret ? null : value,
    type: extra.secret ? 'secret' : type,
    secret: extra.secret ?? false,
    set: extra.set ?? (value !== null && value !== '' && value !== undefined),
  });

  return {
    config_path: '/etc/istota/config.toml',
    editable: false,
    sections: [
      {
        key: 'general',
        label: 'General',
        fields: [
          f('bot_name', 'Istota', 'str'),
          f('db_path', '/srv/app/istota/data/istota.db', 'path'),
          f('effort', '', 'str'),
          f('model', 'claude-opus-4-8', 'str'),
          f('namespace', 'istota', 'str'),
          f('users', 2, 'count'),
        ],
      },
      {
        key: 'nextcloud',
        label: '[nextcloud]',
        fields: [
          f('url', 'https://cloud.example.com', 'str'),
          f('username', 'istota', 'str'),
          f('app_password', null, 'secret', { secret: true, set: true }),
        ],
      },
      {
        key: 'logging',
        label: '[logging]',
        fields: [
          f('level', 'INFO', 'str'),
          f('output', 'both', 'str'),
          f('file', '/var/log/istota/istota.log', 'str'),
          f('rotate', true, 'bool'),
          f('max_size_mb', 10, 'int'),
          f('backup_count', 5, 'int'),
        ],
      },
      {
        key: 'web',
        label: '[web]',
        fields: [
          f('auth', 'nextcloud', 'str'),
          f('port', 8766, 'int'),
          f('token_storage', 'encrypted', 'str'),
          f('oauth2_client_secret', null, 'secret', { secret: true, set: true }),
          f('session_secret_key', null, 'secret', { secret: true, set: true }),
        ],
      },
      {
        key: 'brain',
        label: '[brain]',
        fields: [
          f('kind', 'claude_code', 'str'),
          f('fallback', 'native', 'str'),
          f('fallback_on_transient', true, 'bool'),
          f('fallback_cooldown_seconds', 900, 'int'),
        ],
      },
      {
        key: 'brain.native',
        label: '[brain.native]',
        fields: [
          f('provider', 'openai_compat', 'str'),
          f('model', 'anthropic/claude-sonnet-4-6', 'str'),
          f('base_url', 'https://openrouter.ai/api/v1', 'str'),
          f('api_key', null, 'secret', { secret: true, set: false }),
        ],
      },
    ],
  };
}

/**
 * The subscription section, in whichever of its four states was asked for.
 *
 * `VITE_MOCK_SUBSCRIPTION=absent | stale | nospend`, defaulting to the
 * populated reading above. A working deployment produces the non-populated
 * states rarely and on nobody's schedule, so without a switch the only way to
 * look at them is to edit this file — which is exactly the check nobody
 * performs. The component test asserts them; this is for looking at them.
 *
 * `absent` renders no card at all, which is the whole of that state and is
 * easy to mistake for a broken dev server. It replaced an `unavailable` case
 * that built `available: false` with a reason on it: the card used to draw
 * that reason, and now the server omits the key instead.
 */
function mockSubscriptionState() {
  const base = mockAdminStats.subscription;
  switch (process.env.VITE_MOCK_SUBSCRIPTION) {
    case 'absent':
      return undefined;
    case 'stale':
      // Real numbers from an earlier fetch, plus the failure that made them old.
      return {
        ...base,
        stale: true,
        fetched_at: new Date(Date.now() - 2 * 3600_000).toISOString(),
        error: 'HTTP 503 from the usage endpoint',
      };
    case 'nospend':
      return { ...base, spend: { ...base.spend, enabled: false } };
    default:
      return base;
  }
}

// The inbox behind the bell. Three rows, chosen to cover what the panel has to
// render differently: an actionable object-backed item belonging to no room
// (the case the old confirmations banner existed for), a repeat carrying an
// occurrence count, and a non-actionable fire-and-forget alert that carries a
// `status_note` instead of actions.
const notificationsHandler: MockHandler = ({ url, method, body }) => {
  if (!url.startsWith('/istota/api/notifications')) return undefined;
  const path = url.split('?')[0];
  const open = () => mockNotifications.filter((n) => !n.dismissed);

  if (path === '/istota/api/notifications/count') {
    return { open: open().length, actionable: open().filter((n) => n.actionable).length };
  }
  if (path === '/istota/api/notifications' && method === 'GET') {
    const filter = new URLSearchParams(url.split('?')[1] ?? '').get('filter') ?? 'all';
    const rows = filter === 'action' ? open().filter((n) => n.actionable) : open();
    return {
      notifications: rows.map(({ dismissed: _dismissed, ...n }) => n),
      total_open: open().length,
    };
  }
  if (path.endsWith('/dismiss') && method === 'POST') {
    const row = mockNotifications.find((n) => n.id === Number(path.split('/').at(-2)));
    if (!row) return undefined;
    row.dismissed = true;
    return { ok: true };
  }
  if (path === '/istota/api/notifications/seen' && method === 'POST') {
    // Only `task_alert` auto-resolves on seen, and only when the client's
    // `updated_at` still matches -- the same version check the store makes.
    for (const seen of body?.seen ?? []) {
      const row = mockNotifications.find((n) => n.id === seen.id);
      if (row && row.source === 'task_alert' && row.updated_at === seen.updated_at) {
        row.dismissed = true;
      }
    }
    return { ok: true };
  }
  return undefined;
};

// ---- Avatars ----
//
// The dev server can neither decode nor re-encode an upload, so it holds one
// canned 192x192 WebP and hashes that. What the mock reproduces is the shape
// the client codes against: an upload answers with a hash, `/me` then carries
// it, and the GET serves bytes under the same cache headers the real endpoint
// sends — including the rule that a co-member is never `immutable`, which
// nothing here can exercise but which the dev server should not contradict.
const MOCK_AVATAR_WEBP = Buffer.from(
  'UklGRrgCAABXRUJQVlA4IKwCAACQGACdASrAAMAAPxGIvFYsKiajp9ioAYAiCWdu4XKQz7OZBSdhspOI8yEVWLb1/Hb3cXsf' +
    'vnSffOk+sZWdYTsT70xCm4z1AqDx+4ErpPy8VAksHNZTKjVaEDdSZknYlOSIt4oFVgsHswSBENhbYTbW+IjP7uX6pEtO2ApW' +
    'uZuR9lIsniIjThXCAMAWg8JY8zcttdDR/+D3zZSwatEHRMgivULMTkoa+f4SswMdxTNqyV6KhgfeF2VnpDn6Yo9lblSU3TXY' +
    'rWnJhwVzqOyAAP75r7hN2Hfqxqxw8QbWCGTc/fzal5RooTkbgbiOHL6Ow+hQ9tu1Qr3Gb5p65wG0OAn9D6gZFOpFU+W+XRYe' +
    '3cIRN1/+4pUBiA4AWNdx5G/zTfrw6A/HpFF60f6sLQvxHqUPeZmcSh6vHvkCE2H3fAahVyuUWx6uy7rwuRA41e3C2JxKrpzz' +
    'qTb2FUNTW8pVZ3hVvGZYZatKY4/54QYWQiEJRw+zsPMO0ACzlgkiSQTI5kqIiyT8UfCOOmJORtBnLUDyx02mWRSXgA5N5ly4' +
    'Pq5vwv95zOcMWDDbthXBRoIT3+wz1wR8tKwox7cnvAQ5wKYDD14BygruMtJZAZhdGcxGInJ+ovDW77RCecAn0KrCpU/o/my9' +
    'ZnG0ff2dFStBuHrgdiHpAsaQRSrRJkPva8CvlFp5apoB6Pa037iAsQg3bNfH+3rCr3r5aSTAJRLY5Qa1Ww+lEqf77L5y5Szz' +
    'g6YW2ZANdRI0cHLBMaC8u6ggKFuup0Av1bjI9MgcjF1xxyAEF8b5xI7kQy/3ErioVfxOICjOBnueDkkHv0NpZiF3nDg3UVch' +
    'HSYlQYLpuFdnJaVlO5cYNTkm9U/bjzmJ+LeOPvRU3WKC0rNHqIJUCGXZWcJ6Q0b210e343QAAAA=',
  'base64',
);
const MOCK_AVATAR_HASH = createHash('sha256').update(MOCK_AVATAR_WEBP).digest('hex');

const avatarsHandler: MockHandler = ({ url, method }) => {
  const path = url.split('?')[0];
  if (path === '/istota/api/settings/avatar' && method === 'PUT') {
    user.avatars.user = MOCK_AVATAR_HASH;
    return { hash: MOCK_AVATAR_HASH, mime: 'image/webp', bytes: MOCK_AVATAR_WEBP.length };
  }
  if (path === '/istota/api/settings/avatar' && method === 'DELETE') {
    const deleted = user.avatars.user !== null;
    user.avatars.user = null;
    return { deleted };
  }
  // The bot icon is deployment-wide, so the mock keeps one flag rather than a
  // per-user one. The dev server has no admin gate to reproduce, and putting
  // one here would make the control untestable on the surface it exists for.
  if (path === '/istota/api/admin/avatar' && method === 'PUT') {
    user.avatars.bot = MOCK_AVATAR_HASH;
    return { hash: MOCK_AVATAR_HASH, mime: 'image/webp', bytes: MOCK_AVATAR_WEBP.length };
  }
  if (path === '/istota/api/admin/avatar' && method === 'DELETE') {
    const deleted = user.avatars.bot !== null;
    user.avatars.bot = null;
    return { deleted };
  }
  if (path === '/istota/api/avatars/bot' && method === 'GET') {
    if (!user.avatars.bot) {
      return {
        __status: 404,
        error: 'not found',
        __headers: { 'Cache-Control': 'private, max-age=30' },
      };
    }
    const version = new URLSearchParams(url.split('?')[1] ?? '').get('v');
    return {
      __raw: MOCK_AVATAR_WEBP,
      __contentType: 'image/webp',
      __disposition: 'inline',
      __headers: {
        // The bot icon is not a revocable grant, so a matching version keeps
        // the long cache the real endpoint sends.
        'Cache-Control':
          version === user.avatars.bot
            ? 'private, max-age=31536000, immutable'
            : 'private, no-cache',
        ETag: `"${user.avatars.bot}"`,
      },
    };
  }
  const prefix = '/istota/api/avatars/user/';
  if (path.startsWith(prefix) && method === 'GET') {
    const subject = decodeURIComponent(path.slice(prefix.length));
    // Every refusal is the same 404 the server sends: not visible, no such
    // user and no picture are one answer there, and a mock that answered them
    // apart would teach the client a distinction it must not depend on.
    if (subject !== user.username || !user.avatars.user) {
      // The negative cache is part of the answer: it is what makes one request
      // per author per session true rather than one per page load.
      return {
        __status: 404,
        error: 'not found',
        __headers: { 'Cache-Control': 'private, max-age=30' },
      };
    }
    const version = new URLSearchParams(url.split('?')[1] ?? '').get('v');
    return {
      __raw: MOCK_AVATAR_WEBP,
      __contentType: 'image/webp',
      // Never `attachment`: this renders in an `<img>`.
      __disposition: 'inline',
      __headers: {
        'Cache-Control':
          version === user.avatars.user
            ? 'private, max-age=31536000, immutable'
            : 'private, no-cache',
        ETag: `"${user.avatars.user}"`,
      },
    };
  }
  return undefined;
};

const handlers: MockHandler[] = [
  ({ url }) => (url === '/istota/api/me' ? user : undefined),
  avatarsHandler,
  chatHandler,
  notificationsHandler,

  ({ url }) =>
    url === '/istota/api/admin/stats'
      ? { ...mockAdminStats, subscription: mockSubscriptionState() }
      : undefined,

  // Admin logs + configuration (ISSUE-203)
  ({ url }) => (url === '/istota/api/admin/logs/sources' ? mockLogSources : undefined),
  ({ url }) => (url === '/istota/api/admin/config' ? mockAdminConfig() : undefined),
  // The live tail is SSE, which this mock layer cannot serve. Answer with an
  // empty 200 rather than letting it 404: an EventSource retries a failed
  // connection forever, so a missing handler turns Follow into a reconnect loop
  // in mock dev instead of an obviously-inert button.
  ({ url }) => (/^\/istota\/api\/admin\/logs\/[^/?]+\/stream(\?|$)/.test(url) ? {} : undefined),
  ({ url }) => {
    const match = url.match(/^\/istota\/api\/admin\/logs\/([^/?]+)(\?|$)/);
    if (!match || match[1] === 'sources') return undefined;
    const source = match[1];
    const params = new URLSearchParams(url.split('?')[1] ?? '');
    return mockLogPage(source, params);
  },

  // Settings/secrets (Phase 5)
  (req) => {
    const { url, method, body } = req;
    if (url === '/istota/api/settings/services' && method === 'GET') {
      return mockSettingsServices();
    }
    if (url === '/istota/api/settings/modules' && method === 'GET') {
      return mockModulesResponse();
    }
    if (url === '/istota/api/settings/nextcloud-token' && method === 'DELETE') {
      user.nextcloud_token = { connected: false, expires_at: null };
      return { ok: true };
    }
    if (url === '/istota/api/google/status' && method === 'GET') {
      return mockGoogleStatus();
    }
    if (url === '/istota/api/google/scopes' && method === 'PUT') {
      const raw = (body as { selection?: Record<string, string> })?.selection;
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
        return { error: 'selection must be an object' };
      }
      // Same normalization the server does: unknown services and levels are
      // dropped rather than rejected, and an explicit "off" is kept.
      const known = new Set<string>(GOOGLE_SERVICES.map((s) => s.service));
      mockGoogle.selection = Object.fromEntries(
        Object.entries(raw).filter(
          ([k, v]) => known.has(k) && typeof v === 'string' && v in LEVEL_RANK,
        ),
      );
      const requested = googleResolveScopes(mockGoogle.selection);
      return {
        ok: true,
        selection: mockGoogle.selection,
        requested_scopes: requested,
        reconnect_required:
          mockGoogle.granted !== null && googleMissing(requested, mockGoogle.granted).length > 0,
      };
    }
    if (url === '/istota/api/google/disconnect' && method === 'DELETE') {
      const was = mockGoogle.granted !== null;
      mockGoogle.granted = null;
      return { ok: true, was_connected: was };
    }
    const moduleSvcMatch = url.match(/^\/istota\/api\/settings\/module-services\/([^/?]+)$/);
    if (moduleSvcMatch && method === 'GET') {
      const resp = mockModuleServices(moduleSvcMatch[1]);
      if (!resp) return { error: `Unknown module: ${moduleSvcMatch[1]}` };
      return resp;
    }
    // Monarch login. Mirrors the real endpoint's challenge flow so the code
    // step is reachable in dev: any password is "accepted" and then asks for
    // an emailed code, which must be 6 digits. Without this the mock 404s and
    // the login form can only ever be seen in its failed state.
    if (url === '/istota/api/money/monarch/login' && method === 'POST') {
      const b = (body ?? {}) as Record<string, string>;
      if (!b.email || !b.password) {
        return { __status: 400, detail: 'email and password required' };
      }
      if (b.password === 'wrong') {
        return { __status: 401, detail: 'Invalid email and password combination' };
      }
      const code = (b.email_otp || '').trim();
      if (!code) {
        return {
          __status: 412,
          detail: {
            code: 'email_otp_required',
            message: 'Retrieve the code from your email to continue login.',
          },
        };
      }
      if (!/^\d{6}$/.test(code)) {
        return {
          __status: 412,
          detail: { code: 'email_otp_required', message: 'That code was not accepted.' },
        };
      }
      mockSecrets.monarch = { session_id: 'mock-session', csrftoken: 'mock-csrf' };
      return { ok: true };
    }
    // Ahead of the generic secret matcher below, which this path resembles.
    // (It could not actually be claimed by it — that pattern is anchored with
    // `/` excluded from both groups, so it cannot match three segments — but
    // reading the two in the other order invites the assumption that it can.)
    if (
      url === '/istota/api/settings/secrets/overland/ingest_token/generate' &&
      method === 'POST'
    ) {
      if (mockDisabledModules.has('location')) {
        // __status, not a bare {error}: the real endpoint answers 409 and the
        // page reads the message off it. Returning 200 here would make the
        // dev-mode failure a TypeError on an absent webhook_url instead.
        return {
          __status: 409,
          detail:
            'The location module is off for this user, so an ingest token ' +
            'would not be accepted. Enable it in Settings first.',
        };
      }
      // Shaped like the real one: 43 url-safe characters, returned once.
      const token = Array.from({ length: 43 }, () =>
        'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'.charAt(
          Math.floor(Math.random() * 64),
        ),
      ).join('');
      mockSecrets.overland = { ...(mockSecrets.overland ?? {}), ingest_token: token };
      return {
        ok: true,
        token,
        webhook_url: `https://example.invalid/webhooks/location?token=${token}`,
      };
    }
    const m = url.match(/^\/istota\/api\/settings\/secrets\/([^/]+)\/([^/?]+)$/);
    if (!m) return undefined;
    const [, service, key] = m;
    if (!mockSecrets[service]) return { error: 'unknown service' };
    if (method === 'PUT') {
      const value = (body?.value as string | undefined) ?? '';
      if (value) mockSecrets[service][key] = value;
      else delete mockSecrets[service][key];
      return { ok: true, service, key, configured: Boolean(value) };
    }
    if (method === 'DELETE') {
      const had = Boolean(mockSecrets[service][key]);
      delete mockSecrets[service][key];
      return { ok: true, deleted: had };
    }
    return undefined;
  },

  // Phase 6 — profile + resources
  (() => {
    const mockProfile: Record<string, unknown> = {
      user_id: user.username,
      display_name: user.display_name,
      timezone: 'UTC',
      email_addresses: ['user@example.com'],
      trusted_email_senders: [],
      quiet_email_senders: [],
      log_channel: 'logs-room-token',
      alerts_channel: 'alerts-room-token',
      disabled_skills: [],
      disabled_modules: [],
      max_foreground_workers: 0,
      max_background_workers: 0,
      default_destination: 'talk',
      routing: {},
      briefing_email_html: true,
      timezone_follow_location: false,
      external_turn_display: 'collapsed',
      purposes: ['reply', 'alert', 'log', 'briefing', 'notification'],
      delivery_surfaces: ['email', 'ntfy', 'talk'],
    };
    let nextResourceId = 100;
    const mockDbResources: {
      id: number;
      type: string;
      name: string;
      path: string;
      permissions: string;
      extras?: Record<string, unknown>;
    }[] = [];
    const resourceTypes = [
      {
        type: 'calendar',
        label: 'Calendar (CalDAV)',
        needs_path: true,
        permissions: ['read', 'readwrite'],
      },
      {
        type: 'folder',
        label: 'Nextcloud folder',
        needs_path: true,
        permissions: ['read', 'readwrite'],
      },
      {
        type: 'todo_file',
        label: 'TODO file (markdown)',
        needs_path: true,
        permissions: ['read', 'readwrite'],
      },
      { type: 'feeds', label: 'Feeds (RSS/Atom)', needs_path: false, permissions: ['read'] },
      { type: 'money', label: 'Money (beancount)', needs_path: false, permissions: ['read'] },
      {
        type: 'overland',
        label: 'Location (Overland GPS)',
        needs_path: false,
        permissions: ['read'],
      },
      { type: 'karakeep', label: 'Karakeep bookmarks', needs_path: false, permissions: ['read'] },
    ];
    return ({ url, method, body }: { url: string; method: string; body?: unknown }) => {
      if (url === '/istota/api/settings/profile' && method === 'GET') {
        return { profile: mockProfile };
      }
      if (url === '/istota/api/settings/profile' && method === 'PUT') {
        const patch = body as Record<string, unknown> | undefined;
        if (patch && typeof patch === 'object') {
          for (const [k, v] of Object.entries(patch)) {
            mockProfile[k] = v;
          }
          if (Array.isArray(patch.disabled_modules)) {
            mockDisabledModules.clear();
            for (const m of patch.disabled_modules as unknown[]) {
              if (typeof m === 'string' && MODULE_NAMES.includes(m)) {
                mockDisabledModules.add(m);
              }
            }
          }
        }
        return { ok: true, fields: Object.keys(patch ?? {}) };
      }
      if (url === '/istota/api/settings/resources' && method === 'GET') {
        return {
          types: resourceTypes,
          resources: [
            { managed: 'config', type: 'feeds', name: 'Feeds', path: '', permissions: 'read' },
            ...mockDbResources.map((r) => ({ managed: 'db', ...r })),
          ],
        };
      }
      if (url === '/istota/api/settings/resources' && method === 'POST') {
        const p = body as Record<string, unknown> | undefined;
        if (!p || typeof p !== 'object') return { error: 'bad payload' };
        const id = nextResourceId++;
        const extras = p.extras as Record<string, unknown> | undefined;
        mockDbResources.push({
          id,
          type: String(p.type ?? ''),
          name: String(p.name ?? ''),
          path: String(p.path ?? p.type ?? ''),
          permissions: String(p.permissions ?? 'read'),
          ...(extras && typeof extras === 'object' ? { extras } : {}),
        });
        return { ok: true, id };
      }
      const m = url.match(/^\/istota\/api\/settings\/resources\/(\d+)$/);
      if (m && method === 'DELETE') {
        const id = Number(m[1]);
        const idx = mockDbResources.findIndex((r) => r.id === id);
        if (idx >= 0) mockDbResources.splice(idx, 1);
        return { ok: true, deleted: idx >= 0 };
      }
      return undefined;
    };
  })(),

  // Phase 7b — briefings
  (() => {
    let nextBriefingId = 200;
    const mockDbBriefings: {
      id: number;
      name: string;
      cron: string;
      title: string;
      conversation_token: string;
      output: string;
      enabled: boolean;
    }[] = [];
    const tomlBriefings = [
      {
        name: 'morning',
        cron: '0 7 * * 1-5',
        // Blank exercises the derived-title placeholder in the editor.
        title: '',
        conversation_token: 'abc123',
        output: 'talk' as const,
        enabled: true,
      },
    ];
    return ({ url, method, body }: { url: string; method: string; body?: unknown }) => {
      if (url === '/istota/api/settings/briefings' && method === 'GET') {
        return {
          briefings: [
            ...tomlBriefings.map((b) => ({ managed: 'config', ...b })),
            ...mockDbBriefings.map((b) => ({ managed: 'db', ...b })),
          ],
          rooms: [
            { token: 'abc123', name: 'Log channel' },
            { token: 'def456', name: 'Alerts channel' },
          ],
          outputs: ['talk', 'email', 'ntfy'],
        };
      }
      if (url === '/istota/api/settings/briefings' && method === 'POST') {
        const p = body as Record<string, unknown> | undefined;
        if (!p || typeof p !== 'object') return { error: 'bad payload' };
        const name = String(p.name ?? '');
        const existing = mockDbBriefings.findIndex((b) => b.name === name);
        const title = String(p.title ?? '').trim();
        // Mirrors the server-side shape check so the 400 class is exercised
        // under VITE_MOCK_API=1 rather than only in production.
        if (title.length > 200) return { error: 'title must be 200 characters or fewer' };
        const row = {
          id: existing >= 0 ? mockDbBriefings[existing].id : nextBriefingId++,
          name,
          cron: String(p.cron ?? ''),
          title,
          conversation_token: String(p.conversation_token ?? ''),
          output: (p.output as string) ?? 'talk',
          enabled: p.enabled !== false,
        };
        if (existing >= 0) mockDbBriefings[existing] = row;
        else mockDbBriefings.push(row);
        return {
          ok: true,
          id: row.id,
          state: existing >= 0 ? 'updated' : 'created',
        };
      }
      const m = url.match(/^\/istota\/api\/settings\/briefings\/(\d+)$/);
      if (m && method === 'DELETE') {
        const id = Number(m[1]);
        const idx = mockDbBriefings.findIndex((b) => b.id === id);
        if (idx >= 0) mockDbBriefings.splice(idx, 1);
        return { ok: true, deleted: idx >= 0 };
      }
      return undefined;
    };
  })(),

  // Briefings module (Stage 4/5): reader archive + block/source editor.
  // Mirrors istota.briefings.routes mounted at /istota/api/briefings.
  (() => {
    const SOURCE_KINDS = [
      'rss',
      'email',
      'browse',
      'markets',
      'calendar',
      'todos',
      'reminders',
      'notes',
      'kv',
      'shared_block',
    ];
    const STRUCTURED_KINDS = ['markets', 'calendar'];
    const ALLOWED_SHARED_SOURCE_KINDS = ['browse', 'markets', 'email'];

    // Admin shared-block definitions + a custom-published key with no def.
    interface SharedBlockDef {
      name: string;
      cron: string;
      title: string;
      directive: string;
      render_mode: string;
      enabled: boolean;
      trusted: boolean;
      sources: { kind: string; config: Record<string, unknown> }[];
      last_run_at: string | null;
      value: { text: string; updated_at: string } | null;
    }
    const sharedBlocks: SharedBlockDef[] = [
      {
        name: 'world-headlines',
        cron: '0 6 * * *',
        title: '🌍 World headlines',
        directive: 'Synthesize the frontpages into ~8 top world stories.',
        render_mode: 'synthesis',
        enabled: true,
        trusted: false,
        sources: [
          { kind: 'browse', config: { preset: 'ap' } },
          { kind: 'browse', config: { preset: 'reuters' } },
        ],
        last_run_at: new Date(Date.now() - 3600_000).toISOString(),
        value: {
          text: '🌍 World headlines\n• Story one\n• Story two',
          updated_at: new Date(Date.now() - 3600_000).toISOString(),
        },
      },
      {
        name: 'markets-summary',
        cron: '30 6 * * *',
        title: '📈 Markets',
        directive: '',
        render_mode: 'structured',
        enabled: true,
        trusted: true,
        sources: [{ kind: 'markets', config: {} }],
        last_run_at: new Date(Date.now() - 1800_000).toISOString(),
        value: {
          text: '📈 S&P 500 +0.4%  Nasdaq +0.6%',
          updated_at: new Date(Date.now() - 1800_000).toISOString(),
        },
      },
    ];
    // A custom-published key (from a publish_shared_kv job) with no definition.
    const customPublishedKeys = [
      { name: 'film-business-digest', updated_at: new Date(Date.now() - 7200_000).toISOString() },
    ];
    const sharedStatus = (b: SharedBlockDef) => ({
      last_run_at: b.last_run_at,
      value_updated_at: b.value?.updated_at ?? null,
      value_preview: b.value?.text?.slice(0, 400) ?? null,
      stored_trusted: b.value ? b.trusted : null,
      has_content: b.value != null,
    });
    const mapSharedBlock = (b: SharedBlockDef) => ({
      name: b.name,
      cron: b.cron,
      title: b.title,
      directive: b.directive,
      render_mode: b.render_mode,
      enabled: b.enabled,
      trusted: b.trusted,
      sources: b.sources,
      created_at: null,
      updated_at: null,
      status: sharedStatus(b),
    });
    const BROWSE_PRESETS = [
      { key: 'ap', name: 'AP News', url: 'https://apnews.com' },
      { key: 'reuters', name: 'Reuters', url: 'https://www.reuters.com' },
      { key: 'guardian', name: 'The Guardian', url: 'https://www.theguardian.com/world' },
      { key: 'ft', name: 'Financial Times', url: 'https://www.ft.com' },
      { key: 'aljazeera', name: 'Al Jazeera', url: 'https://www.aljazeera.com' },
      { key: 'lemonde', name: 'Le Monde', url: 'https://www.lemonde.fr/en/' },
      { key: 'spiegel', name: 'Der Spiegel', url: 'https://www.spiegel.de/international/' },
    ];

    interface Src {
      id: number;
      position: number;
      kind: string;
      config: Record<string, unknown>;
      enabled: boolean;
    }
    interface Block {
      id: number;
      briefing_name: string;
      position: number;
      title: string;
      directive: string;
      render_mode: string;
      options: Record<string, unknown>;
      sources: Src[];
    }
    let nextBlockId = 500;
    let nextSourceId = 900;
    const blocks: Block[] = [
      {
        id: 401,
        briefing_name: 'morning',
        position: 0,
        title: 'World news',
        directive: 'Summarize the top world stories in 4-5 bullet points.',
        render_mode: 'synthesis',
        options: {},
        sources: [
          { id: 801, position: 0, kind: 'browse', config: { preset: 'ap' }, enabled: true },
          {
            id: 802,
            position: 1,
            kind: 'rss',
            config: { feed_ref: { kind: 'subscription', value: 12 }, limit: 8 },
            enabled: true,
          },
        ],
      },
      {
        id: 402,
        briefing_name: 'morning',
        position: 1,
        title: 'Markets',
        directive: 'Overnight index moves and notable movers.',
        render_mode: 'structured',
        options: {},
        sources: [{ id: 803, position: 0, kind: 'markets', config: {}, enabled: true }],
      },
      {
        id: 403,
        briefing_name: 'morning',
        position: 2,
        title: 'Todos & notes',
        directive: '',
        render_mode: 'synthesis',
        options: {},
        sources: [
          {
            id: 804,
            position: 0,
            kind: 'todos',
            config: { path: 'shared/team-todo.md' },
            enabled: true,
          },
          { id: 805, position: 1, kind: 'notes', config: {}, enabled: false },
        ],
      },
    ];

    // Mock workspace text files for the file-path picker + verification.
    const MOCK_FILES = [
      'shared/team-todo.md',
      'shared/reminders.md',
      'istota/config/TODO.md',
      'istota/notes/agenda.md',
      'inbox/dropped-note.md',
    ];

    const archive = [
      {
        id: 1,
        briefing_name: 'morning',
        subject: 'Morning briefing — Mon Jul 20',
        generated_at: new Date(Date.now() - 3600_000).toISOString(),
        task_id: 4201,
        delivered_to: ['talk'],
        body_md:
          '# Morning briefing\n\n## World news\n\n- Mock story one about global affairs.\n- Mock story two about the economy.\n\n## Markets\n\n| Index | Level | Change |\n| --- | --- | --- |\n| S&P 500 | 5,432 | +0.4% |\n| Nasdaq | 17,800 | +0.7% |\n',
      },
      {
        id: 2,
        briefing_name: 'morning',
        subject: 'Morning briefing — Fri Jul 17',
        generated_at: new Date(Date.now() - 3 * 86400_000).toISOString(),
        task_id: 4198,
        delivered_to: ['talk', 'email'],
        body_md:
          '# Morning briefing\n\n## World news\n\n- An older mock story.\n- Another older mock story.\n',
      },
    ];
    const briefingNames = [...new Set(archive.map((a) => a.briefing_name))];

    const configResponse = () => ({
      briefings: [{ name: 'morning', blocks }],
      schedule_names: ['morning'],
      source_kinds: SOURCE_KINDS,
      structured_kinds: STRUCTURED_KINDS,
    });

    return ({ url, method, body }: { url: string; method: string; body?: unknown }) => {
      const p = (url.split('?')[0] || '').replace('/istota/api/briefings', '');

      // Archive list (paged, optional ?name= filter)
      if (p === '/archive' && method === 'GET') {
        const qs = new URLSearchParams(url.split('?')[1] || '');
        const name = qs.get('name');
        const items = (name ? archive.filter((a) => a.briefing_name === name) : archive).map(
          ({ body_md, ...rest }) => rest,
        );
        return { items, total: items.length, briefing_names: briefingNames };
      }
      const am = p.match(/^\/archive\/(\d+)$/);
      if (am && method === 'GET') {
        const item = archive.find((a) => a.id === Number(am[1]));
        return item ?? { error: 'not found' };
      }
      if (am && method === 'DELETE') {
        const idx = archive.findIndex((a) => a.id === Number(am[1]));
        if (idx === -1) return { error: 'not found' };
        archive.splice(idx, 1);
        return { status: 'ok' };
      }

      if (p === '/config' && method === 'GET') return configResponse();

      // Block upsert / reorder / delete
      if (p === '/blocks' && method === 'PUT') {
        const b = (body ?? {}) as Record<string, unknown>;
        const reorder = b.reorder as { ordered_ids?: number[] } | undefined;
        if (reorder?.ordered_ids) {
          const order = reorder.ordered_ids;
          blocks.sort((x, y) => order.indexOf(x.id) - order.indexOf(y.id));
          blocks.forEach((blk, i) => (blk.position = i));
          return { status: 'ok' };
        }
        const id = Number(b.id) || 0;
        if (id) {
          const blk = blocks.find((x) => x.id === id);
          if (blk) {
            if (b.title !== undefined) blk.title = String(b.title);
            if (b.directive !== undefined) blk.directive = String(b.directive);
            if (b.render_mode !== undefined) blk.render_mode = String(b.render_mode);
            if (b.position !== undefined) blk.position = Number(b.position);
            return { status: 'ok', block: blk };
          }
        }
        const blk: Block = {
          id: nextBlockId++,
          briefing_name: String(b.briefing_name ?? 'morning'),
          position: Number(b.position ?? blocks.length),
          title: String(b.title ?? 'New block'),
          directive: String(b.directive ?? ''),
          render_mode: String(b.render_mode ?? 'synthesis'),
          options: {},
          sources: [],
        };
        blocks.push(blk);
        return { status: 'ok', block: blk };
      }
      const bm = p.match(/^\/blocks\/(\d+)$/);
      if (bm && method === 'DELETE') {
        const idx = blocks.findIndex((x) => x.id === Number(bm[1]));
        if (idx >= 0) blocks.splice(idx, 1);
        return { status: 'ok' };
      }

      // Source upsert / delete
      if (p === '/sources' && method === 'PUT') {
        const s = (body ?? {}) as Record<string, unknown>;
        const sid = Number(s.id) || 0;
        // Update-by-id: the real route locates the source by id alone
        // (no block_id needed), so search across every block.
        if (sid) {
          for (const b of blocks) {
            const src = b.sources.find((x) => x.id === sid);
            if (src) {
              if (s.kind !== undefined) src.kind = String(s.kind);
              if (s.config !== undefined) src.config = s.config as Record<string, unknown>;
              if (s.enabled !== undefined) src.enabled = Boolean(s.enabled);
              return { status: 'ok', id: src.id };
            }
          }
          return { error: 'unknown source' };
        }
        const blk = blocks.find((x) => x.id === Number(s.block_id));
        if (!blk) return { error: 'unknown block' };
        const src: Src = {
          id: nextSourceId++,
          position: blk.sources.length,
          kind: String(s.kind ?? 'rss'),
          config: (s.config as Record<string, unknown>) ?? {},
          enabled: s.enabled !== false,
        };
        blk.sources.push(src);
        return { status: 'ok', id: src.id };
      }
      const sm = p.match(/^\/sources\/(\d+)$/);
      if (sm && method === 'DELETE') {
        const id = Number(sm[1]);
        for (const blk of blocks) {
          const idx = blk.sources.findIndex((x) => x.id === id);
          if (idx >= 0) {
            blk.sources.splice(idx, 1);
            break;
          }
        }
        return { status: 'ok' };
      }

      if (p === '/browse-presets' && method === 'GET') {
        return { presets: BROWSE_PRESETS };
      }
      if (p === '/feed-options' && method === 'GET') {
        return {
          available: true,
          subscriptions: [
            { kind: 'subscription', value: 12, label: 'Hacker News' },
            { kind: 'subscription', value: 13, label: 'Ars Technica' },
          ],
          categories: [
            { kind: 'category', value: 1, label: 'Tech' },
            { kind: 'category', value: 2, label: 'News' },
          ],
        };
      }
      if (p === '/path-suggest' && method === 'GET') {
        const qs = new URLSearchParams(url.split('?')[1] || '');
        const q = (qs.get('q') || '').trim().toLowerCase();
        const paths = q ? MOCK_FILES.filter((f) => f.toLowerCase().includes(q)) : MOCK_FILES;
        return { paths };
      }
      if (p === '/path-check' && method === 'GET') {
        const qs = new URLSearchParams(url.split('?')[1] || '');
        const path = (qs.get('path') || '').trim();
        if (!path) return { ok: false, error: 'Enter a file path.' };
        if (MOCK_FILES.includes(path)) return { ok: true, resolved: `Users/dana/${path}` };
        return { ok: false, error: 'No file found at that path.' };
      }

      // --- Shared blocks (admin) + options (any user) ---
      if (p === '/shared-block-options' && method === 'GET') {
        const opts: {
          name: string;
          updated_at: string | null;
          has_content: boolean;
          source: string;
        }[] = [];
        for (const b of sharedBlocks) {
          opts.push({
            name: b.name,
            updated_at: b.value?.updated_at ?? null,
            has_content: b.value != null,
            source: 'config',
          });
        }
        for (const c of customPublishedKeys) {
          opts.push({
            name: c.name,
            updated_at: c.updated_at,
            has_content: true,
            source: 'custom',
          });
        }
        opts.sort((a, z) => a.name.localeCompare(z.name));
        return { options: opts };
      }
      if (p === '/shared-blocks' && method === 'GET') {
        return {
          shared_blocks: sharedBlocks.map(mapSharedBlock),
          allowed_source_kinds: ALLOWED_SHARED_SOURCE_KINDS,
          render_modes: ['synthesis', 'structured'],
        };
      }
      if (p === '/shared-blocks' && method === 'PUT') {
        const b = (body ?? {}) as Record<string, unknown>;
        const name = String(b.name ?? '').trim();
        if (!name) return { error: 'name required' };
        const srcs = ((b.sources as { kind: string; config: Record<string, unknown> }[]) ?? []).map(
          (s) => ({ kind: s.kind, config: s.config ?? {} }),
        );
        let existing = sharedBlocks.find((x) => x.name === name);
        if (!existing) {
          existing = {
            name,
            cron: '',
            title: '',
            directive: '',
            render_mode: 'synthesis',
            enabled: true,
            trusted: false,
            sources: [],
            last_run_at: null,
            value: null,
          };
          sharedBlocks.push(existing);
        }
        existing.cron = String(b.cron ?? '');
        existing.title = String(b.title ?? '');
        existing.directive = String(b.directive ?? '') || '';
        existing.render_mode = String(b.render_mode ?? 'synthesis');
        existing.enabled = b.enabled !== false;
        existing.trusted = !!b.trusted;
        existing.sources = srcs;
        return { status: 'ok', shared_block: mapSharedBlock(existing) };
      }
      const sbRun = p.match(/^\/shared-blocks\/([^/]+)\/run$/);
      if (sbRun && method === 'POST') {
        const name = decodeURIComponent(sbRun[1]);
        const b = sharedBlocks.find((x) => x.name === name);
        if (!b) return { error: 'not found', __status: 404 };
        b.last_run_at = new Date().toISOString();
        b.value = {
          text: `${b.title}\n(freshly generated ${new Date().toLocaleTimeString()})`,
          updated_at: b.last_run_at,
        };
        return { status: 'ok', error: null, block_status: sharedStatus(b) };
      }
      const sbDel = p.match(/^\/shared-blocks\/([^/]+)$/);
      if (sbDel && method === 'DELETE') {
        const name = decodeURIComponent(sbDel[1]);
        const idx = sharedBlocks.findIndex((x) => x.name === name);
        if (idx >= 0) sharedBlocks.splice(idx, 1);
        return { status: 'ok', deleted_value: false };
      }
      return undefined;
    };
  })(),

  // Feeds settings: config GET/PUT
  ({ url, method, body }) => {
    if (url !== '/istota/api/feeds/config') return undefined;
    if (method === 'GET') return feedsConfigResponse();
    if (method === 'PUT') {
      const cfg = body?.config;
      if (cfg && typeof cfg === 'object') {
        mockFeedsConfig.settings = cfg.settings ?? {};
        mockFeedsConfig.categories = cfg.categories ?? [];
        mockFeedsConfig.feeds = cfg.feeds ?? [];
      }
      return {
        status: 'ok',
        sync: {
          categories_added: 0,
          feeds_added: 0,
          feeds_updated: mockFeedsConfig.feeds.length,
        },
      };
    }
    return undefined;
  },

  ({ url, method }) => {
    if (url !== '/istota/api/feeds/import-opml' || method !== 'POST') return undefined;
    return {
      status: 'ok',
      feeds_added: 1,
      feeds_updated: 0,
      categories_added: 1,
      rewritten_bridger_urls: 0,
    };
  },

  // Reader: GET /feeds with pagination, sorting, filtering
  ({ url, method }) => {
    if (method !== 'GET') return undefined;
    const [path, query] = url.split('?');
    if (path !== '/istota/api/feeds') return undefined;
    return feedsListResponse(new URLSearchParams(query ?? ''));
  },

  // Reader mutations — accept and acknowledge.
  ({ url, method, body }) => {
    const m = url.match(/^\/istota\/api\/feeds\/entries\/(\d+)$/);
    if (!m || method !== 'PUT') return undefined;
    const id = Number(m[1]);
    const entry = mockReaderEntries.find((e) => e.id === id);
    if (entry && body && typeof body === 'object') {
      if (typeof body.starred === 'boolean') {
        entry.starred = body.starred;
        entry.starred_at = body.starred ? new Date().toISOString() : '';
      }
      if (typeof body.status === 'string') {
        entry.status = body.status === 'read' ? 'read' : 'unread';
      }
    }
    return { status: 'ok' };
  },
  ({ url, method, body }) => {
    if (url !== '/istota/api/feeds/entries/batch' || method !== 'PUT') return undefined;
    const ids: number[] = Array.isArray(body?.entry_ids) ? body.entry_ids : [];
    const status = body?.status === 'read' ? 'read' : 'unread';
    for (const id of ids) {
      const e = mockReaderEntries.find((x) => x.id === id);
      if (e) e.status = status;
    }
    return { status: 'ok', updated: ids.length };
  },
  ({ url, method, body }) => {
    if (url !== '/istota/api/feeds/mark-as-read' || method !== 'POST') return undefined;
    const scope = body?.scope;
    const beforeId: number | undefined = body?.before_id;
    const targetId: number | undefined = body?.id;
    let updated = 0;
    for (const e of mockReaderEntries) {
      if (e.status === 'read') continue;
      if (beforeId != null && e.id > beforeId) continue;
      if (scope === 'feed' && targetId != null && e.feed.id !== targetId) continue;
      e.status = 'read';
      updated++;
    }
    return { status: 'ok', updated };
  },
  ({ url, method }) => {
    if (url !== '/istota/api/feeds/refresh' || method !== 'POST') return undefined;
    return { status: 'ok' };
  },
  ({ url }) => {
    if (url !== '/istota/api/location/settings-info') return undefined;
    return {
      webhook_url: 'https://example.invalid/webhooks/location?token=<token>',
      module_enabled: !mockDisabledModules.has('location'),
      place_detection: {
        accuracy_threshold_m: 100,
        visit_exit_minutes: 5,
      },
    };
  },

  ({ url }) => (url.startsWith('/istota/api/location/current') ? mockCurrent : undefined),

  // Place stats
  ({ url }) => {
    const m = url.match(/\/istota\/api\/location\/places\/(\d+)\/stats/);
    if (!m) return undefined;
    return {
      place_id: Number(m[1]),
      total_visits: 0,
      first_visit: null,
      last_visit: null,
      avg_duration_min: null,
      total_duration_min: null,
      longest_visit_min: null,
    };
  },

  // Place CRUD
  ({ url, method, body }) => {
    if (!url.startsWith('/istota/api/location/places')) return undefined;

    const idMatch = url.match(/\/istota\/api\/location\/places\/(\d+)$/);
    if (idMatch && method === 'PUT') {
      const id = Number(idMatch[1]);
      const idx = mockPlaces.places.findIndex((p) => p.id === id);
      if (idx >= 0) {
        mockPlaces.places[idx] = { ...mockPlaces.places[idx], ...body };
      }
      return mockPlaces.places[idx] ?? {};
    }
    if (idMatch && method === 'DELETE') {
      const id = Number(idMatch[1]);
      mockPlaces.places = mockPlaces.places.filter((p) => p.id !== id);
      return {};
    }
    if (method === 'POST') {
      const created: MockPlace = {
        id: nextPlaceId++,
        name: body?.name ?? 'Untitled',
        lat: body?.lat ?? 0,
        lon: body?.lon ?? 0,
        radius_meters: body?.radius_meters ?? 100,
        category: body?.category ?? 'other',
        notes: body?.notes ?? '',
      };
      mockPlaces.places.push(created);
      dropClusterNear(created, created.radius_meters);
      return created;
    }
    return mockPlaces;
  },

  // Dismissed clusters
  ({ url, method, body }) => {
    if (!url.startsWith('/istota/api/location/dismissed-clusters')) return undefined;

    const idMatch = url.match(/\/istota\/api\/location\/dismissed-clusters\/(\d+)$/);
    if (idMatch && method === 'DELETE') {
      const id = Number(idMatch[1]);
      mockDismissed.dismissed = mockDismissed.dismissed.filter((d) => d.id !== id);
      return {};
    }
    if (method === 'POST') {
      const created: MockDismissed = {
        id: nextDismissedId++,
        lat: body?.lat ?? 0,
        lon: body?.lon ?? 0,
        radius_meters: body?.radius_meters ?? 100,
        dismissed_at: new Date().toISOString(),
      };
      mockDismissed.dismissed.push(created);
      dropClusterNear(created, created.radius_meters);
      return created;
    }
    return mockDismissed;
  },

  ({ url }) => (url.startsWith('/istota/api/location/discover-places') ? mockDiscover : undefined),
  ({ url }) => (url.startsWith('/istota/api/location/pings') ? mockPings : undefined),
  ({ url }) => (url.startsWith('/istota/api/location/day-summary') ? mockDay : undefined),
  ({ url }) => (url.startsWith('/istota/api/money/ledgers') ? ledgers : undefined),
  ({ url }) => (url.startsWith('/istota/api/money/check') ? checkResp : undefined),
  ({ url }) => (url.startsWith('/istota/api/money/accounts') ? accountsResp : undefined),

  // Money reports (income statement / balance sheet / cash flow). The three
  // Reports tabs 404'd against the mock while working fine against the real
  // API, so the whole section was uninspectable in dev.
  //
  // Beancount's sign convention is reproduced rather than prettified: income
  // accrues negative (a credit) and expenses positive. The pages take
  // Math.abs() of each side, so mocking income as positive would still render
  // — and would then disagree with production the moment anything downstream
  // cares about the sign.
  ({ url }) => {
    const match = url.match(/^\/istota\/api\/money\/report\/([a-z-]+)(?:\?|$)/);
    if (!match) return undefined;
    const reportType = match[1];
    const year = Number(new URLSearchParams(url.split('?')[1] ?? '').get('year')) || 2026;

    if (reportType === 'income-statement') {
      const results = [
        { account: 'Income:Consulting', 'sum(position)': '-84000.00 USD' },
        { account: 'Income:Design', 'sum(position)': '-31500.00 USD' },
        { account: 'Income:Retainer', 'sum(position)': '-18000.00 USD' },
        { account: 'Expenses:Contractors', 'sum(position)': '26400.00 USD' },
        { account: 'Expenses:Food', 'sum(position)': '7310.55 USD' },
        { account: 'Expenses:Rent', 'sum(position)': '21600.00 USD' },
        { account: 'Expenses:Software', 'sum(position)': '4188.20 USD' },
        { account: 'Expenses:Travel', 'sum(position)': '9127.40 USD' },
      ];
      return { status: 'ok', report_type: reportType, year, row_count: results.length, results };
    }

    if (reportType === 'balance-sheet') {
      // No year filter on this one, matching the real query.
      const results = [
        { account: 'Assets:Checking', 'sum(position)': '48250.15 USD' },
        { account: 'Assets:Receivable', 'sum(position)': '12400.00 USD' },
        { account: 'Assets:Savings', 'sum(position)': '96000.00 USD' },
        { account: 'Liabilities:CreditCard', 'sum(position)': '-3182.66 USD' },
        { account: 'Liabilities:TaxPayable', 'sum(position)': '-18940.00 USD' },
        { account: 'Equity:Opening-Balances', 'sum(position)': '-65000.00 USD' },
      ];
      return {
        status: 'ok',
        report_type: reportType,
        year: null,
        row_count: results.length,
        results,
      };
    }

    if (reportType === 'cash-flow') {
      // Grouped by year/month/account, so the page can build its month picker
      // and chart. Twelve months of gently varying figures beats one flat month.
      const accounts: [string, number][] = [
        ['Income:Consulting', -7000],
        ['Income:Design', -2625],
        ['Expenses:Contractors', 2200],
        ['Expenses:Food', 609],
        ['Expenses:Rent', 1800],
        ['Expenses:Software', 349],
        ['Expenses:Travel', 760],
      ];
      const results = [];
      for (let month = 1; month <= 12; month++) {
        // Deterministic wobble — a mock that changes on every reload makes a
        // chart impossible to eyeball against itself.
        const wobble = 1 + (((month * 37) % 23) - 11) / 100;
        for (const [account, base] of accounts) {
          const amount = (base * wobble).toFixed(2);
          results.push({ year, month, account, 'sum(position)': `${amount} USD` });
        }
      }
      return { status: 'ok', report_type: reportType, year, row_count: results.length, results };
    }

    // Mirrors the real handler's rejection of an unknown report type.
    return { __status: 400, error: 'unknown report type' };
  },
  // Money module mock — invoicing config, transactions, invoices and work
  // entries in one stateful closure, so the CRUD surfaces (client/entity/
  // service forms, the kebab actions, the delete guards) are exercisable
  // end-to-end without a backend. The collections are shared: deleting a
  // service the work fixtures reference has to 409 here the way it does
  // against the real API.
  (() => {
    const PREFIX = '/istota/api/money';
    const today = () => new Date().toISOString().slice(0, 10);

    // --- Invoicing config (mutable) ---
    interface Entity {
      key: string;
      name: string;
      address: string;
      email: string;
      payment_instructions: string;
      logo: string;
      ar_account: string;
      bank_account: string;
      currency: string;
    }
    interface ServiceCfg {
      key: string;
      display_name: string;
      rate: number;
      type: string;
      income_account: string;
    }
    interface ClientCfg {
      key: string;
      name: string;
      address: string;
      email: string;
      terms: number | string;
      ar_account: string;
      entity: string;
      schedule: string;
      schedule_day: number;
      reminder_days: number;
      notifications: string;
      days_until_overdue: number;
      ledger_posting: boolean;
      bundles: Record<string, unknown>[];
      separate: string[];
    }

    const entities: Entity[] = [
      {
        key: 'main',
        name: 'Acme Studio LLC',
        address: '123 Example St, Berlin',
        email: 'billing@example.com',
        payment_instructions: 'Wire to IBAN DE00 …',
        logo: '',
        ar_account: 'Assets:AR:Acme',
        bank_account: 'Assets:Checking',
        currency: 'EUR',
      },
    ];

    const services: ServiceCfg[] = [
      {
        key: 'consulting',
        display_name: 'Consulting',
        rate: 150,
        type: 'hours',
        income_account: 'Income:Consulting',
      },
      {
        key: 'design',
        display_name: 'Design',
        rate: 1200,
        type: 'days',
        income_account: 'Income:Design',
      },
      {
        key: 'retainer',
        display_name: 'Retainer',
        rate: 2000,
        type: 'flat',
        income_account: 'Income:Retainer',
      },
      {
        key: 'expenses',
        display_name: 'Expenses',
        rate: 0,
        type: 'other',
        income_account: 'Income:Reimbursements',
      },
    ];

    function newClient(over: Partial<ClientCfg> & { key: string }): ClientCfg {
      return {
        name: over.key,
        address: '',
        email: '',
        terms: 30,
        ar_account: '',
        entity: '',
        schedule: 'on-demand',
        schedule_day: 1,
        reminder_days: 3,
        notifications: '',
        days_until_overdue: 0,
        ledger_posting: true,
        bundles: [],
        separate: [],
        ...over,
      };
    }

    const clientConfigs: ClientCfg[] = [
      newClient({
        key: 'globex',
        name: 'Globex',
        email: 'ap@globex.example',
        address: '1 Globex Way',
        entity: 'main',
        schedule: 'monthly',
        ar_account: 'Assets:AR:Globex',
      }),
      newClient({
        key: 'initech',
        name: 'Initech',
        email: 'billing@initech.example',
        address: '4 Initech Plaza',
        terms: 14,
        entity: 'main',
        ar_account: 'Assets:AR:Initech',
      }),
      newClient({
        key: 'hooli',
        name: 'Hooli',
        email: 'accounts@hooli.example',
        address: '9 Hooli Campus',
        entity: 'main',
        schedule: 'monthly',
        schedule_day: 15,
        ar_account: 'Assets:AR:Hooli',
      }),
    ];

    const defaults = {
      currency: 'EUR',
      default_entity: 'main',
      default_ar_account: 'Assets:AR:Acme',
      default_bank_account: 'Assets:Checking',
      invoice_output: '/tmp/invoices',
      next_invoice_number: 42,
      notifications: 'email',
      days_until_overdue: 14,
    };

    interface Txn {
      date: string;
      flag: string;
      payee: string;
      narration: string;
      account: string;
      position: string;
      id: string;
    }
    const transactions: Txn[] = [
      {
        id: 'mock-1',
        date: '2026-05-28',
        flag: '*',
        payee: 'Whole Foods',
        narration: 'Groceries',
        account: 'Expenses:Food',
        position: '-82.14 USD',
      },
      {
        id: 'mock-2',
        date: '2026-05-28',
        flag: '*',
        payee: 'Acme Corp',
        narration: 'May salary',
        account: 'Income:Salary',
        position: '5200.00 USD',
      },
      {
        id: 'mock-3',
        date: '2026-05-26',
        flag: '*',
        payee: 'Shell',
        narration: 'Fuel',
        account: 'Expenses:Auto',
        position: '-54.30 USD',
      },
      {
        id: 'mock-4',
        date: '2026-05-24',
        flag: '*',
        payee: 'Netflix',
        narration: 'Subscription',
        account: 'Expenses:Subscriptions',
        position: '-15.99 USD',
      },
      {
        id: 'mock-5',
        date: '2026-05-22',
        flag: '*',
        payee: 'Transfer',
        narration: 'To savings',
        account: 'Assets:Savings',
        position: '500.00 USD',
      },
      {
        id: 'mock-6',
        date: '2026-05-20',
        flag: '*',
        payee: 'Cafe Luna',
        narration: 'Coffee',
        account: 'Expenses:Food',
        position: '-6.75 USD',
      },
    ];

    interface Invoice {
      invoice_number: string;
      client: string;
      client_key: string;
      date: string;
      total: number;
      status: string;
      paid_date?: string;
    }
    const invoices: Invoice[] = [
      {
        invoice_number: 'INV-000042',
        client: 'Globex',
        client_key: 'globex',
        date: '2026-05-15',
        total: 4500,
        status: 'outstanding',
      },
      {
        invoice_number: 'INV-000041',
        client: 'Initech',
        client_key: 'initech',
        date: '2026-04-30',
        total: 1800,
        status: 'paid',
        paid_date: '2026-05-10',
      },
      {
        invoice_number: 'INV-000040',
        client: 'Globex',
        client_key: 'globex',
        date: '2026-04-15',
        total: 3200,
        status: 'outstanding',
      },
      {
        invoice_number: 'INV-000039',
        client: 'Hooli',
        client_key: 'hooli',
        date: '2026-03-31',
        total: 950,
        status: 'draft',
      },
    ];

    const invoiceItems: Record<
      string,
      Array<{
        description: string;
        detail: string;
        quantity: number;
        rate: number;
        discount: number;
        amount: number;
      }>
    > = {
      'INV-000042': [
        {
          description: 'Consulting',
          detail: 'May engagement',
          quantity: 30,
          rate: 150,
          discount: 0,
          amount: 4500,
        },
      ],
      'INV-000041': [
        {
          description: 'Design',
          detail: 'Brand refresh',
          quantity: 1.5,
          rate: 1200,
          discount: 0,
          amount: 1800,
        },
      ],
      'INV-000040': [
        {
          description: 'Consulting',
          detail: 'April engagement',
          quantity: 20,
          rate: 150,
          discount: 0,
          amount: 3000,
        },
        {
          description: 'Support',
          detail: 'Retainer',
          quantity: 1,
          rate: 200,
          discount: 0,
          amount: 200,
        },
      ],
      'INV-000039': [
        {
          description: 'Consulting',
          detail: 'Scoping',
          quantity: 6,
          rate: 150,
          discount: 0,
          amount: 900,
        },
        {
          description: 'Travel',
          detail: 'Reimbursement',
          quantity: 1,
          rate: 50,
          discount: 0,
          amount: 50,
        },
      ],
    };

    // Work entries — the input side of invoicing. Seeded to cover every
    // state the Work page renders differently: uninvoiced, invoiced, paid,
    // a flat-rate service, and an entry whose service no longer resolves.
    interface Work {
      uid: string;
      date: string;
      client: string;
      service: string;
      qty: number | null;
      amount: number | null;
      discount: number;
      description: string;
      entity: string;
      invoice: string;
      paid_date: string | null;
    }

    // Resolved against the live config above, not a frozen copy — so a
    // service edited or deleted through the settings page immediately
    // reprices (or flags) the work rows that name it.
    const findService = (key: string) => services.find((s) => s.key === key);
    const findClient = (key: string) => clientConfigs.find((c) => c.key === key);

    const work: Work[] = [
      {
        uid: 'work-1',
        date: '2026-05-28',
        client: 'globex',
        service: 'consulting',
        qty: 6,
        amount: null,
        discount: 0,
        description: 'Platform review',
        entity: 'main',
        invoice: '',
        paid_date: null,
      },
      {
        uid: 'work-2',
        date: '2026-05-27',
        client: 'globex',
        service: 'design',
        qty: 1.5,
        amount: null,
        discount: 100,
        description: 'Landing page',
        entity: 'main',
        invoice: '',
        paid_date: null,
      },
      {
        uid: 'work-3',
        date: '2026-05-20',
        client: 'hooli',
        service: 'retainer',
        qty: null,
        amount: null,
        discount: 0,
        description: 'May retainer',
        entity: 'main',
        invoice: '',
        paid_date: null,
      },
      {
        uid: 'work-4',
        date: '2026-05-12',
        client: 'initech',
        service: 'legacy-support',
        qty: 4,
        amount: null,
        discount: 0,
        description: 'Service no longer configured',
        entity: 'main',
        invoice: '',
        paid_date: null,
      },
      {
        uid: 'work-5',
        date: '2026-05-15',
        client: 'globex',
        service: 'consulting',
        qty: 30,
        amount: null,
        discount: 0,
        description: 'May engagement',
        entity: 'main',
        invoice: 'INV-000042',
        paid_date: null,
      },
      {
        uid: 'work-6',
        date: '2026-04-30',
        client: 'initech',
        service: 'design',
        qty: 1.5,
        amount: null,
        discount: 0,
        description: 'Brand refresh',
        entity: 'main',
        invoice: 'INV-000041',
        paid_date: '2026-05-10',
      },
    ];
    let nextWorkUid = 7;

    function workAmount(w: Work): number | null {
      const svc = findService(w.service);
      if (!svc) return null;
      let subtotal: number;
      if (svc.type === 'flat') subtotal = svc.rate;
      else if (svc.type === 'other') subtotal = w.amount ?? 0;
      else {
        subtotal = (w.qty ?? 0) * svc.rate;
        if (!subtotal && w.amount) subtotal = w.amount;
      }
      return Math.round((subtotal - w.discount) * 100) / 100;
    }

    function workEtag(w: Work): string {
      // Cheap content hash — enough for the mock's conflict path.
      const body = `${w.date}|${w.client}|${w.service}|${w.qty}|${w.amount}|${w.discount}|${w.description}|${w.entity}`;
      let hash = 0;
      for (let i = 0; i < body.length; i++) hash = (hash * 31 + body.charCodeAt(i)) | 0;
      return (hash >>> 0).toString(16);
    }

    function workRow(w: Work, index: number) {
      const svc = findService(w.service);
      const client = findClient(w.client);
      const warnings: string[] = [];
      if (!svc) warnings.push('unknown_service');
      if (!client) warnings.push('unknown_client');
      return {
        ...w,
        index,
        etag: workEtag(w),
        client_name: client?.name ?? w.client,
        service_name: svc?.display_name ?? w.service,
        service_type: svc?.type ?? '',
        computed_amount: workAmount(w),
        editable: !!w.uid && !w.invoice,
        warnings,
      };
    }

    const KEY_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
    const SERVICE_TYPES = ['hours', 'days', 'flat', 'other'];
    const CLIENT_SCHEDULES = ['on-demand', 'monthly'];
    const ACCOUNT_FIELDS = ['ar_account', 'bank_account', 'income_account'];

    /**
     * Mirror of `config_store`'s value invariants.
     *
     * Kept in step with the server deliberately: without it the whole
     * 400-validation class — the closed sets, the account shapes, the
     * finite non-negative rate — is invisible under `VITE_MOCK_API=1`, so a
     * form that mishandles a rejection develops the bug in dev and only shows
     * it in production.
     */
    function isAccount(value: string): boolean {
      const parts = value.split(':');
      if (parts.length < 2) return false;
      return parts.every((part, index) => {
        if (!/^[^\W_](?:[^\W_]|-)*$/u.test(part)) return false;
        const first = part[0];
        if (index === 0) return /\p{L}/u.test(first) && first === first.toUpperCase();
        return /\p{Nd}/u.test(first) || (/\p{L}/u.test(first) && first === first.toUpperCase());
      });
    }

    /** Only the fields actually changed are checked, as the store does. */
    function validateFields(kind: string, fields: any, current: any | null): string | null {
      const changed = (name: string) =>
        name in fields && (!current || fields[name] !== current[name]);

      for (const name of ACCOUNT_FIELDS) {
        if (!changed(name) || !fields[name]) continue;
        if (typeof fields[name] !== 'string' || !isAccount(fields[name])) {
          return `invalid ${name}: ${JSON.stringify(fields[name])} — expected a beancount account like Assets:Accounts-Receivable`;
        }
      }
      if (changed('currency') && fields.currency) {
        if (!/^[A-Z](?:[A-Z0-9'._-]*[A-Z0-9])?$/.test(fields.currency)) {
          return `invalid currency: ${JSON.stringify(fields.currency)} — expected a commodity like USD`;
        }
      }
      if (kind === 'service') {
        if (changed('type') && fields.type && !SERVICE_TYPES.includes(fields.type)) {
          return `invalid type: ${JSON.stringify(fields.type)} — expected one of ${SERVICE_TYPES.join(', ')}`;
        }
        if (changed('rate') && fields.rate != null) {
          const rate = Number(fields.rate);
          if (!Number.isFinite(rate) || rate < 0) {
            return `invalid rate: ${JSON.stringify(fields.rate)} — expected a finite amount >= 0`;
          }
        }
      }
      if (kind === 'client') {
        if (changed('schedule') && fields.schedule && !CLIENT_SCHEDULES.includes(fields.schedule)) {
          return `invalid schedule: ${JSON.stringify(fields.schedule)} — expected one of ${CLIENT_SCHEDULES.join(', ')}`;
        }
        if (changed('terms') && fields.terms != null) {
          const asNumber = Number(String(fields.terms).trim());
          if (String(fields.terms).trim() === '') {
            return 'invalid terms: expected a number of days or a label';
          }
          if (Number.isInteger(asNumber) && asNumber < 0) {
            return `invalid terms: ${JSON.stringify(fields.terms)} — expected at least 0`;
          }
        }
      }
      if (kind === 'company' && changed('logo') && fields.logo) {
        const value = String(fields.logo).replace(/\\/g, '/');
        if (value.startsWith('/') || value.startsWith('~') || value.split('/').includes('..')) {
          return `invalid logo: ${JSON.stringify(fields.logo)} — expected a path inside the accounting folder, like invoices/logo.png`;
        }
      }
      return null;
    }

    /**
     * One CRUD handler for all three config collections.
     *
     * Reproduces the guarantees the pages branch on: 409 on a duplicate
     * create (create means create), 400 on a malformed key or value, 404 on a
     * missing record (including a `?create=false` PUT), and a `references`
     * payload on delete. The per-collection delete guard is passed in — the
     * asymmetry between them is the point.
     */
    function collectionCrud<T extends { key: string }>(opts: {
      kind: string;
      rows: T[];
      make: (key: string, body: any) => T;
      guard?: (row: T) => { error: string; references: any } | null;
      references?: (row: T) => any;
    }) {
      // PUT on a missing key 404s here unconditionally, which is what the forms
      // ask for with `?create=false`. The real routes still upsert without it,
      // for `ensure`-style CLI callers the mock has no equivalent of.
      return (method: string, body: any, itemKey: string | null) => {
        const { kind, rows } = opts;
        const fieldsOf = (b: any) => {
          const { key: _ignored, ...rest } = b ?? {};
          return rest;
        };
        if (!itemKey) {
          if (method === 'POST') {
            const key = body?.key;
            if (!key || !KEY_RE.test(key)) {
              return { __status: 400, status: 'error', error: 'invalid key' };
            }
            // Client keys are lowercase-only: work entries store the client
            // lowercased, so a mixed-case key matches none of them.
            if (kind === 'client' && key !== key.toLowerCase()) {
              return {
                __status: 400,
                status: 'error',
                error: `invalid client key: ${JSON.stringify(key)} — use lowercase.`,
              };
            }
            if (rows.some((r) => r.key === key)) {
              return {
                __status: 409,
                status: 'error',
                error: `${kind} '${key}' already exists`,
              };
            }
            const invalid = validateFields(kind, fieldsOf(body), null);
            if (invalid) return { __status: 400, status: 'error', error: invalid };
            const created = opts.make(key, body);
            rows.push(created);
            return { status: 'ok', state: 'created', [kind]: created };
          }
          return undefined;
        }

        const row = rows.find((r) => r.key === itemKey);
        if (!row) {
          return { __status: 404, status: 'error', error: `${kind} '${itemKey}' not found` };
        }
        if (method === 'PUT') {
          const invalid = validateFields(kind, fieldsOf(body), row);
          if (invalid) return { __status: 400, status: 'error', error: invalid };
          for (const [k, v] of Object.entries(body ?? {})) {
            if (k === 'key') continue;
            (row as any)[k] = v;
          }
          return { status: 'ok', state: 'updated', [kind]: row };
        }
        if (method === 'DELETE') {
          const refused = opts.guard?.(row);
          if (refused) {
            return {
              __status: 409,
              status: 'error',
              error: refused.error,
              references: refused.references,
            };
          }
          rows.splice(rows.indexOf(row), 1);
          return { status: 'ok', removed: true, references: opts.references?.(row) ?? {} };
        }
        return undefined;
      };
    }

    const clientCrud = collectionCrud<ClientCfg>({
      kind: 'client',
      rows: clientConfigs,
      make: (key, body) => newClient({ ...body, key }),
      // Soft: entries and invoices survive a missing client — only the
      // display name degrades to the raw key. Matched case-insensitively,
      // since work entries store the client lowercased.
      references: (row) => ({
        work_entries: work.filter((w) => w.client.toLowerCase() === row.key.toLowerCase()).length,
      }),
    });

    const entityCrud = collectionCrud<Entity>({
      kind: 'company',
      rows: entities,
      make: (key, body) => ({
        key,
        name: body?.name ?? key,
        address: body?.address ?? '',
        email: body?.email ?? '',
        payment_instructions: body?.payment_instructions ?? '',
        logo: body?.logo ?? '',
        ar_account: body?.ar_account ?? '',
        bank_account: body?.bank_account ?? '',
        currency: body?.currency ?? '',
      }),
      // Strict: a client whose entity vanished silently bills under a
      // different legal entity on the next generated PDF. Four ways to depend
      // on one — named by a client, pinned by a work entry (which outranks the
      // client's), stored as the default, or falling back to it with a blank
      // entity. The effective default is the entity actually resolved to,
      // which is the first one when the stored default names nothing.
      guard: (row) => {
        const clients = clientConfigs.filter((c) => c.entity === row.key).map((c) => c.key);
        const pinned = work.filter((w) => w.entity === row.key).length;
        const storedDefault = defaults.default_entity;
        const isDefault = storedDefault === row.key;
        const effectiveDefault = entities.some((e) => e.key === storedDefault)
          ? storedDefault
          : (entities[0]?.key ?? '');
        const fallbackClients =
          effectiveDefault === row.key ? clientConfigs.filter((c) => !c.entity).length : 0;
        const references = {
          clients,
          work_entries: pinned,
          default_entity: isDefault,
          default_for_clients: fallbackClients,
          quarantined: [],
        };
        if (!clients.length && !pinned && !isDefault && !fallbackClients) return null;
        let error: string;
        if (clients.length) {
          error = `entity '${row.key}' is used by ${clients.length} client(s): ${clients.join(', ')}`;
        } else if (pinned) {
          error = `entity '${row.key}' is pinned by ${pinned} work entr${pinned === 1 ? 'y' : 'ies'}`;
        } else if (isDefault) {
          error = `entity '${row.key}' is the default entity`;
        } else {
          error = `entity '${row.key}' is the default ${fallbackClients} client(s) bill under — give them an explicit entity first`;
        }
        return { error, references };
      },
    });

    const serviceCrud = collectionCrud<ServiceCfg>({
      kind: 'service',
      rows: services,
      make: (key, body) => ({
        key,
        display_name: body?.display_name ?? key,
        rate: Number(body?.rate ?? 0),
        type: body?.type ?? 'hours',
        income_account: body?.income_account ?? '',
      }),
      // Strictest: deletion unbills future work *and* re-renders every past
      // invoice containing such an entry short.
      guard: (row) => {
        const entries = work.filter((w) => w.service === row.key);
        if (!entries.length) return null;
        const invoices = new Set(entries.filter((w) => w.invoice).map((w) => w.invoice));
        return {
          error:
            `service '${row.key}' is used by ${entries.length} work ` +
            `entr${entries.length === 1 ? 'y' : 'ies'}`,
          references: { work_entries: entries.length, invoices: invoices.size, quarantined: [] },
        };
      },
    });

    return ({ url, method, body }: { url: string; method: string; body?: any }) => {
      if (!url.startsWith(PREFIX)) return undefined;
      const parsed = new URL(url, 'http://mock');
      const path = parsed.pathname.slice(PREFIX.length); // e.g. /transactions
      const q = parsed.searchParams;

      // --- Invoicing config CRUD ---
      const configPath = path.match(/^\/config\/(clients|companies|services)(?:\/([^/]+))?$/);
      if (configPath) {
        const collection = configPath[1];
        const itemKey = configPath[2] ? decodeURIComponent(configPath[2]) : null;
        if (method === 'GET' && !itemKey) {
          if (collection === 'clients') return { status: 'ok', clients: clientConfigs };
          if (collection === 'companies') return { status: 'ok', companies: entities };
          return { status: 'ok', services };
        }
        const crud =
          collection === 'clients'
            ? clientCrud
            : collection === 'companies'
              ? entityCrud
              : serviceCrud;
        const resp = crud(method, body, itemKey);
        if (resp !== undefined) return resp;
      }

      if (path === '/clients' && method === 'GET') {
        // The display shape: business defaults resolved in, unlike
        // /config/clients which stays raw for the edit form.
        return {
          status: 'ok',
          clients: clientConfigs.map((c) => {
            const entityKey = c.entity || defaults.default_entity;
            const entity = entities.find((e) => e.key === entityKey);
            return {
              key: c.key,
              name: c.name,
              email: c.email,
              address: c.address,
              terms: c.terms,
              entity: entityKey,
              entity_name: entity?.name ?? entityKey,
              schedule: c.schedule,
              schedule_day: c.schedule_day,
              ar_account: c.ar_account || defaults.default_ar_account,
            };
          }),
        };
      }

      if (path === '/business-settings' && method === 'GET') {
        return { status: 'ok', entities, services, defaults };
      }

      // --- Work entries (uid routes precede the collection route) ---
      const workItem = path.match(/^\/work\/([^/]+)$/);
      if (workItem) {
        const uid = decodeURIComponent(workItem[1]);
        const idx = work.findIndex((w) => w.uid === uid);
        if (idx < 0) return { __status: 404, status: 'error', error: 'entry not found' };
        const entry = work[idx];
        if (entry.invoice) {
          return { __status: 409, status: 'error', error: 'entry is invoiced' };
        }
        // Optimistic concurrency: without this the conflict banner and its
        // reload path have no dev-mode route at all.
        const sentEtag = method === 'DELETE' ? q.get('etag') : body?.etag;
        if (sentEtag && sentEtag !== workEtag(entry)) {
          return {
            __status: 409,
            status: 'error',
            error: 'entry changed',
            entry: workRow(entry, idx + 1),
          };
        }
        if (method === 'DELETE') {
          work.splice(idx, 1);
          return { status: 'ok', uid };
        }
        if (method === 'PATCH') {
          for (const key of [
            'date',
            'client',
            'service',
            'qty',
            'amount',
            'discount',
            'description',
            'entity',
          ] as const) {
            if (body && key in body) (entry as any)[key] = body[key];
          }
          work.sort((a, b) => a.date.localeCompare(b.date));
          return { status: 'ok', entry: workRow(entry, work.indexOf(entry) + 1) };
        }
        return undefined;
      }

      if (path === '/work' && method === 'POST') {
        const created: Work = {
          uid: `work-${nextWorkUid++}`,
          date: body?.date ?? new Date().toISOString().slice(0, 10),
          client: (body?.client ?? '').toLowerCase(),
          service: body?.service ?? '',
          qty: body?.qty ?? null,
          amount: body?.amount ?? null,
          discount: body?.discount ?? 0,
          description: body?.description ?? '',
          entity: body?.entity ?? '',
          invoice: '',
          paid_date: null,
        };
        if (created.service && !findService(created.service)) {
          return {
            __status: 400,
            status: 'error',
            error: `unknown service: ${created.service}`,
          };
        }
        work.push(created);
        work.sort((a, b) => a.date.localeCompare(b.date));
        return { status: 'ok', entry: workRow(created, work.indexOf(created) + 1) };
      }

      if (path === '/work' && method === 'GET') {
        work.sort((a, b) => a.date.localeCompare(b.date));
        let rows = work.map((w, i) => workRow(w, i + 1));
        const clientKey = q.get('client');
        if (clientKey) rows = rows.filter((r) => r.client === clientKey);
        const period = q.get('period');
        if (period) rows = rows.filter((r) => r.date.startsWith(period));

        const uninvoiced = rows.filter((r) => !r.invoice);
        const totals = {
          uninvoiced_count: uninvoiced.length,
          uninvoiced_amount:
            Math.round(uninvoiced.reduce((s, r) => s + (r.computed_amount ?? 0), 0) * 100) / 100,
          invoiced_count: rows.filter((r) => r.invoice).length,
          paid_count: rows.filter((r) => r.paid_date).length,
        };

        const status = q.get('status') ?? 'all';
        if (status === 'uninvoiced') rows = rows.filter((r) => !r.invoice);
        else if (status === 'invoiced') rows = rows.filter((r) => r.invoice && !r.paid_date);
        else if (status === 'paid') rows = rows.filter((r) => r.paid_date);

        return { status: 'ok', entries: rows, totals };
      }

      // --- Invoice action routes (must precede the broad /invoices match) ---
      const action = path.match(/^\/invoices\/([^/]+)\/(mark-paid|mark-pending|pdf)$/);
      if (action) {
        const number = decodeURIComponent(action[1]);
        const verb = action[2];
        const inv = invoices.find((i) => i.invoice_number === number);
        if (verb === 'pdf') {
          return { status: 'ok', note: 'PDF download only works against the real backend' };
        }
        if (!inv) return { status: 'error', error: 'invoice not found' };
        if (verb === 'mark-paid') {
          inv.status = 'paid';
          inv.paid_date = (body && body.paid_date) || today();
          return { status: 'ok', invoice_number: number, paid_date: inv.paid_date, count: 1 };
        }
        inv.status = 'outstanding';
        delete inv.paid_date;
        return { status: 'ok', invoice_number: number, count: 1 };
      }

      if (path === '/transactions/update' && method === 'POST') {
        const t = transactions.find((x) => x.id === body?.id);
        if (!t) return { status: 'error', error: `Transaction not found: ${body?.id}` };
        if (body.new_payee !== undefined) t.payee = body.new_payee;
        if (body.new_narration !== undefined) t.narration = body.new_narration;
        if (body.new_date !== undefined && body.new_date) t.date = body.new_date;
        if (body.new_account !== undefined) t.account = body.new_account;
        if (body.new_position !== undefined) t.position = body.new_position;
        return { status: 'ok', id: t.id };
      }

      if (path === '/transactions' && method === 'GET') {
        let rows = transactions;
        const account = q.get('account');
        if (account) rows = rows.filter((t) => t.account === account);
        const filter = q.get('filter');
        if (filter) {
          const f = filter.toLowerCase();
          rows = rows.filter(
            (t) => t.payee.toLowerCase().includes(f) || t.narration.toLowerCase().includes(f),
          );
        }
        const perPage = Number(q.get('per_page') || 100);
        const page = Number(q.get('page') || 1);
        const start = (page - 1) * perPage;
        return {
          status: 'ok',
          transactions: rows.slice(start, start + perPage),
          total: rows.length,
          page,
          per_page: perPage,
        };
      }

      if (path === '/postings' && method === 'GET') {
        const account = q.get('account') || '';
        const position = q.get('position') || '';
        return {
          status: 'ok',
          postings: [
            { account, position },
            { account: 'Assets:Checking', position: '' },
          ],
        };
      }

      if (path === '/invoices' && method === 'GET') {
        const showAll = q.get('show_all') === 'true';
        const list = showAll ? invoices : invoices.filter((i) => i.status !== 'paid');
        const outstanding = invoices.filter((i) => i.status === 'outstanding');
        return {
          status: 'ok',
          invoice_count: invoices.length,
          outstanding_count: outstanding.length,
          invoices: list,
        };
      }

      if (path === '/invoice-details' && method === 'GET') {
        const number = q.get('invoice_number') || '';
        return { status: 'ok', invoice_number: number, items: invoiceItems[number] || [] };
      }

      return undefined;
    };
  })(),

  // Health module mock — populated with a realistic shape so the UI is
  // browsable end-to-end without a backend.
  (() => {
    const iso = (daysAgo: number, hour = 9) => {
      const d = new Date();
      d.setUTCDate(d.getUTCDate() - daysAgo);
      d.setUTCHours(hour, 0, 0, 0);
      return d.toISOString();
    };

    // --- Documents (health-document-attachments) --------------------------
    interface MockDocument {
      id: number;
      filename: string;
      original_filename: string | null;
      mime: string;
      byte_size: number;
      source: 'manual' | 'import' | 'agent';
      notes: string | null;
      created_at: string;
    }
    interface MockDocumentLink {
      document_id: number;
      entity_type: 'encounter' | 'diagnosis' | 'immunization';
      entity_id: number;
    }
    const documents: MockDocument[] = [
      {
        id: 1,
        filename: 'after-visit-summary.pdf',
        original_filename: 'After Visit Summary.pdf',
        mime: 'application/pdf',
        byte_size: 184320,
        source: 'import',
        notes: null,
        created_at: iso(40),
      },
      {
        id: 2,
        filename: 'vaccination-card.jpg',
        original_filename: 'vaccination-card.jpg',
        mime: 'image/jpeg',
        byte_size: 921600,
        source: 'agent',
        notes: 'Filed from email',
        created_at: iso(12),
      },
    ];
    const documentLinks: MockDocumentLink[] = [
      { document_id: 1, entity_type: 'encounter', entity_id: 1 },
      { document_id: 1, entity_type: 'diagnosis', entity_id: 1 },
      { document_id: 2, entity_type: 'immunization', entity_id: 1 },
    ];
    let nextDocumentId = 3;

    // The extract routes keep their upload; the review screen passes the id
    // back to /bulk, which links it to every row the import creates.
    const storeImportDocument = (body: unknown): number => {
      const rawBody = typeof body === 'string' ? body : '';
      const name = rawBody.match(/filename="([^"]*)"/)?.[1] || 'import.pdf';
      const doc: MockDocument = {
        id: nextDocumentId++,
        filename: name.replace(/[^A-Za-z0-9._-]/g, '_'),
        original_filename: name,
        mime: name.toLowerCase().endsWith('.pdf') ? 'application/pdf' : 'image/jpeg',
        byte_size: rawBody.length,
        source: 'import',
        notes: null,
        created_at: new Date().toISOString(),
      };
      documents.push(doc);
      return doc.id;
    };
    const linkImportDocument = (
      documentId: unknown,
      entityType: MockDocumentLink['entity_type'],
      ids: number[],
    ) => {
      const id = Number(documentId);
      if (!id || !documents.some((d) => d.id === id)) return null;
      for (const entityId of ids) {
        documentLinks.push({ document_id: id, entity_type: entityType, entity_id: entityId });
      }
      return id;
    };

    const documentDict = (d: MockDocument) => ({
      ...d,
      url: `/istota/api/health/documents/${d.id}/file`,
    });
    const documentsFor = (entityType: string, entityId: number) =>
      documentLinks
        .filter((l) => l.entity_type === entityType && l.entity_id === entityId)
        .map((l) => documents.find((d) => d.id === l.document_id))
        .filter((d): d is MockDocument => Boolean(d))
        .map(documentDict);
    const documentCount = (entityType: string, entityId: number) =>
      documentLinks.filter((l) => l.entity_type === entityType && l.entity_id === entityId).length;

    interface Stat {
      id: number;
      metric: string;
      value: number;
      unit: string;
      measured_at: string;
      source: string;
      source_ref: number | null;
      notes: string | null;
    }
    interface Bio {
      id: number;
      panel_id: number;
      name: string;
      display_name: string | null;
      value: number;
      unit: string;
      ref_range_low: number | null;
      ref_range_high: number | null;
      flag: string | null;
    }
    interface Panel {
      id: number;
      drawn_at: string;
      lab_name: string | null;
      panel_type: string | null;
      source_file: string | null;
      source_mime: string | null;
      ocr_text: string | null;
      draft: boolean;
      notes: string | null;
      encounter_id: number | null;
    }

    const settings = {
      dob: '1985-03-12',
      height_cm: 178,
      sex: 'M' as 'M' | 'F' | null,
      display_units: {
        weight: 'kg' as 'kg' | 'lb',
        height: 'cm' as 'cm' | 'ft_in',
        temp: 'C' as 'C' | 'F',
      },
    };

    // Garmin connection state — toggle via the settings card.
    // Test branches: email "*+mfa*" → MFA flow (code 123456); "*+bad*" → bad creds.
    const garmin: {
      connected: boolean;
      email: string | null;
      last_sync: string | null;
      error: string | null;
      pendingEmail: string | null;
    } = {
      connected: false,
      email: null,
      last_sync: null,
      error: null,
      pendingEmail: null,
    };

    // Encounters / diagnoses — kept in-closure so they survive across
    // requests in dev mode.
    interface Encounter {
      id: number;
      encounter_date: string;
      encounter_type: string;
      provider: string | null;
      facility: string | null;
      specialty: string | null;
      reason: string | null;
      notes: string | null;
      created_at: string;
    }
    interface Diagnosis {
      id: number;
      name: string;
      icd10: string | null;
      status: 'active' | 'resolved' | 'chronic';
      date_diagnosed: string | null;
      date_resolved: string | null;
      encounter_id: number | null;
      severity: 'mild' | 'moderate' | 'severe' | null;
      notes: string | null;
      created_at: string;
    }
    let nextEncounterId = 1;
    let nextDiagnosisId = 1;
    const encounters: Encounter[] = [
      {
        id: nextEncounterId++,
        encounter_date: '2025-09-15',
        encounter_type: 'visit',
        provider: 'Dr. Patel',
        facility: 'Riverside Clinic',
        specialty: 'primary_care',
        reason: 'Annual physical',
        notes: 'All clear. Recommended continuing exercise routine.',
        created_at: new Date().toISOString(),
      },
      {
        id: nextEncounterId++,
        encounter_date: '2026-03-04',
        encounter_type: 'procedure',
        provider: 'Dr. Cohen',
        facility: 'Riverside Clinic',
        specialty: 'gastroenterology',
        reason: 'Screening colonoscopy',
        notes: 'Grade I-II internal hemorrhoids found. No polyps. Follow-up in 5 years.',
        created_at: new Date().toISOString(),
      },
    ];
    const diagnoses: Diagnosis[] = [
      {
        id: nextDiagnosisId++,
        name: 'Internal hemorrhoids',
        icd10: 'K64.0',
        status: 'active',
        date_diagnosed: '2026-03-04',
        date_resolved: null,
        encounter_id: 2,
        severity: 'mild',
        notes: 'Found on screening colonoscopy. No active bleeding.',
        created_at: new Date().toISOString(),
      },
      {
        id: nextDiagnosisId++,
        name: 'Seasonal allergies',
        icd10: 'J30.2',
        status: 'chronic',
        date_diagnosed: null,
        date_resolved: null,
        encounter_id: null,
        severity: null,
        notes: null,
        created_at: new Date().toISOString(),
      },
      // The three below exist to stress the conditions card grid: a name long
      // enough to wrap several lines, a bare row with no tags and no encounter,
      // and a severe one carrying notes.
      {
        id: nextDiagnosisId++,
        name: 'Bilateral patellofemoral pain syndrome with anterior knee crepitus',
        icd10: 'M22.2X9',
        status: 'active',
        date_diagnosed: '2026-05-19',
        date_resolved: null,
        encounter_id: 1,
        severity: 'moderate',
        notes: null,
        created_at: new Date().toISOString(),
      },
      {
        id: nextDiagnosisId++,
        name: 'Tension headache',
        icd10: null,
        status: 'active',
        date_diagnosed: '2026-02-11',
        date_resolved: null,
        encounter_id: null,
        severity: null,
        notes: null,
        created_at: new Date().toISOString(),
      },
      {
        id: nextDiagnosisId++,
        name: 'Iron deficiency anemia',
        icd10: 'D50.9',
        status: 'active',
        date_diagnosed: '2026-06-02',
        date_resolved: null,
        encounter_id: 2,
        severity: 'severe',
        notes: 'Ferritin 11 ng/mL. Started oral iron; recheck ferritin in 12 weeks.',
        created_at: new Date().toISOString(),
      },
    ];

    // Many-to-many: a condition is seen by a GP, a specialist and a follow-up.
    // Seeded from each row's legacy scalar, exactly as the server migration
    // does, plus one extra link so multi-encounter conditions are visible in
    // dev without having to click one together first.
    const diagnosisEncounters: { diagnosis_id: number; encounter_id: number }[] = diagnoses
      .filter((d) => d.encounter_id !== null)
      .map((d) => ({ diagnosis_id: d.id, encounter_id: d.encounter_id as number }));
    // Iron deficiency anemia: found at encounter 2, followed up at 1.
    diagnosisEncounters.push({ diagnosis_id: 5, encounter_id: 1 });

    /** Linked encounter ids for one condition, newest encounter first. */
    const encounterIdsFor = (diagnosisId: number): number[] =>
      diagnosisEncounters
        .filter((l) => l.diagnosis_id === diagnosisId)
        .map((l) => l.encounter_id)
        .sort((a, b) => {
          const ea = encounters.find((e) => e.id === a);
          const eb = encounters.find((e) => e.id === b);
          const da = ea?.encounter_date ?? '';
          const db = eb?.encounter_date ?? '';
          if (da !== db) return db.localeCompare(da);
          return b - a;
        });

    /** Serialize a diagnosis the way the server does. */
    const withLinks = (d: Diagnosis) => ({ ...d, encounter_ids: encounterIdsFor(d.id) });

    interface Immunization {
      id: number;
      name: string;
      product_name: string | null;
      date_given: string;
      manufacturer: string | null;
      dose_label: string | null;
      lot_number: string | null;
      route: string | null;
      site: string | null;
      administered_by: string | null;
      facility: string | null;
      encounter_id: number | null;
      cvx_code: string | null;
      notes: string | null;
      source: string;
      created_at: string;
    }
    let nextImmunizationId = 1;
    const immunizations: Immunization[] = [
      {
        id: nextImmunizationId++,
        name: 'Influenza',
        product_name: 'Fluzone Trivalent',
        date_given: '2025-11-28',
        manufacturer: 'Sanofi',
        dose_label: 'Annual 2025-26',
        lot_number: null,
        route: 'IM',
        site: 'left deltoid',
        administered_by: null,
        facility: 'CVS Pharmacy',
        encounter_id: null,
        cvx_code: null,
        notes: null,
        source: 'manual',
        created_at: new Date().toISOString(),
      },
      {
        id: nextImmunizationId++,
        name: 'Influenza',
        product_name: 'Fluzone Quadrivalent',
        date_given: '2023-10-23',
        manufacturer: 'Sanofi',
        dose_label: null,
        lot_number: null,
        route: 'IM',
        site: null,
        administered_by: null,
        facility: 'CVS Pharmacy',
        encounter_id: null,
        cvx_code: null,
        notes: null,
        source: 'manual',
        created_at: new Date().toISOString(),
      },
      {
        id: nextImmunizationId++,
        name: 'Tdap',
        product_name: 'Boostrix',
        date_given: '2016-12-01',
        manufacturer: 'GSK',
        dose_label: null,
        lot_number: null,
        route: 'IM',
        site: null,
        administered_by: null,
        facility: null,
        encounter_id: null,
        cvx_code: null,
        notes: null,
        source: 'manual',
        created_at: new Date().toISOString(),
      },
      {
        id: nextImmunizationId++,
        name: 'COVID-19',
        product_name: 'Janssen/J&J',
        date_given: '2021-03-17',
        manufacturer: 'Janssen',
        dose_label: null,
        lot_number: null,
        route: 'IM',
        site: null,
        administered_by: null,
        facility: null,
        encounter_id: null,
        cvx_code: null,
        notes: 'External Administration',
        source: 'manual',
        created_at: new Date().toISOString(),
      },
      {
        id: nextImmunizationId++,
        name: 'Typhoid',
        product_name: 'Typhim Vi',
        date_given: '2023-10-23',
        manufacturer: 'Sanofi',
        dose_label: null,
        lot_number: null,
        route: 'IM',
        site: null,
        administered_by: null,
        facility: null,
        encounter_id: null,
        cvx_code: null,
        notes: null,
        source: 'manual',
        created_at: new Date().toISOString(),
      },
    ];

    let nextStatId = 1;
    const stats: Stat[] = [];
    // Weight — daily-ish, 2 years of history with a slow downward drift
    // followed by a stabilization around 82 kg.
    for (let i = 730; i >= 0; i -= 2) {
      const trend = 86 - (86 - 82) * Math.min(1, (730 - i) / 500);
      const noise = (Math.random() - 0.5) * 0.6;
      stats.push({
        id: nextStatId++,
        metric: 'weight',
        value: Math.round((trend + noise) * 10) / 10,
        unit: 'kg',
        measured_at: iso(i),
        source: 'manual',
        source_ref: null,
        notes: null,
      });
    }
    // Resting HR — every 3-4 days, ~60 bpm with seasonal swings.
    for (let i = 365; i >= 0; i -= 4) {
      const trend = 60 + Math.sin(i / 30) * 3;
      const noise = (Math.random() - 0.5) * 4;
      stats.push({
        id: nextStatId++,
        metric: 'resting_hr',
        value: Math.max(50, Math.round(trend + noise)),
        unit: 'bpm',
        measured_at: iso(i),
        source: 'manual',
        source_ref: null,
        notes: null,
      });
    }
    // Body fat % — bi-weekly
    for (let i = 365; i >= 0; i -= 14) {
      const trend = 19 - (19 - 17) * Math.min(1, (365 - i) / 365);
      stats.push({
        id: nextStatId++,
        metric: 'body_fat_pct',
        value: Math.round((trend + (Math.random() - 0.5) * 0.6) * 10) / 10,
        unit: '%',
        measured_at: iso(i),
        source: 'manual',
        source_ref: null,
        notes: null,
      });
    }
    // Body temp — sparse, every few weeks
    for (let i = 180; i >= 0; i -= 20) {
      stats.push({
        id: nextStatId++,
        metric: 'body_temp',
        value: Math.round((36.6 + (Math.random() - 0.5) * 0.5) * 10) / 10,
        unit: '°C',
        measured_at: iso(i),
        source: 'manual',
        source_ref: null,
        notes: null,
      });
    }
    // SpO2 — weekly
    for (let i = 120; i >= 0; i -= 7) {
      stats.push({
        id: nextStatId++,
        metric: 'blood_oxygen',
        value: 97 + Math.floor(Math.random() * 2),
        unit: '%',
        measured_at: iso(i),
        source: 'manual',
        source_ref: null,
        notes: null,
      });
    }
    // Blood pressure — every few days
    for (let i = 180; i >= 0; i -= 3) {
      const sys = 118 + Math.round(Math.random() * 12);
      const dia = 74 + Math.round(Math.random() * 10);
      stats.push({
        id: nextStatId++,
        metric: 'blood_pressure_systolic',
        value: sys,
        unit: 'mmHg',
        measured_at: iso(i),
        source: 'manual',
        source_ref: null,
        notes: null,
      });
      stats.push({
        id: nextStatId++,
        metric: 'blood_pressure_diastolic',
        value: dia,
        unit: 'mmHg',
        measured_at: iso(i),
        source: 'manual',
        source_ref: null,
        notes: null,
      });
    }

    // Panels: a multi-year longitudinal record + one draft awaiting review.
    const panels: Panel[] = [
      {
        id: 1,
        drawn_at: '2018-01-10',
        lab_name: 'Kaiser, Los Angeles CA',
        panel_type: 'CBC + CMP + Lipid',
        source_file: null,
        source_mime: null,
        ocr_text: null,
        draft: false,
        notes: null,
        encounter_id: null,
      },
      {
        id: 2,
        drawn_at: '2019-04-04',
        lab_name: 'Kaiser, Los Angeles CA',
        panel_type: 'CBC + Lipid',
        source_file: null,
        source_mime: null,
        ocr_text: null,
        draft: false,
        notes: null,
        encounter_id: null,
      },
      {
        id: 3,
        drawn_at: '2021-06-23',
        lab_name: 'Kaiser, Los Angeles CA',
        panel_type: 'CBC + Lipid',
        source_file: null,
        source_mime: null,
        ocr_text: null,
        draft: false,
        notes: null,
        encounter_id: null,
      },
      {
        id: 4,
        drawn_at: '2022-05-03',
        lab_name: 'Kaiser, Los Angeles CA',
        panel_type: 'CBC + Lipid',
        source_file: null,
        source_mime: null,
        ocr_text: null,
        draft: false,
        notes: null,
        encounter_id: null,
      },
      {
        id: 5,
        drawn_at: '2023-09-01',
        lab_name: 'Kaiser, Los Angeles CA',
        panel_type: 'CBC + CMP + Lipid',
        source_file: null,
        source_mime: null,
        ocr_text: null,
        draft: false,
        notes: null,
        encounter_id: null,
      },
      {
        id: 6,
        drawn_at: '2024-07-27',
        lab_name: 'Kaiser, Los Angeles CA',
        panel_type: 'CBC + CMP + Lipid',
        source_file: null,
        source_mime: null,
        ocr_text: null,
        draft: false,
        notes: null,
        encounter_id: null,
      },
      {
        id: 7,
        drawn_at: '2025-11-28',
        lab_name: 'Kaiser, Los Angeles CA',
        panel_type: 'CBC + CMP + Lipid',
        source_file: null,
        source_mime: null,
        ocr_text: null,
        draft: false,
        notes: null,
        encounter_id: 1,
      },
      {
        id: 8,
        drawn_at: '2026-04-22',
        lab_name: 'Quest Diagnostics',
        panel_type: 'Lipid + Thyroid + Iron + Vitamins',
        source_file: null,
        source_mime: null,
        ocr_text: null,
        draft: false,
        notes: 'Pre-surgical workup',
        encounter_id: 2,
      },
      {
        id: 9,
        drawn_at: '2026-05-09',
        lab_name: 'Quest Diagnostics',
        panel_type: 'CBC + CMP + Lipid',
        source_file: null,
        source_mime: null,
        ocr_text: null,
        draft: true,
        notes: 'Pending review',
        encounter_id: null,
      },
    ];

    const biomarkers: Bio[] = [];
    let nextBioId = 1;
    const seed = (
      panelId: number,
      items: [string, number, string, number | null, number | null, string | null][],
    ) => {
      for (const [name, value, unit, low, high, flag] of items) {
        biomarkers.push({
          id: nextBioId++,
          panel_id: panelId,
          name,
          display_name: null,
          value,
          unit,
          ref_range_low: low,
          ref_range_high: high,
          flag,
        });
      }
    };

    // 2018-01-10 — Kaiser, LA
    seed(1, [
      ['WBC', 4.9, '10^3/uL', 4.0, 11.0, null],
      ['RBC', 4.61, '10^6/uL', 4.5, 5.9, null],
      ['Hemoglobin', 14.8, 'g/dL', 13.5, 17.5, null],
      ['Hematocrit', 42.5, '%', 41, 53, null],
      ['MCV', 92.2, 'fL', 80, 100, null],
      ['MCH', 32.1, 'pg', 27, 33, null],
      ['MCHC', 34.8, 'g/dL', 32, 36, null],
      ['RDW', 13.3, '%', 11.5, 14.5, null],
      ['Platelets', 242, '10^3/uL', 150, 400, null],
      ['Creatinine', 0.93, 'mg/dL', 0.74, 1.35, null],
      ['eGFR', 107, 'mL/min/1.73m^2', 60, null, null],
      ['Glucose', 96, 'mg/dL', 70, 99, null],
      ['Bilirubin_Total', 0.7, 'mg/dL', 0.1, 1.2, null],
      ['ALT', 22, 'U/L', 7, 56, null],
      ['Cholesterol_Total', 193, 'mg/dL', null, 200, null],
      ['Triglycerides', 97, 'mg/dL', null, 150, null],
      ['HDL', 67, 'mg/dL', 40, null, null],
      ['LDL', 107, 'mg/dL', null, 100, 'H'],
      ['Cholesterol_HDL_Ratio', 2.9, 'ratio', null, 5.0, null],
    ]);
    // 2019-04-04
    seed(2, [
      ['WBC', 5.6, '10^3/uL', 4.0, 11.0, null],
      ['RBC', 4.73, '10^6/uL', 4.5, 5.9, null],
      ['Hemoglobin', 15.0, 'g/dL', 13.5, 17.5, null],
      ['Hematocrit', 43.8, '%', 41, 53, null],
      ['Platelets', 274, '10^3/uL', 150, 400, null],
      ['Creatinine', 1.02, 'mg/dL', 0.74, 1.35, null],
      ['Glucose', 89, 'mg/dL', 70, 99, null],
      ['ALT', 19, 'U/L', 7, 56, null],
      ['Vitamin_D', 12, 'ng/mL', 30, 100, 'L'],
      ['HbA1c', 5.4, '%', null, 5.6, null],
      ['Cholesterol_Total', 201, 'mg/dL', null, 200, 'H'],
      ['Triglycerides', 98, 'mg/dL', null, 150, null],
      ['HDL', 57, 'mg/dL', 40, null, null],
      ['LDL', 124, 'mg/dL', null, 100, 'H'],
      ['Cholesterol_HDL_Ratio', 3.5, 'ratio', null, 5.0, null],
    ]);
    // 2021-06-23
    seed(3, [
      ['WBC', 5.1, '10^3/uL', 4.0, 11.0, null],
      ['RBC', 4.7, '10^6/uL', 4.5, 5.9, null],
      ['Hemoglobin', 14.8, 'g/dL', 13.5, 17.5, null],
      ['Hematocrit', 43.3, '%', 41, 53, null],
      ['Platelets', 268, '10^3/uL', 150, 400, null],
      ['Creatinine', 0.94, 'mg/dL', 0.74, 1.35, null],
      ['Glucose', 90, 'mg/dL', 70, 99, null],
      ['ALT', 16, 'U/L', 7, 56, null],
      ['Vitamin_D', 14, 'ng/mL', 30, 100, 'L'],
      ['HbA1c', 5.3, '%', null, 5.6, null],
      ['Cholesterol_Total', 187, 'mg/dL', null, 200, null],
      ['Triglycerides', 95, 'mg/dL', null, 150, null],
      ['HDL', 53, 'mg/dL', 40, null, null],
      ['LDL', 115, 'mg/dL', null, 100, 'H'],
      ['Cholesterol_HDL_Ratio', 3.5, 'ratio', null, 5.0, null],
    ]);
    // 2022-05-03
    seed(4, [
      ['WBC', 5.4, '10^3/uL', 4.0, 11.0, null],
      ['RBC', 4.59, '10^6/uL', 4.5, 5.9, null],
      ['Hemoglobin', 14.5, 'g/dL', 13.5, 17.5, null],
      ['Hematocrit', 41.4, '%', 41, 53, null],
      ['Platelets', 268, '10^3/uL', 150, 400, null],
      ['Creatinine', 1.0, 'mg/dL', 0.74, 1.35, null],
      ['Glucose', 89, 'mg/dL', 70, 99, null],
      ['ALT', 28, 'U/L', 7, 56, null],
      ['Vitamin_D', 14, 'ng/mL', 30, 100, 'L'],
      ['HbA1c', 5.3, '%', null, 5.6, null],
      ['Cholesterol_Total', 224, 'mg/dL', null, 200, 'H'],
      ['Triglycerides', 121, 'mg/dL', null, 150, null],
      ['HDL', 55, 'mg/dL', 40, null, null],
      ['LDL', 147, 'mg/dL', null, 100, 'H'],
      ['Cholesterol_HDL_Ratio', 4.1, 'ratio', null, 5.0, null],
    ]);
    // 2023-09-01
    seed(5, [
      ['WBC', 4.4, '10^3/uL', 4.0, 11.0, null],
      ['RBC', 4.54, '10^6/uL', 4.5, 5.9, null],
      ['Hemoglobin', 14.2, 'g/dL', 13.5, 17.5, null],
      ['Hematocrit', 42.6, '%', 41, 53, null],
      ['Platelets', 250, '10^3/uL', 150, 400, null],
      ['Sodium', 140, 'mmol/L', 135, 145, null],
      ['Potassium', 4.1, 'mmol/L', 3.5, 5.0, null],
      ['Chloride', 104, 'mmol/L', 96, 106, null],
      ['CO2', 30, 'mmol/L', 22, 29, 'H'],
      ['Creatinine', 1.12, 'mg/dL', 0.74, 1.35, null],
      ['ALT', 18, 'U/L', 7, 56, null],
      ['Vitamin_D', 11, 'ng/mL', 30, 100, 'L'],
      ['HbA1c', 5.3, '%', null, 5.6, null],
      ['Cholesterol_Total', 192, 'mg/dL', null, 200, null],
      ['Triglycerides', 129, 'mg/dL', null, 150, null],
      ['HDL', 56, 'mg/dL', 40, null, null],
      ['LDL', 113, 'mg/dL', null, 100, 'H'],
      ['Cholesterol_HDL_Ratio', 3.4, 'ratio', null, 5.0, null],
    ]);
    // 2024-07-27
    seed(6, [
      ['WBC', 6.4, '10^3/uL', 4.0, 11.0, null],
      ['RBC', 4.73, '10^6/uL', 4.5, 5.9, null],
      ['Hemoglobin', 14.5, 'g/dL', 13.5, 17.5, null],
      ['Hematocrit', 43.5, '%', 41, 53, null],
      ['Platelets', 278, '10^3/uL', 150, 400, null],
      ['Sodium', 139, 'mmol/L', 135, 145, null],
      ['Potassium', 4.4, 'mmol/L', 3.5, 5.0, null],
      ['Chloride', 102, 'mmol/L', 96, 106, null],
      ['CO2', 28, 'mmol/L', 22, 29, null],
      ['Creatinine', 1.04, 'mg/dL', 0.74, 1.35, null],
      ['ALT', 20, 'U/L', 7, 56, null],
      ['HbA1c', 5.5, '%', null, 5.6, null],
      ['Cholesterol_Total', 229, 'mg/dL', null, 200, 'H'],
      ['Triglycerides', 165, 'mg/dL', null, 150, 'H'],
      ['HDL', 51, 'mg/dL', 40, null, null],
      ['LDL', 148, 'mg/dL', null, 100, 'H'],
      ['Cholesterol_HDL_Ratio', 4.5, 'ratio', null, 5.0, null],
    ]);
    // 2025-11-28
    seed(7, [
      ['WBC', 6.1, '10^3/uL', 4.0, 11.0, null],
      ['RBC', 4.66, '10^6/uL', 4.5, 5.9, null],
      ['Hemoglobin', 14.6, 'g/dL', 13.5, 17.5, null],
      ['Hematocrit', 42.9, '%', 41, 53, null],
      ['Platelets', 272, '10^3/uL', 150, 400, null],
      ['Sodium', 135, 'mmol/L', 135, 145, null],
      ['Potassium', 4.3, 'mmol/L', 3.5, 5.0, null],
      ['Chloride', 100, 'mmol/L', 96, 106, null],
      ['CO2', 31, 'mmol/L', 22, 29, 'H'],
      ['Creatinine', 0.99, 'mg/dL', 0.74, 1.35, null],
      ['ALT', 24, 'U/L', 7, 56, null],
      ['Vitamin_B12', 412, 'pg/mL', 200, 900, null],
      ['Homocysteine', 11, 'umol/L', null, 10.4, 'H'],
      ['HbA1c', 5.4, '%', null, 5.6, null],
      ['Cholesterol_Total', 219, 'mg/dL', null, 200, 'H'],
      ['Triglycerides', 90, 'mg/dL', null, 150, null],
      ['HDL', 55, 'mg/dL', 40, null, null],
      ['LDL', 148, 'mg/dL', null, 100, 'H'],
      ['Cholesterol_HDL_Ratio', 4.0, 'ratio', null, 5.0, null],
    ]);
    // 2026-04-22 — Quest, expanded panel
    seed(8, [
      ['Cholesterol_Total', 188, 'mg/dL', null, 200, null],
      ['LDL', 108, 'mg/dL', null, 100, 'H'],
      ['HDL', 56, 'mg/dL', 40, null, null],
      ['Triglycerides', 118, 'mg/dL', null, 150, null],
      ['Cholesterol_HDL_Ratio', 3.4, 'ratio', null, 5.0, null],
      ['TSH', 1.8, 'mIU/L', 0.4, 4.0, null],
      ['Free_T3', 3.1, 'pg/mL', 2.3, 4.2, null],
      ['Free_T4', 1.2, 'ng/dL', 0.8, 1.8, null],
      ['Iron', 95, 'ug/dL', 65, 175, null],
      ['Ferritin', 145, 'ng/mL', 30, 400, null],
      ['Iron_Saturation', 32, '%', 20, 50, null],
      ['Vitamin_D', 38, 'ng/mL', 30, 100, null],
      ['Vitamin_B12', 528, 'pg/mL', 200, 900, null],
      ['Homocysteine', 9.2, 'umol/L', null, 10.4, null],
      ['HbA1c', 5.4, '%', null, 5.6, null],
    ]);

    const panelDict = (p: Panel) => {
      const own = biomarkers.filter((b) => b.panel_id === p.id);
      const flagged = own.filter((b) => b.flag).length;
      return {
        id: p.id,
        drawn_at: p.drawn_at,
        lab_name: p.lab_name,
        panel_type: p.panel_type,
        biomarker_count: own.length,
        flagged_count: flagged,
        draft: p.draft,
        notes: p.notes,
        has_source: false,
        encounter_id: p.encounter_id,
      };
    };

    const latestByMetric = (): Record<string, Stat> => {
      const out: Record<string, Stat> = {};
      for (const s of stats) {
        const prev = out[s.metric];
        if (!prev || s.measured_at > prev.measured_at) out[s.metric] = s;
      }
      return out;
    };

    return ({ url, method, body }: { url: string; method: string; body?: any }) => {
      // --- /documents ---------------------------------------------------
      const fileMatch = url.match(/^\/istota\/api\/health\/documents\/(\d+)\/file$/);
      if (fileMatch && method === 'GET') {
        const doc = documents.find((d) => d.id === Number(fileMatch[1]));
        if (!doc) return { error: 'document not found', __status: 404 };
        // A one-page PDF, so the dev browser has something real to open.
        return {
          __raw: '%PDF-1.4\n% mock document\n',
          __contentType: doc.mime,
        };
      }
      const linkMatch = url.match(
        /^\/istota\/api\/health\/documents\/(\d+)\/links\/([a-z]+)\/(\d+)$/,
      );
      if (linkMatch && method === 'DELETE') {
        const id = Number(linkMatch[1]);
        const before = documentLinks.length;
        for (let i = documentLinks.length - 1; i >= 0; i--) {
          const l = documentLinks[i];
          if (
            l.document_id === id &&
            l.entity_type === linkMatch[2] &&
            l.entity_id === Number(linkMatch[3])
          ) {
            documentLinks.splice(i, 1);
          }
        }
        return { status: 'ok', removed: documentLinks.length < before };
      }
      const linksMatch = url.match(/^\/istota\/api\/health\/documents\/(\d+)\/links$/);
      if (linksMatch && method === 'POST') {
        const id = Number(linksMatch[1]);
        if (!documents.some((d) => d.id === id)) {
          return { error: 'document not found', __status: 404 };
        }
        const entityType = body?.entity_type;
        const entityId = Number(body?.entity_id);
        if (!['encounter', 'diagnosis', 'immunization'].includes(entityType)) {
          return { error: `unknown entity type: ${entityType}`, __status: 400 };
        }
        const exists = documentLinks.some(
          (l) => l.document_id === id && l.entity_type === entityType && l.entity_id === entityId,
        );
        if (!exists)
          documentLinks.push({ document_id: id, entity_type: entityType, entity_id: entityId });
        return { status: 'ok', created: !exists };
      }
      const docMatch = url.match(/^\/istota\/api\/health\/documents\/(\d+)$/);
      if (docMatch && method === 'GET') {
        const doc = documents.find((d) => d.id === Number(docMatch[1]));
        if (!doc) return { error: 'document not found', __status: 404 };
        const links = documentLinks
          .filter((l) => l.document_id === doc.id)
          .map((l) => ({ ...l, label: `${l.entity_type} ${l.entity_id}` }));
        return { document: documentDict(doc), links };
      }
      if (docMatch && method === 'DELETE') {
        const id = Number(docMatch[1]);
        const idx = documents.findIndex((d) => d.id === id);
        if (idx === -1) return { error: 'document not found', __status: 404 };
        documents.splice(idx, 1);
        for (let i = documentLinks.length - 1; i >= 0; i--) {
          if (documentLinks[i].document_id === id) documentLinks.splice(i, 1);
        }
        return { status: 'ok' };
      }
      if (url === '/istota/api/health/documents' && method === 'POST') {
        // Multipart arrives as the raw payload; the part headers are ASCII
        // at the front, so the filename survives the utf8 decode.
        const rawBody = typeof body === 'string' ? body : '';
        const name = rawBody.match(/filename="([^"]*)"/)?.[1] || 'upload.pdf';
        const entityType = rawBody.match(/name="entity_type"\r?\n\r?\n([^\r\n-]*)/)?.[1];
        const entityId = rawBody.match(/name="entity_id"\r?\n\r?\n(\d+)/)?.[1];
        const doc: MockDocument = {
          id: nextDocumentId++,
          filename: name.replace(/[^A-Za-z0-9._-]/g, '_'),
          original_filename: name,
          mime: name.toLowerCase().endsWith('.pdf') ? 'application/pdf' : 'image/jpeg',
          byte_size: rawBody.length,
          source: 'manual',
          notes: null,
          created_at: new Date().toISOString(),
        };
        documents.push(doc);
        let linked = false;
        if (entityType && entityId) {
          documentLinks.push({
            document_id: doc.id,
            entity_type: entityType as MockDocumentLink['entity_type'],
            entity_id: Number(entityId),
          });
          linked = true;
        }
        return { ...documentDict(doc), status: 'ok', created: true, linked };
      }
      if (url.startsWith('/istota/api/health/documents') && method === 'GET') {
        const u = new URL(url, 'http://x');
        const entityType = u.searchParams.get('entity_type');
        const entityId = Number(u.searchParams.get('entity_id') || 0);
        if (entityType && entityId) {
          return { documents: documentsFor(entityType, entityId) };
        }
        return { documents: documents.map(documentDict) };
      }

      // /stats endpoints
      if (url.startsWith('/istota/api/health/stats/latest') && method === 'GET') {
        return { stats: latestByMetric() };
      }
      if (url.startsWith('/istota/api/health/stats/series') && method === 'GET') {
        const u = new URL(url, 'http://x');
        const metric = u.searchParams.get('metric') || '';
        const since = u.searchParams.get('since');
        const points = stats
          .filter((s) => s.metric === metric && (!since || s.measured_at >= since))
          .sort((a, b) => a.measured_at.localeCompare(b.measured_at))
          .map((s) => ({ measured_at: s.measured_at, value: s.value, unit: s.unit }));
        return { metric, points };
      }
      if (url.startsWith('/istota/api/health/stats') && method === 'GET') {
        const u = new URL(url, 'http://x');
        const metric = u.searchParams.get('metric');
        const since = u.searchParams.get('since');
        const limit = Number(u.searchParams.get('limit') || 200);
        let rows = [...stats];
        if (metric) rows = rows.filter((s) => s.metric === metric);
        if (since) rows = rows.filter((s) => s.measured_at >= since);
        rows.sort((a, b) => b.measured_at.localeCompare(a.measured_at));
        return { stats: rows.slice(0, limit) };
      }
      if (url === '/istota/api/health/stats' && method === 'POST') {
        const s: Stat = {
          id: nextStatId++,
          metric: body.metric,
          value: Number(body.value),
          unit: body.unit,
          measured_at: body.measured_at || new Date().toISOString(),
          source: body.source || 'manual',
          source_ref: null,
          notes: body.notes ?? null,
        };
        stats.push(s);
        return { status: 'ok', id: s.id };
      }
      const delStatMatch = url.match(/^\/istota\/api\/health\/stats\/(\d+)$/);
      if (delStatMatch && method === 'DELETE') {
        const id = Number(delStatMatch[1]);
        const idx = stats.findIndex((s) => s.id === id);
        if (idx >= 0) stats.splice(idx, 1);
        return { status: 'ok' };
      }

      // /panels endpoints
      if (url === '/istota/api/health/panels' && method === 'GET') {
        return {
          panels: panels
            .slice()
            .sort((a, b) => b.drawn_at.localeCompare(a.drawn_at))
            .map(panelDict),
        };
      }
      if (url.startsWith('/istota/api/health/panels?') && method === 'GET') {
        return {
          panels: panels
            .slice()
            .sort((a, b) => b.drawn_at.localeCompare(a.drawn_at))
            .map(panelDict),
        };
      }
      if (url === '/istota/api/health/panels' && method === 'POST') {
        if (body.encounter_id != null && !encounters.find((e) => e.id === body.encounter_id)) {
          return { error: 'encounter not found' };
        }
        const p: Panel = {
          id: panels.length + 1,
          drawn_at: body.drawn_at,
          lab_name: body.lab_name || null,
          panel_type: body.panel_type || null,
          source_file: null,
          source_mime: null,
          ocr_text: null,
          draft: false,
          notes: body.notes ?? null,
          encounter_id: body.encounter_id ?? null,
        };
        panels.push(p);
        return { status: 'ok', id: p.id };
      }
      const panelDetailMatch = url.match(/^\/istota\/api\/health\/panels\/(\d+)$/);
      if (panelDetailMatch) {
        const id = Number(panelDetailMatch[1]);
        const p = panels.find((x) => x.id === id);
        if (!p) return { error: 'not found' };
        if (method === 'GET') {
          return {
            panel: panelDict(p),
            biomarkers: biomarkers.filter((b) => b.panel_id === id),
            source: { available: false, mime: null },
          };
        }
        if (method === 'PUT') {
          if (typeof body.draft === 'boolean') p.draft = body.draft;
          if (body.lab_name !== undefined) p.lab_name = body.lab_name;
          if (body.panel_type !== undefined) p.panel_type = body.panel_type;
          if (body.notes !== undefined) p.notes = body.notes;
          if (body.encounter_id !== undefined) {
            if (body.encounter_id !== null && !encounters.find((e) => e.id === body.encounter_id)) {
              return { error: 'encounter not found' };
            }
            p.encounter_id = body.encounter_id;
          }
          return { status: 'ok' };
        }
        if (method === 'DELETE') {
          const idx = panels.findIndex((x) => x.id === id);
          if (idx >= 0) panels.splice(idx, 1);
          for (let i = biomarkers.length - 1; i >= 0; i--) {
            if (biomarkers[i].panel_id === id) biomarkers.splice(i, 1);
          }
          return { status: 'ok' };
        }
      }
      const bioMatch = url.match(/^\/istota\/api\/health\/panels\/(\d+)\/biomarkers$/);
      if (bioMatch) {
        const id = Number(bioMatch[1]);
        if (method === 'POST') {
          for (let i = biomarkers.length - 1; i >= 0; i--) {
            if (biomarkers[i].panel_id === id) biomarkers.splice(i, 1);
          }
          const incoming: any[] = body?.biomarkers || [];
          for (const b of incoming) {
            biomarkers.push({
              id: nextBioId++,
              panel_id: id,
              name: b.name,
              display_name: b.display_name ?? null,
              value: Number(b.value),
              unit: b.unit,
              ref_range_low: b.ref_range_low ?? null,
              ref_range_high: b.ref_range_high ?? null,
              flag: b.flag ?? null,
            });
          }
          if (body?.confirm) {
            const p = panels.find((x) => x.id === id);
            if (p) p.draft = false;
          }
          return { status: 'ok', count: incoming.length };
        }
        if (method === 'GET') {
          return { biomarkers: biomarkers.filter((b) => b.panel_id === id) };
        }
      }
      const extractMatch = url.match(/^\/istota\/api\/health\/panels\/(\d+)\/extract$/);
      if (extractMatch && method === 'POST') {
        return {
          biomarkers: [
            {
              name: 'WBC',
              value: 7.4,
              unit: '10^3/uL',
              ref_range_low: 4.0,
              ref_range_high: 11.0,
              flag: null,
            },
            {
              name: 'Hemoglobin',
              value: 15.0,
              unit: 'g/dL',
              ref_range_low: 13.5,
              ref_range_high: 17.5,
              flag: null,
            },
            {
              name: 'LDL',
              value: 112,
              unit: 'mg/dL',
              ref_range_low: null,
              ref_range_high: 100,
              flag: 'H',
            },
            {
              name: 'HDL',
              value: 54,
              unit: 'mg/dL',
              ref_range_low: 40,
              ref_range_high: null,
              flag: null,
            },
          ],
          drawn_at: '2025-11-28',
          lab_name: 'Kaiser',
          panel_type: 'CBC + Lipid Panel',
          warnings: [],
          raw_text: 'Mock OCR text — replace with real extraction output.',
        };
      }

      if (url === '/istota/api/health/csv/import' && method === 'POST') {
        return {
          status: 'ok',
          panels_created: 2,
          panels_replaced: 0,
          panels_skipped: 0,
          biomarkers_created: 8,
          rows_processed: 2,
          warnings: [],
        };
      }

      // /biomarkers endpoints
      if (url.startsWith('/istota/api/health/biomarkers/trend') && method === 'GET') {
        const u = new URL(url, 'http://x');
        const name = u.searchParams.get('name') || '';
        // Match by canonical name OR by alias.
        const ref =
          BIOMARKER_REFS.find((r) => r.name === name) ||
          BIOMARKER_REFS.find((r) =>
            (r.aliases || []).some((a) => a.toLowerCase() === name.toLowerCase()),
          );
        const canonical = ref?.name || name;
        const matches = biomarkers
          .filter((b) => b.name === canonical)
          .map((b) => {
            const p = panels.find((x) => x.id === b.panel_id);
            return { drawn_at: p?.drawn_at || '', value: b.value, unit: b.unit, flag: b.flag };
          })
          .filter(
            (x) => Boolean(x.drawn_at) && panels.find((p) => p.drawn_at === x.drawn_at && !p.draft),
          )
          .sort((a, b) => a.drawn_at.localeCompare(b.drawn_at));
        // Use sex-specific male range if present, else unisex, else widest.
        const lowM = ref?.ref_range_low_m ?? null;
        const highM = ref?.ref_range_high_m ?? null;
        const low = lowM ?? ref?.ref_range_low ?? null;
        const high = highM ?? ref?.ref_range_high ?? null;
        return {
          name: canonical,
          display_name: ref?.display_name || canonical,
          points: matches,
          unit_mismatch: false,
          ref_range_low: low,
          ref_range_high: high,
          unit: ref?.default_unit ?? null,
        };
      }
      if (url === '/istota/api/health/biomarkers/summary' && method === 'GET') {
        const byName = new Map<string, Bio[]>();
        for (const b of biomarkers) {
          const arr = byName.get(b.name) || [];
          arr.push(b);
          byName.set(b.name, arr);
        }
        const summary: any[] = [];
        for (const [name, items] of byName.entries()) {
          items.sort((a, b) => {
            const pa = panels.find((p) => p.id === a.panel_id);
            const pb = panels.find((p) => p.id === b.panel_id);
            return (pa?.drawn_at || '').localeCompare(pb?.drawn_at || '');
          });
          const latestBio = items[items.length - 1];
          const previousBio = items.length > 1 ? items[items.length - 2] : null;
          const drawn = (b: Bio) => panels.find((p) => p.id === b.panel_id)?.drawn_at || '';
          const dir =
            previousBio && latestBio.value > previousBio.value * 1.01
              ? 'up'
              : previousBio && latestBio.value < previousBio.value * 0.99
                ? 'down'
                : 'flat';
          summary.push({
            name,
            latest: {
              drawn_at: drawn(latestBio),
              value: latestBio.value,
              unit: latestBio.unit,
              flag: latestBio.flag,
            },
            previous: previousBio
              ? {
                  drawn_at: drawn(previousBio),
                  value: previousBio.value,
                  unit: previousBio.unit,
                  flag: previousBio.flag,
                }
              : null,
            direction: dir,
            sample_count: items.length,
          });
        }
        summary.sort((a, b) => a.name.localeCompare(b.name));
        return { summary };
      }
      if (url === '/istota/api/health/biomarkers/refs' && method === 'GET') {
        return { refs: BIOMARKER_REFS };
      }

      // Biomarker out-of-range explainer.
      const explainerMatch = url.match(
        /^\/istota\/api\/health\/biomarkers\/([^/]+)\/explainer(?:\?(.*))?$/,
      );
      if (explainerMatch && method === 'GET') {
        const requestedName = decodeURIComponent(explainerMatch[1]);
        const params = new URLSearchParams(explainerMatch[2] || '');
        const direction = params.get('direction');
        if (direction !== 'high' && direction !== 'low') {
          return { error: "direction must be 'high' or 'low'" };
        }
        // Canonicalise via the loaded refs (handles alias lookups).
        const ref =
          BIOMARKER_REFS.find((r) => r.name === requestedName) ||
          BIOMARKER_REFS.find((r) =>
            (r.aliases || []).some((a) => a.toLowerCase() === requestedName.toLowerCase()),
          );
        const canonical = ref?.name || requestedName;
        const displayName = ref?.display_name || canonical;

        const STUBS: Record<string, { summary: string; causes: string[]; mitigations: string[] }> =
          {
            'CO2:high': {
              summary:
                'Elevated CO2 (bicarbonate) can reflect a shift in acid-base balance. A single high reading is rarely meaningful on its own — context, hydration, and trends matter.',
              causes: [
                'Mild dehydration or volume contraction may raise bicarbonate transiently.',
                'Chronic diuretic use is commonly associated with elevated CO2.',
                'Persistent vomiting or low potassium can drive metabolic alkalosis.',
                'Compensatory response to chronic respiratory conditions may show up here.',
                'Antacid-heavy regimens (calcium carbonate, baking soda) can nudge values up.',
              ],
              mitigations: [
                'Consider a repeat test in 2–4 weeks to confirm the trend.',
                'Review hydration status — measure intake and salt loss over recent weeks.',
                'Discuss any diuretics, PPIs, or alkali supplements with your prescriber.',
                'Bring electrolyte panel context (Na, K, Cl) to your clinician for interpretation.',
              ],
            },
            'Vitamin_D:low': {
              summary:
                'Low 25-OH Vitamin D is common, especially in northern latitudes and during winter. It plays roles in bone, immune, and muscle health.',
              causes: [
                'Limited sun exposure or sunscreen use, especially in winter months.',
                'Darker skin can be associated with lower endogenous synthesis at the same exposure.',
                'Malabsorption (celiac, IBD, gastric bypass) reduces dietary uptake.',
                'Obesity may sequester vitamin D in adipose tissue and lower serum levels.',
                'Some medications (corticosteroids, anticonvulsants) accelerate breakdown.',
              ],
              mitigations: [
                'Discuss whether a vitamin D supplement is appropriate with your clinician.',
                'Increase dietary sources (fatty fish, fortified dairy, egg yolks) gradually.',
                'Aim for safe, regular sun exposure — minutes vary by skin tone and season.',
                'Retest in 8–12 weeks after any intervention to gauge response.',
              ],
            },
            'LDL:high': {
              summary:
                'Elevated LDL cholesterol is the strongest routine lipid contributor to atherosclerotic cardiovascular risk. Targets depend on overall risk profile, not a single value.',
              causes: [
                'Diet high in saturated fat, refined carbohydrates, or trans fats.',
                'Genetic factors (familial hypercholesterolemia is more common than appreciated).',
                'Hypothyroidism can raise LDL noticeably.',
                'Sedentary lifestyle and excess body weight are associated with higher LDL.',
                'Some medications (corticosteroids, beta blockers, retinoids) can elevate it.',
              ],
              mitigations: [
                'Discuss overall cardiovascular risk with your clinician — not just the LDL number.',
                'Consider dietary changes emphasising fiber, fish, nuts, and unsaturated fats.',
                'Review activity levels and sleep — both move LDL over time.',
                'If recent results have trended up, ask about thyroid and family-history workup.',
              ],
            },
            'Cholesterol_Total:high': {
              summary:
                'Total cholesterol above ~200 mg/dL is a coarse signal — the breakdown into LDL, HDL, and triglycerides gives a far better picture of cardiovascular risk.',
              causes: [
                'Genetics often dominate, especially when LDL is the bulk of the elevation.',
                'Diet quality (saturated and trans fats) shifts total cholesterol over weeks.',
                'High HDL can elevate total cholesterol without raising risk.',
                'Hypothyroidism and kidney disease are commonly associated.',
                'Pregnancy and the post-partum period can transiently raise total cholesterol.',
              ],
              mitigations: [
                'Look at LDL, HDL, and triglycerides separately rather than total alone.',
                'Discuss whether a calculated risk score is appropriate for the next visit.',
                'Repeat fasted if the prior test was non-fasting.',
                'Review lifestyle changes incrementally rather than chasing a single number.',
              ],
            },
            'Triglycerides:high': {
              summary:
                'Elevated triglycerides are sensitive to recent meals, alcohol, and refined carbohydrates — a single non-fasted value is often not informative on its own.',
              causes: [
                'Non-fasting samples can read 30–50% higher than fasted.',
                'Recent heavy alcohol intake elevates triglycerides for days.',
                'Diets high in refined carbohydrates and fructose drive triglyceride production.',
                'Uncontrolled diabetes and insulin resistance commonly raise triglycerides.',
                'Some medications (estrogens, retinoids, beta blockers, thiazides) are associated.',
              ],
              mitigations: [
                'Re-test fasted (9+ hours, water only) before drawing conclusions.',
                'Discuss alcohol patterns over the past week with your clinician.',
                'Consider reducing refined-carb intake and increasing fiber.',
                'Bring HbA1c context if insulin resistance is a known concern.',
              ],
            },
            'Homocysteine:high': {
              summary:
                'Elevated homocysteine is associated with cardiovascular and cognitive risk over time. It often responds well to specific B-vitamin support.',
              causes: [
                'B12, folate, or B6 deficiency is the most common driver.',
                'MTHFR polymorphisms can reduce homocysteine clearance.',
                'Kidney disease impairs homocysteine excretion.',
                'Hypothyroidism is commonly associated.',
                'Some medications (methotrexate, anticonvulsants, metformin) can raise it.',
              ],
              mitigations: [
                'Discuss B12, folate, and B6 levels with your clinician before supplementing blindly.',
                'Consider methylated B-vitamin forms if MTHFR variants are suspected.',
                'Review thyroid and kidney panels for context.',
                'Re-test in 8–12 weeks after any intervention to gauge response.',
              ],
            },
            'Sodium:low': {
              summary:
                'Mildly low sodium (hyponatremia) typically reflects water balance rather than salt intake — the body is holding more water than usual relative to electrolytes.',
              causes: [
                'Over-hydration (especially in endurance athletes) can dilute serum sodium.',
                'Thiazide diuretics commonly cause mild hyponatremia.',
                'SIADH (a hormone signaling issue) keeps water in the body.',
                'Heart, kidney, or liver dysfunction can shift fluid balance.',
                'Severe vomiting or diarrhea, paired with plain-water replacement, can lower sodium.',
              ],
              mitigations: [
                'Avoid drinking large volumes of plain water on the day of testing.',
                'Discuss any diuretic, SSRI, or chemotherapy use with your prescriber.',
                'Bring osmolality and a urine sodium to the next visit if hyponatremia persists.',
                'Repeat the test — single mildly-low values are common with no clinical relevance.',
              ],
            },
          };

        const key = `${canonical}:${direction}`;
        const stub = STUBS[key];
        if (stub) {
          return {
            name: canonical,
            display_name: displayName,
            direction,
            summary: stub.summary,
            causes: stub.causes,
            mitigations: stub.mitigations,
            disclaimer:
              'Educational information only — not medical advice or diagnosis. Discuss your results with a healthcare professional before acting on them.',
            source: 'cache',
            generated_at: new Date(Date.now() - 86400_000).toISOString(),
          };
        }
        // Generic fallback for any other biomarker.
        return {
          name: canonical,
          display_name: displayName,
          direction,
          summary: `A ${direction} ${displayName} reading sits outside the typical reference range. A single value isn't enough to draw conclusions — trends, recent context, and clinical correlation matter.`,
          causes: [
            'Recent illness, dehydration, or stress can shift values temporarily.',
            'Medications, supplements, and recent meals can move many markers.',
            'Inter-lab and inter-assay variability is real; repeat testing helps confirm.',
          ],
          mitigations: [
            'Discuss the result with your healthcare provider before acting on it.',
            'Consider a repeat test in a few weeks to confirm the trend.',
            'Review recent changes in medication, diet, and lifestyle for context.',
          ],
          disclaimer:
            'Educational information only — not medical advice or diagnosis. Discuss your results with a healthcare professional before acting on them.',
          source: 'fallback',
          generated_at: null,
        };
      }

      // Spreadsheet matrix: confirmed panels × every biomarker, grouped by category.
      if (url === '/istota/api/health/bloodwork/matrix' && method === 'GET') {
        const confirmed = panels
          .filter((p) => !p.draft)
          .sort((a, b) => a.drawn_at.localeCompare(b.drawn_at));
        const seenMarker: Record<string, { unit: string }> = {};
        const values: Record<
          string,
          Record<string, { value: number; unit: string; flag: string | null }>
        > = {};
        for (const p of confirmed) {
          values[String(p.id)] = {};
          for (const b of biomarkers.filter((b) => b.panel_id === p.id)) {
            if (!seenMarker[b.name]) seenMarker[b.name] = { unit: b.unit };
            values[String(p.id)][b.name] = { value: b.value, unit: b.unit, flag: b.flag };
          }
        }
        const refByName = new Map(BIOMARKER_REFS.map((r) => [r.name, r] as const));
        const widestRange = (r: (typeof BIOMARKER_REFS)[number]) => {
          const lows: number[] = [];
          const highs: number[] = [];
          if (r.ref_range_low != null) lows.push(r.ref_range_low);
          if (r.ref_range_low_m != null) lows.push(r.ref_range_low_m);
          if (r.ref_range_low_f != null) lows.push(r.ref_range_low_f);
          if (r.ref_range_high != null) highs.push(r.ref_range_high);
          if (r.ref_range_high_m != null) highs.push(r.ref_range_high_m);
          if (r.ref_range_high_f != null) highs.push(r.ref_range_high_f);
          return [
            lows.length ? Math.min(...lows) : null,
            highs.length ? Math.max(...highs) : null,
          ] as const;
        };
        const catOrder: string[] = [];
        const catMarkers: Record<string, any[]> = {};
        for (const r of BIOMARKER_REFS) {
          if (!catMarkers[r.category]) {
            catOrder.push(r.category);
            catMarkers[r.category] = [];
          }
        }
        for (const name of Object.keys(seenMarker)) {
          const r = refByName.get(name);
          const cat = r?.category || 'Other';
          if (!catMarkers[cat]) {
            catOrder.push(cat);
            catMarkers[cat] = [];
          }
          let low: number | null = null,
            high: number | null = null;
          if (r) [low, high] = widestRange(r);
          catMarkers[cat].push({
            name,
            display_name: r?.display_name || name,
            unit: r?.default_unit || seenMarker[name].unit,
            ref_range_low: low,
            ref_range_high: high,
            category: cat,
          });
        }
        const orderedCats = catOrder
          .filter((c) => catMarkers[c]?.length)
          .map((c) => ({
            name: c,
            markers: [...catMarkers[c]].sort((a, b) =>
              a.display_name.localeCompare(b.display_name),
            ),
          }));
        return {
          categories: orderedCats,
          panels: confirmed.map((p) => ({
            id: p.id,
            drawn_at: p.drawn_at,
            lab_name: p.lab_name,
            panel_type: p.panel_type,
          })),
          values,
        };
      }

      // /settings endpoints
      if (url === '/istota/api/health/settings' && method === 'GET') {
        return { settings };
      }
      if (url === '/istota/api/health/settings' && method === 'PUT') {
        if (body && typeof body === 'object') {
          if ('dob' in body) settings.dob = body.dob;
          if ('height_cm' in body) settings.height_cm = body.height_cm;
          if ('sex' in body) settings.sex = body.sex;
          if (body.display_units) {
            settings.display_units = { ...settings.display_units, ...body.display_units };
          }
        }
        return { status: 'ok', settings };
      }

      // /garmin/* — keep state in this closure so connect/MFA/disconnect/sync
      // all see consistent connection status across calls.
      if (url === '/istota/api/health/garmin/status' && method === 'GET') {
        return {
          connected: garmin.connected,
          email: garmin.connected ? garmin.email : null,
          last_sync: garmin.connected ? garmin.last_sync : null,
          error: garmin.error,
        };
      }
      if (url === '/istota/api/health/garmin/connect' && method === 'POST') {
        if (!body || typeof body !== 'object' || !body.email || !body.password) {
          return { status: 'error', error: 'email and password are required' };
        }
        // MFA branch: any email containing "+mfa" triggers the MFA flow.
        if (typeof body.email === 'string' && body.email.includes('+mfa')) {
          garmin.pendingEmail = body.email;
          return { status: 'mfa_required', prompt: 'Enter Garmin MFA code (mock: 123456)' };
        }
        // Bad-credentials branch: emails containing "+bad" fail.
        if (typeof body.email === 'string' && body.email.includes('+bad')) {
          return { status: 'error', error: 'invalid credentials' };
        }
        garmin.connected = true;
        garmin.email = body.email;
        garmin.last_sync = null;
        garmin.error = null;
        return { status: 'ok' };
      }
      if (url === '/istota/api/health/garmin/mfa' && method === 'POST') {
        if (!body || typeof body !== 'object' || typeof body.code !== 'string') {
          return { status: 'error', error: 'code is required' };
        }
        if (!garmin.pendingEmail) {
          return {
            status: 'error',
            error: 'no pending Garmin auth — restart from /garmin/connect',
          };
        }
        if (body.code !== '123456') {
          return { status: 'error', error: 'invalid MFA code' };
        }
        garmin.connected = true;
        garmin.email = garmin.pendingEmail;
        garmin.last_sync = null;
        garmin.error = null;
        garmin.pendingEmail = null;
        return { status: 'ok' };
      }
      if (url === '/istota/api/health/garmin/disconnect' && method === 'POST') {
        garmin.connected = false;
        garmin.email = null;
        garmin.last_sync = null;
        garmin.error = null;
        garmin.pendingEmail = null;
        return { status: 'ok' };
      }
      if (url === '/istota/api/health/garmin/sync' && method === 'POST') {
        if (!garmin.connected) {
          return {
            inserted: 0,
            skipped: 0,
            errored: 0,
            days_processed: 0,
            errors: ['no Garmin tokens — connect via /garmin/connect'],
            auth_error: true,
          };
        }
        const daysBack = Math.max(1, Math.min(90, Number(body?.days_back) || 7));
        garmin.last_sync = new Date().toISOString();
        const inserted = Math.max(0, 5 * daysBack - 2);
        return {
          inserted,
          skipped: 2,
          errored: 0,
          days_processed: daysBack,
          errors: [],
          auth_error: false,
        };
      }

      // /encounters
      if (url === '/istota/api/health/encounters/extract' && method === 'POST') {
        // Dev fixture: pretend the LLM extracted a single visit from the
        // uploaded paperwork. Real backend route runs OCR + brain call.
        return {
          document_id: storeImportDocument(body),
          mode: 'vision',
          rows: [
            {
              encounter_date: '2026-04-14',
              encounter_type: 'visit',
              provider: 'Dr. Jane Smith, MD',
              facility: 'Kaiser Permanente — Sunset',
              specialty: 'primary care',
              reason: 'Annual physical',
              notes:
                'BP and labs normal. Recommended continuing current exercise routine; follow up in 12 months unless symptomatic.',
              diagnoses: [
                {
                  name: 'Essential hypertension, well controlled',
                  icd10: 'I10',
                  status: 'chronic',
                  severity: 'mild',
                },
              ],
              confidence: 'high',
            },
          ],
          warnings: ['Mock extraction (dev mode) — the real LLM runs against the uploaded file.'],
        };
      }
      if (url === '/istota/api/health/encounters/bulk' && method === 'POST') {
        if (!body || !Array.isArray(body.rows)) return { error: 'rows must be a list' };
        const encIds: number[] = [];
        const diagIds: number[] = [];
        for (let i = 0; i < body.rows.length; i++) {
          const r = body.rows[i];
          if (!r.encounter_date || !r.encounter_type) {
            return { error: `row ${i} missing fields` };
          }
          const enc: Encounter = {
            id: nextEncounterId++,
            encounter_date: String(r.encounter_date),
            encounter_type: String(r.encounter_type),
            provider: r.provider || null,
            facility: r.facility || null,
            specialty: r.specialty || null,
            reason: r.reason || null,
            notes: r.notes || null,
            created_at: new Date().toISOString(),
          };
          encounters.push(enc);
          encIds.push(enc.id);
          for (const d of r.diagnoses || []) {
            if (!d || !d.name) continue;
            const dx: Diagnosis = {
              id: nextDiagnosisId++,
              name: String(d.name),
              icd10: d.icd10 || null,
              status: (d.status as Diagnosis['status']) || 'active',
              date_diagnosed: enc.encounter_date,
              date_resolved: null,
              encounter_id: enc.id,
              severity: (d.severity as Diagnosis['severity']) || null,
              notes: null,
              created_at: new Date().toISOString(),
            };
            diagnoses.push(dx);
            diagIds.push(dx.id);
          }
        }
        if (body.document_id != null && !documents.some((d) => d.id === Number(body.document_id))) {
          return { error: 'document not found', __status: 400 };
        }
        linkImportDocument(body.document_id, 'encounter', encIds);
        linkImportDocument(body.document_id, 'diagnosis', diagIds);
        return {
          status: 'ok',
          ids: encIds,
          count: encIds.length,
          diagnosis_ids: diagIds,
          document_id: body.document_id ?? null,
        };
      }
      if (url.startsWith('/istota/api/health/encounters') && method === 'GET') {
        const encMatch = url.match(/^\/istota\/api\/health\/encounters\/(\d+)$/);
        if (encMatch) {
          const id = Number(encMatch[1]);
          const enc = encounters.find((e) => e.id === id);
          if (!enc) return { error: 'encounter not found' };
          const linkedIds = new Set(
            diagnosisEncounters.filter((l) => l.encounter_id === id).map((l) => l.diagnosis_id),
          );
          const linkedDiag = diagnoses.filter((d) => linkedIds.has(d.id)).map(withLinks);
          const linkedPanels = panels
            .filter((p) => p.encounter_id === id)
            .slice()
            .sort((a, b) => b.drawn_at.localeCompare(a.drawn_at))
            .map(panelDict);
          return {
            encounter: enc,
            diagnoses: linkedDiag,
            panels: linkedPanels,
            documents: documentsFor('encounter', id),
          };
        }
        const u = new URL(url, 'http://x');
        const since = u.searchParams.get('since');
        const until = u.searchParams.get('until');
        const t = u.searchParams.get('type');
        let rows = [...encounters];
        if (since) rows = rows.filter((e) => e.encounter_date >= since);
        if (until) rows = rows.filter((e) => e.encounter_date <= until);
        if (t) rows = rows.filter((e) => e.encounter_type === t);
        rows.sort((a, b) => b.encounter_date.localeCompare(a.encounter_date) || b.id - a.id);
        return {
          encounters: rows.map((e) => ({
            ...e,
            document_count: documentCount('encounter', e.id),
          })),
        };
      }
      if (url === '/istota/api/health/encounters' && method === 'POST') {
        if (!body || typeof body !== 'object') return { error: 'bad body' };
        if (!body.encounter_date || !body.encounter_type) {
          return { error: 'encounter_date and encounter_type are required' };
        }
        const enc: Encounter = {
          id: nextEncounterId++,
          encounter_date: body.encounter_date,
          encounter_type: body.encounter_type,
          provider: body.provider || null,
          facility: body.facility || null,
          specialty: body.specialty || null,
          reason: body.reason || null,
          notes: body.notes || null,
          created_at: new Date().toISOString(),
        };
        encounters.push(enc);
        return { status: 'ok', id: enc.id };
      }
      const encUpdMatch = url.match(/^\/istota\/api\/health\/encounters\/(\d+)$/);
      if (encUpdMatch && method === 'PUT') {
        const id = Number(encUpdMatch[1]);
        const enc = encounters.find((e) => e.id === id);
        if (!enc) return { error: 'encounter not found' };
        const allowed = [
          'encounter_date',
          'encounter_type',
          'provider',
          'facility',
          'specialty',
          'reason',
          'notes',
        ];
        for (const k of allowed) {
          if (body && k in body && body[k] !== undefined) (enc as any)[k] = body[k];
        }
        return { status: 'ok' };
      }
      if (encUpdMatch && method === 'DELETE') {
        const id = Number(encUpdMatch[1]);
        const idx = encounters.findIndex((e) => e.id === id);
        if (idx < 0) return { error: 'encounter not found' };
        encounters.splice(idx, 1);
        // Mirror ON DELETE SET NULL on diagnoses.encounter_id + panels.encounter_id,
        // and ON DELETE CASCADE on diagnosis_encounters — the condition itself
        // survives, along with its links to any other encounter.
        for (const d of diagnoses) {
          if (d.encounter_id === id) d.encounter_id = null;
        }
        for (let i = diagnosisEncounters.length - 1; i >= 0; i--) {
          if (diagnosisEncounters[i].encounter_id === id) diagnosisEncounters.splice(i, 1);
        }
        for (const p of panels) {
          if (p.encounter_id === id) p.encounter_id = null;
        }
        return { status: 'ok' };
      }

      // /diagnoses
      if (url.startsWith('/istota/api/health/diagnoses') && method === 'GET') {
        const diagMatch = url.match(/^\/istota\/api\/health\/diagnoses\/(\d+)$/);
        if (diagMatch) {
          const id = Number(diagMatch[1]);
          const d = diagnoses.find((x) => x.id === id);
          if (!d) return { error: 'diagnosis not found' };
          const linked = encounterIdsFor(id)
            .map((eid) => encounters.find((e) => e.id === eid))
            .filter((e): e is Encounter => e !== undefined);
          return {
            diagnosis: withLinks(d),
            encounters: linked,
            documents: documentsFor('diagnosis', id),
          };
        }
        const u = new URL(url, 'http://x');
        const status = u.searchParams.get('status');
        let rows = [...diagnoses];
        if (status && status !== 'all') rows = rows.filter((d) => d.status === status);
        const statusOrder = { active: 0, chronic: 1, resolved: 2 } as const;
        rows.sort((a, b) => {
          const sa = statusOrder[a.status] ?? 3;
          const sb = statusOrder[b.status] ?? 3;
          if (sa !== sb) return sa - sb;
          return (b.date_diagnosed || '').localeCompare(a.date_diagnosed || '');
        });
        return {
          diagnoses: rows.map((d) => ({
            ...withLinks(d),
            document_count: documentCount('diagnosis', d.id),
          })),
        };
      }
      if (url === '/istota/api/health/diagnoses' && method === 'POST') {
        if (!body || typeof body !== 'object' || !body.name) {
          return { error: 'name is required' };
        }
        const status = body.status || 'active';
        if (!['active', 'resolved', 'chronic'].includes(status)) {
          return { error: 'unknown status' };
        }
        // `encounter_ids` is the real field; `encounter_id` is legacy shorthand
        // for one link. Validate the whole set before creating anything.
        const wanted: number[] = Array.isArray(body.encounter_ids)
          ? body.encounter_ids.map((x: unknown) => Number(x))
          : [];
        if (body.encounter_id != null && !wanted.includes(Number(body.encounter_id))) {
          wanted.unshift(Number(body.encounter_id));
        }
        for (const eid of wanted) {
          if (!Number.isFinite(eid) || !encounters.some((e) => e.id === eid)) {
            return { error: 'encounter not found', __status: 400 };
          }
        }
        const d: Diagnosis = {
          id: nextDiagnosisId++,
          name: String(body.name),
          icd10: body.icd10 || null,
          status,
          date_diagnosed: body.date_diagnosed || null,
          date_resolved: body.date_resolved || null,
          encounter_id: body.encounter_id ?? null,
          severity: body.severity || null,
          notes: body.notes || null,
          created_at: new Date().toISOString(),
        };
        diagnoses.push(d);
        for (const eid of new Set(wanted)) {
          diagnosisEncounters.push({ diagnosis_id: d.id, encounter_id: eid });
        }
        return { status: 'ok', id: d.id };
      }
      const diagUpdMatch = url.match(/^\/istota\/api\/health\/diagnoses\/(\d+)$/);
      if (diagUpdMatch && method === 'PUT') {
        const id = Number(diagUpdMatch[1]);
        const d = diagnoses.find((x) => x.id === id);
        if (!d) return { error: 'diagnosis not found' };
        // Mirrors the server: a non-null encounter_id must name a real
        // encounter, so the link pickers exercise that 400 under the mock.
        if (body && body.encounter_id !== undefined && body.encounter_id !== null) {
          const eid = Number(body.encounter_id);
          if (!Number.isFinite(eid)) {
            return { error: 'encounter_id must be an integer or null', __status: 400 };
          }
          if (!encounters.some((e) => e.id === eid)) {
            return { error: 'encounter not found', __status: 400 };
          }
        }
        // `encounter_ids` replaces the whole set; the legacy scalar replaces it
        // with a single link (or clears it when null).
        let replace: number[] | null = null;
        if (body && Array.isArray(body.encounter_ids)) {
          replace = body.encounter_ids.map((x: unknown) => Number(x));
          for (const eid of replace as number[]) {
            if (!Number.isFinite(eid) || !encounters.some((e) => e.id === eid)) {
              return { error: 'encounter not found', __status: 400 };
            }
          }
        } else if (body && 'encounter_id' in body) {
          replace = body.encounter_id === null ? [] : [Number(body.encounter_id)];
        }
        const allowed = [
          'name',
          'icd10',
          'status',
          'date_diagnosed',
          'date_resolved',
          'encounter_id',
          'severity',
          'notes',
        ];
        for (const k of allowed) {
          if (body && k in body) (d as any)[k] = body[k];
        }
        if (replace !== null) {
          for (let i = diagnosisEncounters.length - 1; i >= 0; i--) {
            if (diagnosisEncounters[i].diagnosis_id === id) diagnosisEncounters.splice(i, 1);
          }
          for (const eid of new Set(replace)) {
            diagnosisEncounters.push({ diagnosis_id: id, encounter_id: eid });
          }
        }
        return { status: 'ok' };
      }
      if (diagUpdMatch && method === 'DELETE') {
        const id = Number(diagUpdMatch[1]);
        const idx = diagnoses.findIndex((x) => x.id === id);
        if (idx < 0) return { error: 'diagnosis not found' };
        diagnoses.splice(idx, 1);
        for (let i = diagnosisEncounters.length - 1; i >= 0; i--) {
          if (diagnosisEncounters[i].diagnosis_id === id) diagnosisEncounters.splice(i, 1);
        }
        return { status: 'ok' };
      }

      // Add / remove one encounter link without touching the rest of the set.
      const diagLinkMatch = url.match(/^\/istota\/api\/health\/diagnoses\/(\d+)\/encounters$/);
      if (diagLinkMatch && method === 'POST') {
        const did = Number(diagLinkMatch[1]);
        if (!diagnoses.some((x) => x.id === did)) {
          return { error: 'diagnosis not found', __status: 404 };
        }
        const eid = Number(body?.encounter_id);
        if (!Number.isFinite(eid) || !encounters.some((e) => e.id === eid)) {
          return { error: 'encounter not found', __status: 400 };
        }
        const exists = diagnosisEncounters.some(
          (l) => l.diagnosis_id === did && l.encounter_id === eid,
        );
        if (!exists) diagnosisEncounters.push({ diagnosis_id: did, encounter_id: eid });
        return { status: 'ok', created: !exists };
      }
      const diagUnlinkMatch = url.match(
        /^\/istota\/api\/health\/diagnoses\/(\d+)\/encounters\/(\d+)$/,
      );
      if (diagUnlinkMatch && method === 'DELETE') {
        const did = Number(diagUnlinkMatch[1]);
        const eid = Number(diagUnlinkMatch[2]);
        const idx = diagnosisEncounters.findIndex(
          (l) => l.diagnosis_id === did && l.encounter_id === eid,
        );
        if (idx < 0) return { error: 'link not found', __status: 404 };
        diagnosisEncounters.splice(idx, 1);
        return { status: 'ok' };
      }

      // /history/summary
      if (url === '/istota/api/health/history/summary' && method === 'GET') {
        const oneYearAgo = new Date(Date.now() - 365 * 86400 * 1000).toISOString().slice(0, 10);
        const fiveYearsAgo = new Date(Date.now() - 5 * 365 * 86400 * 1000)
          .toISOString()
          .slice(0, 10);
        const active = diagnoses.filter((d) => d.status === 'active');
        const chronic = diagnoses.filter((d) => d.status === 'chronic');
        const recent = encounters
          .filter((e) => e.encounter_date >= oneYearAgo)
          .sort((a, b) => b.encounter_date.localeCompare(a.encounter_date));
        const procs = encounters
          .filter((e) => e.encounter_type === 'procedure' && e.encounter_date >= fiveYearsAgo)
          .sort((a, b) => b.encounter_date.localeCompare(a.encounter_date))
          .slice(0, 5);
        return {
          active_diagnoses: active,
          chronic_diagnoses: chronic,
          recent_encounters: recent,
          recent_procedures: procs,
        };
      }

      // /dashboard
      if (url === '/istota/api/health/dashboard' && method === 'GET') {
        const latest = latestByMetric();
        const recent = panels
          .filter((p) => !p.draft)
          .sort((a, b) => b.drawn_at.localeCompare(a.drawn_at))
          .slice(0, 3)
          .map(panelDict);
        const flagged: any[] = [];
        const seen = new Set<string>();
        const sortedPanels = [...panels].sort((a, b) => b.drawn_at.localeCompare(a.drawn_at));
        for (const p of sortedPanels) {
          if (p.draft) continue;
          for (const b of biomarkers.filter((b) => b.panel_id === p.id && b.flag)) {
            if (seen.has(b.name)) continue;
            seen.add(b.name);
            flagged.push({ ...b, panel_id: p.id, drawn_at: p.drawn_at, lab_name: p.lab_name });
          }
        }
        const weight = latest['weight'];
        const bmi =
          weight && settings.height_cm
            ? Math.round((weight.value / Math.pow(settings.height_cm / 100, 2)) * 100) / 100
            : null;
        const activeDiagCount =
          diagnoses.filter((d) => d.status === 'active').length +
          diagnoses.filter((d) => d.status === 'chronic').length;
        const recentEncounters = [...encounters]
          .sort((a, b) => b.encounter_date.localeCompare(a.encounter_date) || b.id - a.id)
          .slice(0, 3);
        return {
          latest_stats: latest,
          bmi,
          recent_panels: recent,
          alerts: flagged.slice(0, 20),
          settings,
          active_diagnoses_count: activeDiagCount,
          recent_encounters: recentEncounters,
        };
      }

      // ---- Immunizations ---------------------------------------------
      function _parseDateUS(raw: string): string | null {
        const iso = raw.match(/^(\d{4})-(\d{2})-(\d{2})$/);
        if (iso) return raw;
        const m = raw.match(/^(\d{1,2})\/(\d{1,2})\/(\d{2,4})$/);
        if (!m) return null;
        let [, mo, dy, yr] = m;
        if (yr.length === 2) yr = parseInt(yr, 10) < 70 ? `20${yr}` : `19${yr}`;
        const d = new Date(`${yr}-${mo.padStart(2, '0')}-${dy.padStart(2, '0')}T00:00:00Z`);
        if (isNaN(d.getTime())) return null;
        return d.toISOString().slice(0, 10);
      }

      function _resolveFamily(product: string): {
        name: string;
        ref?: (typeof IMMUNIZATION_REFS)[number];
      } {
        const p = product.toLowerCase();
        const candidates: Array<{ alias: string; ref: (typeof IMMUNIZATION_REFS)[number] }> = [];
        for (const r of IMMUNIZATION_REFS) {
          candidates.push({ alias: r.name.toLowerCase(), ref: r });
          for (const a of r.aliases || []) candidates.push({ alias: a.toLowerCase(), ref: r });
        }
        candidates.sort((a, b) => b.alias.length - a.alias.length);
        for (const c of candidates) {
          const idx = p.indexOf(c.alias);
          if (idx === -1) continue;
          const before = idx > 0 ? p[idx - 1] : ' ';
          const after = idx + c.alias.length < p.length ? p[idx + c.alias.length] : ' ';
          if (!/[a-z0-9]/.test(before) && !/[a-z0-9]/.test(after)) {
            return { name: c.ref.name, ref: c.ref };
          }
        }
        return { name: 'Unknown' };
      }

      function _parsePaste(text: string) {
        const out: any[] = [];
        for (const rawLine of text.split('\n')) {
          const line = rawLine.trim();
          if (!line) continue;
          let product: string | null = null;
          let date: string | null = null;
          let confidence: string = 'low';
          let m = line.match(/^(.+?)\s*\(\s*Given\s+(\d{1,2}\/\d{1,2}\/\d{2,4})\s*\)\s*$/i);
          if (m) {
            product = m[1].trim();
            date = _parseDateUS(m[2]);
            confidence = date ? 'high' : 'medium';
          } else {
            m = line.match(/^(.+?)\s+(\d{4}-\d{2}-\d{2})\s*$/);
            if (m) {
              product = m[1].trim();
              date = _parseDateUS(m[2]);
              confidence = date ? 'high' : 'medium';
            } else {
              product = line;
              confidence = 'manual';
            }
          }
          const resolved = _resolveFamily(product || '');
          out.push({
            name: resolved.name,
            product_name: product || null,
            date_given: date,
            source_line: line,
            confidence,
            notes: resolved.name === 'Unknown' ? line : null,
          });
        }
        return out;
      }

      function _computeCoverage() {
        const today = new Date();
        const todayMs = today.getTime();
        const out = IMMUNIZATION_REFS.map((ref) => {
          const matching = immunizations.filter((r) => r.name === ref.name);
          const dates = matching
            .map((r) => r.date_given)
            .filter(Boolean)
            .sort();
          const lastGiven = dates.length ? dates[dates.length - 1] : null;
          const doseCount = matching.length;
          let status = 'never_recorded';
          let nextDue: string | null = null;
          let isOverdue = false;
          let daysUntilDue: number | null = null;
          const lastDate = lastGiven ? new Date(lastGiven + 'T00:00:00Z') : null;
          if (ref.schedule === 'risk_based') {
            status = doseCount === 0 ? 'risk_based' : 'up_to_date';
          } else if (ref.schedule === 'annual' || ref.schedule === 'every_10y') {
            if (!lastDate) {
              status = 'never_recorded';
            } else {
              const interval = ref.interval_days ?? (ref.schedule === 'annual' ? 365 : 3650);
              const due = new Date(lastDate.getTime() + interval * 86400 * 1000);
              nextDue = due.toISOString().slice(0, 10);
              const delta = Math.floor((due.getTime() - todayMs) / 86400 / 1000);
              daysUntilDue = delta;
              if (delta < 0) {
                status = 'overdue';
                isOverdue = true;
              } else if (delta <= 30) status = 'due_soon';
              else status = 'up_to_date';
            }
          } else if (
            ref.schedule === 'lifetime_after_series' ||
            ref.schedule === 'series_then_booster'
          ) {
            const required = ref.primary_series_doses ?? 1;
            // Zero doses is never_recorded, not series_incomplete — mirrors
            // _coverage_for_ref in src/istota/health/immunizations.py.
            if (doseCount === 0) status = 'never_recorded';
            else status = doseCount >= required ? 'up_to_date' : 'series_incomplete';
          } else if (ref.schedule === 'travel_pre_trip') {
            if (!lastDate) status = 'never_recorded';
            else {
              const interval = ref.interval_days ?? 365;
              const due = new Date(lastDate.getTime() + interval * 86400 * 1000);
              nextDue = due.toISOString().slice(0, 10);
              const delta = Math.floor((due.getTime() - todayMs) / 86400 / 1000);
              daysUntilDue = delta;
              if (delta < 0) {
                status = 'expired';
                isOverdue = true;
              } else status = 'up_to_date';
            }
          } else {
            status = doseCount > 0 ? 'up_to_date' : 'never_recorded';
          }
          return {
            name: ref.name,
            display_name: ref.display_name,
            category: ref.category,
            status,
            last_given: lastGiven,
            dose_count: doseCount,
            next_due: nextDue,
            is_overdue: isOverdue,
            days_until_due: daysUntilDue,
          };
        });
        const canonical = new Set(IMMUNIZATION_REFS.map((r) => r.name));
        const otherMap = new Map<string, any[]>();
        for (const r of immunizations) {
          if (canonical.has(r.name)) continue;
          if (!otherMap.has(r.name)) otherMap.set(r.name, []);
          otherMap.get(r.name)!.push(r);
        }
        const other = [];
        for (const [name, group] of otherMap) {
          other.push({
            name,
            display_name: name,
            category: 'other',
            status: 'recorded',
            last_given:
              group
                .map((r) => r.date_given)
                .sort()
                .pop() || null,
            dose_count: group.length,
            next_due: null,
            is_overdue: false,
            days_until_due: null,
          });
        }
        return { coverage: out, other };
      }

      if (url === '/istota/api/health/immunizations/refs' && method === 'GET') {
        return { refs: IMMUNIZATION_REFS };
      }
      if (url === '/istota/api/health/immunizations/coverage' && method === 'GET') {
        return _computeCoverage();
      }
      if (url === '/istota/api/health/immunizations/parse' && method === 'POST') {
        if (!body || typeof body.text !== 'string') return { error: 'text is required' };
        return { rows: _parsePaste(body.text) };
      }
      if (url === '/istota/api/health/immunizations/extract' && method === 'POST') {
        // Dev fixture: mock the LLM extraction so the review UI is
        // reachable in offline development. The real backend route
        // runs OCR / vision against the uploaded file.
        return {
          document_id: storeImportDocument(body),
          mode: 'vision',
          rows: [
            {
              name: 'Influenza',
              product_name: 'Fluzone Quadrivalent',
              date_given: '2025-11-12',
              source_line: '',
              confidence: 'high',
              notes: null,
            },
            {
              name: 'COVID-19',
              product_name: 'Comirnaty',
              date_given: '2024-10-04',
              source_line: '',
              confidence: 'high',
              notes: null,
            },
            {
              name: 'Unknown',
              product_name: 'Adacel — pertussis booster',
              date_given: null,
              source_line: '',
              confidence: 'manual',
              notes: 'Date not visible in source — please add manually',
            },
          ],
          warnings: ['Mock extraction (dev mode) — the real LLM runs against the uploaded file.'],
        };
      }
      if (url === '/istota/api/health/immunizations/bulk' && method === 'POST') {
        if (!body || !Array.isArray(body.rows)) return { error: 'rows must be a list' };
        const ids: number[] = [];
        for (let i = 0; i < body.rows.length; i++) {
          const r = body.rows[i];
          if (!r.name || !r.date_given) return { error: `row ${i} missing fields` };
          const imm: Immunization = {
            id: nextImmunizationId++,
            name: String(r.name),
            product_name: r.product_name || null,
            date_given: String(r.date_given),
            manufacturer: r.manufacturer || null,
            dose_label: r.dose_label || null,
            lot_number: r.lot_number || null,
            route: r.route || null,
            site: r.site || null,
            administered_by: r.administered_by || null,
            facility: r.facility || null,
            encounter_id: r.encounter_id ?? null,
            cvx_code: r.cvx_code || null,
            notes: r.notes || null,
            source: r.source || 'import',
            created_at: new Date().toISOString(),
          };
          immunizations.push(imm);
          ids.push(imm.id);
        }
        if (body.document_id != null && !documents.some((d) => d.id === Number(body.document_id))) {
          return { error: 'document not found', __status: 400 };
        }
        linkImportDocument(body.document_id, 'immunization', ids);
        return { status: 'ok', ids, count: ids.length, document_id: body.document_id ?? null };
      }
      const immExplainerMatch = url.match(
        /^\/istota\/api\/health\/immunizations\/([^/?]+)\/explainer$/,
      );
      if (immExplainerMatch && method === 'GET') {
        const target = decodeURIComponent(immExplainerMatch[1]);
        const ref =
          IMMUNIZATION_REFS.find((r) => r.name === target) ||
          IMMUNIZATION_REFS.find((r) =>
            (r.aliases || []).some((a) => a.toLowerCase() === target.toLowerCase()),
          );
        if (!ref) return { error: 'vaccine not found' };
        const cov = _computeCoverage().coverage.find((c) => c.name === ref.name);
        const status = cov?.status || 'never_recorded';
        const disclaimer =
          'Educational information only — not medical advice or diagnosis. Discuss vaccination decisions with your clinician.';
        const data = IMMUNIZATION_EXPLAINERS[ref.name];
        if (!data) {
          return {
            name: ref.name,
            display_name: ref.display_name,
            status,
            summary: `${ref.display_name} is recommended for many adults; the current coverage indicator shows that records or doses may be incomplete. Confirm your history and the current recommended schedule with a clinician.`,
            why_it_matters: [],
            disclaimer,
            source: 'fallback',
            generated_at: null,
          };
        }
        return {
          name: ref.name,
          display_name: ref.display_name,
          status,
          summary: data.summary,
          why_it_matters: data.why_it_matters,
          disclaimer,
          source: 'static',
          generated_at: null,
        };
      }
      if (url.startsWith('/istota/api/health/immunizations') && method === 'GET') {
        const idMatch = url.match(/^\/istota\/api\/health\/immunizations\/(\d+)$/);
        if (idMatch) {
          const id = Number(idMatch[1]);
          const row = immunizations.find((x) => x.id === id);
          if (!row) return { error: 'immunization not found' };
          const enc = row.encounter_id
            ? encounters.find((e) => e.id === row.encounter_id) || null
            : null;
          return {
            immunization: row,
            encounter: enc,
            documents: documentsFor('immunization', id),
          };
        }
        const u = new URL(url, 'http://x');
        const filterName = u.searchParams.get('name');
        const since = u.searchParams.get('since');
        const until = u.searchParams.get('until');
        let rows = [...immunizations];
        if (filterName) rows = rows.filter((r) => r.name === filterName);
        if (since) rows = rows.filter((r) => r.date_given >= since);
        if (until) rows = rows.filter((r) => r.date_given <= until);
        rows.sort((a, b) => b.date_given.localeCompare(a.date_given) || b.id - a.id);
        return {
          immunizations: rows.map((r) => ({
            ...r,
            document_count: documentCount('immunization', r.id),
          })),
        };
      }
      if (url === '/istota/api/health/immunizations' && method === 'POST') {
        if (!body || !body.name || !body.date_given) {
          return { error: 'name and date_given required' };
        }
        const imm: Immunization = {
          id: nextImmunizationId++,
          name: String(body.name),
          product_name: body.product_name || null,
          date_given: String(body.date_given),
          manufacturer: body.manufacturer || null,
          dose_label: body.dose_label || null,
          lot_number: body.lot_number || null,
          route: body.route || null,
          site: body.site || null,
          administered_by: body.administered_by || null,
          facility: body.facility || null,
          encounter_id: body.encounter_id ?? null,
          cvx_code: body.cvx_code || null,
          notes: body.notes || null,
          source: body.source || 'manual',
          created_at: new Date().toISOString(),
        };
        immunizations.push(imm);
        return { status: 'ok', id: imm.id };
      }
      const immUpdMatch = url.match(/^\/istota\/api\/health\/immunizations\/(\d+)$/);
      if (immUpdMatch && method === 'PUT') {
        const id = Number(immUpdMatch[1]);
        const imm = immunizations.find((x) => x.id === id);
        if (!imm) return { error: 'immunization not found' };
        const allowed = [
          'name',
          'product_name',
          'date_given',
          'manufacturer',
          'dose_label',
          'lot_number',
          'route',
          'site',
          'administered_by',
          'facility',
          'encounter_id',
          'cvx_code',
          'notes',
        ];
        for (const k of allowed) {
          if (body && k in body) (imm as any)[k] = body[k];
        }
        return { status: 'ok' };
      }
      if (immUpdMatch && method === 'DELETE') {
        const id = Number(immUpdMatch[1]);
        const idx = immunizations.findIndex((x) => x.id === id);
        if (idx < 0) return { error: 'immunization not found' };
        immunizations.splice(idx, 1);
        return { status: 'ok' };
      }

      return undefined;
    };
  })(),
  // Portfolio (positions snapshots) mock — a small stateful closure mirroring
  // the server's read-time semantics: exclusion + classification resolve at
  // read time, so toggling an account or editing a classification reshapes
  // the summary the way the real API does.
  (() => {
    const PREFIX = '/istota/api/money/portfolio';

    interface MockPosition {
      account: string;
      symbol: string; // normalized
      description: string;
      row_type: 'position' | 'cash' | 'pending';
      quantity: number | null;
      price: number | null;
      value: number;
      cost_basis: number | null;
    }
    interface MockSnapshot {
      id: number;
      exported_at: string;
      exported_at_estimated: boolean;
      imported_at: string;
      source: string;
      source_file: string | null;
      positions: MockPosition[];
    }

    const accounts = [
      {
        id: 1,
        account_name: 'Taxable Brokerage',
        account_number: 'X111',
        group: 'Carol',
        account_type: 'taxable',
        excluded: false,
        first_seen_at: '2026-01-05T09:00:00Z',
        last_seen_at: '2026-07-01T09:00:00Z',
      },
      {
        id: 2,
        account_name: 'Roth IRA A',
        account_number: 'X222',
        group: 'Carol',
        account_type: 'retirement',
        excluded: false,
        first_seen_at: '2026-01-05T09:00:00Z',
        last_seen_at: '2026-07-01T09:00:00Z',
      },
      {
        id: 3,
        account_name: 'Active Trading (IBKR)',
        account_number: 'X333',
        group: 'Carol',
        account_type: 'trading',
        excluded: false,
        first_seen_at: '2026-02-10T09:00:00Z',
        last_seen_at: '2026-07-01T09:00:00Z',
      },
      {
        id: 4,
        account_name: 'Joint Brokerage',
        account_number: 'X444',
        group: 'Dana',
        account_type: 'taxable',
        excluded: false,
        first_seen_at: '2026-01-05T09:00:00Z',
        last_seen_at: '2026-07-01T09:00:00Z',
      },
      {
        id: 5,
        account_name: 'SK Tax',
        account_number: 'X555',
        group: 'Carol',
        account_type: 'cash',
        excluded: true,
        first_seen_at: '2026-01-05T09:00:00Z',
        last_seen_at: '2026-07-01T09:00:00Z',
      },
    ];

    const classifications = [
      {
        symbol: 'VTI',
        asset_class: 'Stocks',
        sub_class: 'Total Market',
        geography: 'US',
        source: 'seed',
        updated_at: '2026-01-05T09:00:00Z',
      },
      {
        symbol: 'VXUS',
        asset_class: 'Stocks',
        sub_class: 'Total Market',
        geography: 'International',
        source: 'seed',
        updated_at: '2026-01-05T09:00:00Z',
      },
      {
        symbol: 'SGOV',
        asset_class: 'Fixed Income',
        sub_class: 'Short-Term',
        geography: 'US',
        source: 'seed',
        updated_at: '2026-01-05T09:00:00Z',
      },
      {
        symbol: 'PDBC',
        asset_class: 'Commodities',
        sub_class: 'Broad Basket',
        geography: 'Global',
        source: 'seed',
        updated_at: '2026-01-05T09:00:00Z',
      },
      {
        symbol: 'SPAXX',
        asset_class: 'Cash & Equivalents',
        sub_class: 'Money Market',
        geography: 'US',
        source: 'seed',
        updated_at: '2026-01-05T09:00:00Z',
      },
      {
        symbol: 'FBTC',
        asset_class: 'Alternative',
        sub_class: 'Cryptocurrency',
        geography: 'US',
        source: 'seed',
        updated_at: '2026-01-05T09:00:00Z',
      },
    ];

    function pos(
      account: string,
      symbol: string,
      quantity: number | null,
      price: number | null,
      value: number,
      costBasis: number | null,
      rowType: 'position' | 'cash' | 'pending' = 'position',
    ): MockPosition {
      return {
        account,
        symbol,
        description: symbol ? `${symbol} FUND` : 'Pending Activity',
        row_type: rowType,
        quantity,
        price,
        value,
        cost_basis: costBasis,
      };
    }

    let nextSnapshotId = 4;
    const snapshots: MockSnapshot[] = [
      {
        id: 1,
        exported_at: '2026-05-02T10:15:00',
        exported_at_estimated: false,
        imported_at: '2026-05-02T17:20:00Z',
        source: 'fidelity-positions-csv',
        source_file: 'Portfolio_Positions_May-02-2026.csv',
        positions: [
          pos('Taxable Brokerage', 'VTI', 500, 350.1, 175050, 140000),
          pos('Taxable Brokerage', 'VXUS', 600, 80.2, 48120, 40000),
          pos('Taxable Brokerage', 'SPAXX', null, null, 1200, null, 'cash'),
          pos('Roth IRA A', 'VTI', 150, 350.1, 52515, 38000),
          pos('Active Trading (IBKR)', 'GOOG', 40, 340.0, 13600, 14600),
          pos('Joint Brokerage', 'FZDXX', 230000, 1.0, 230000, null, 'cash'),
          pos('SK Tax', 'CORE', null, null, 1800, null, 'cash'),
        ],
      },
      {
        id: 2,
        exported_at: '2026-06-15T09:00:00',
        exported_at_estimated: false,
        imported_at: '2026-06-15T16:00:00Z',
        source: 'fidelity-positions-csv',
        source_file: 'Portfolio_Positions_Jun-15-2026.csv',
        positions: [
          pos('Taxable Brokerage', 'VTI', 510, 360.5, 183855, 143500),
          pos('Taxable Brokerage', 'VXUS', 610, 82.9, 50569, 40800),
          pos('Taxable Brokerage', 'SPAXX', null, null, 900, null, 'cash'),
          pos('Roth IRA A', 'VTI', 150, 360.5, 54075, 38000),
          pos('Active Trading (IBKR)', 'GOOG', 40, 352.0, 14080, 14600),
          pos('Active Trading (IBKR)', 'FBTC', 90, 60.0, 5400, 7300),
          pos('Joint Brokerage', 'FZDXX', 232000, 1.0, 232000, null, 'cash'),
          pos('SK Tax', 'CORE', null, null, 1850, null, 'cash'),
        ],
      },
      {
        id: 3,
        exported_at: '2026-08-01T14:04:00',
        exported_at_estimated: false,
        imported_at: '2026-08-01T18:05:00Z',
        source: 'fidelity-positions-csv',
        source_file: 'Portfolio_Positions_Aug-01-2026.csv',
        positions: [
          pos('Taxable Brokerage', 'VTI', 518, 368.2, 190728, 145800),
          pos('Taxable Brokerage', 'VXUS', 615, 84.6, 52029, 41600),
          pos('Taxable Brokerage', 'SPAXX', null, null, 1.2, null, 'cash'),
          pos('Roth IRA A', 'VTI', 151, 368.2, 55598, 39800),
          pos('Active Trading (IBKR)', 'GOOG', 40, 357.9, 14316, 14600),
          pos('Active Trading (IBKR)', 'FBTC', 97, 54.7, 5306, 7328),
          pos('Active Trading (IBKR)', 'ZZZT', 100, 10.0, 1000, 1100),
          pos('Active Trading (IBKR)', '', null, null, -120.5, null, 'pending'),
          pos('Joint Brokerage', 'FZDXX', 233588, 1.0, 233588, null, 'cash'),
          pos('SK Tax', 'CORE', null, null, 1780, null, 'cash'),
        ],
      },
    ];

    function accountFor(name: string) {
      return accounts.find((a) => a.account_name === name);
    }

    function classify(p: MockPosition): {
      asset_class: string;
      sub_class: string;
      geography: string;
    } {
      if (p.row_type !== 'position')
        return { asset_class: 'Cash & Equivalents', sub_class: 'Cash', geography: 'US' };
      const explicit = classifications.find((c) => c.symbol === p.symbol);
      if (explicit) return explicit;
      if (p.symbol === 'FZDXX' || p.symbol === 'CORE')
        return { asset_class: 'Cash & Equivalents', sub_class: 'Money Market', geography: 'US' };
      return { asset_class: 'Unclassified', sub_class: 'Unclassified', geography: 'Unclassified' };
    }

    // Symbols the server's two tiers both give up on: no ticker metadata and
    // a description carrying no signal. Keeping one means the "could not
    // classify" branch is reachable under VITE_MOCK_API=1 — it was not, and
    // neither was the import-time auto-classified notice, because the import
    // handler returned a hardcoded unclassified list and never ran the fill-in.
    const MOCK_UNRESOLVABLE = new Set(['ZZZT']);

    function autoClassifyPositions(scope?: MockSnapshot): {
      classified: {
        symbol: string;
        asset_class: string;
        sub_class: string;
        geography: string;
        method: string;
      }[];
      unresolved: string[];
    } {
      const classified: {
        symbol: string;
        asset_class: string;
        sub_class: string;
        geography: string;
        method: string;
      }[] = [];
      const unresolved: string[] = [];
      const seen = new Set<string>();
      for (const snap of scope ? [scope] : snapshots) {
        for (const p of snap.positions) {
          if (p.row_type !== 'position' || !p.symbol) continue;
          if (seen.has(p.symbol)) continue;
          if (classify(p).asset_class !== 'Unclassified') continue;
          seen.add(p.symbol);
          if (MOCK_UNRESOLVABLE.has(p.symbol)) {
            unresolved.push(p.symbol);
            continue;
          }
          const record = {
            symbol: p.symbol,
            asset_class: 'Stocks',
            sub_class: 'Individual Stock',
            geography: 'US',
            source: 'auto',
            updated_at: new Date().toISOString(),
          };
          classifications.push(record);
          classified.push({
            symbol: p.symbol,
            asset_class: record.asset_class,
            sub_class: record.sub_class,
            geography: record.geography,
            method: 'lookup',
          });
        }
      }
      return { classified, unresolved: unresolved.sort() };
    }

    function visiblePositions(snap: MockSnapshot, group?: string | null) {
      return snap.positions.filter((p) => {
        const acct = accountFor(p.account);
        if (!acct || acct.excluded) return false;
        if (group && acct.group !== group) return false;
        return true;
      });
    }

    function groupSums(rows: MockPosition[], labelFn: (p: MockPosition) => string) {
      const sums = new Map<string, number>();
      for (const p of rows) sums.set(labelFn(p), (sums.get(labelFn(p)) ?? 0) + p.value);
      const total = [...sums.values()].reduce((a, b) => a + b, 0);
      return [...sums.entries()]
        .sort((a, b) => b[1] - a[1])
        .map(([key, value]) => ({
          key,
          value: Math.round(value * 100) / 100,
          pct: total ? Math.round((value / total) * 10000) / 10000 : 0,
        }));
    }

    function summaryOf(snap: MockSnapshot, group?: string | null) {
      const rows = visiblePositions(snap, group);
      const total = rows.reduce((a, p) => a + p.value, 0);
      const holdings = new Map<string, any>();
      for (const p of rows) {
        const key = p.row_type === 'position' ? p.symbol : 'CASH';
        const h = holdings.get(key) ?? {
          symbol: key,
          description: key === 'CASH' ? 'Cash & equivalents' : p.description,
          quantity: null,
          value: 0,
          cost_basis: null,
          gain: null,
          gain_pct: null,
          ...(key === 'CASH'
            ? { asset_class: 'Cash & Equivalents', sub_class: 'Cash', geography: 'US' }
            : classify(p)),
          accounts: new Set<string>(),
        };
        h.value += p.value;
        if (p.quantity != null && key !== 'CASH') h.quantity = (h.quantity ?? 0) + p.quantity;
        if (p.cost_basis != null) h.cost_basis = (h.cost_basis ?? 0) + p.cost_basis;
        h.accounts.add(p.account);
        holdings.set(key, h);
      }
      const holdingsList = [...holdings.values()]
        .map((h) => ({
          ...h,
          accounts: h.accounts.size,
          gain: h.cost_basis ? Math.round((h.value - h.cost_basis) * 100) / 100 : null,
          gain_pct: h.cost_basis
            ? Math.round(((h.value - h.cost_basis) / h.cost_basis) * 10000) / 10000
            : null,
        }))
        .sort((a, b) => b.value - a.value);
      const byAccount = groupSums(rows, (p) => p.account).map((g) => {
        const acct = accountFor(g.key)!;
        return { ...g, account_id: acct.id, group: acct.group, account_type: acct.account_type };
      });
      return {
        snapshot_id: snap.id,
        exported_at: snap.exported_at,
        exported_at_estimated: snap.exported_at_estimated,
        total_value: Math.round(total * 100) / 100,
        position_count: rows.length,
        by_asset_class: groupSums(rows, (p) => classify(p).asset_class),
        by_account: byAccount,
        by_account_type: groupSums(
          rows,
          (p) => accountFor(p.account)?.account_type || 'unspecified',
        ),
        by_group: groupSums(rows, (p) => accountFor(p.account)?.group || 'Ungrouped'),
        by_geography: groupSums(rows, (p) => classify(p).geography),
        holdings: holdingsList,
      };
    }

    function snapshotRow(snap: MockSnapshot) {
      const rows = visiblePositions(snap);
      return {
        id: snap.id,
        exported_at: snap.exported_at,
        exported_at_estimated: snap.exported_at_estimated,
        imported_at: snap.imported_at,
        source: snap.source,
        source_file: snap.source_file,
        position_count: snap.positions.length,
        total_value: Math.round(rows.reduce((a, p) => a + p.value, 0) * 100) / 100,
      };
    }

    return ({ url, method, body }: MockReq) => {
      if (!url.startsWith(PREFIX)) return undefined;
      const [path, query] = url.slice(PREFIX.length).split('?');
      const params = new URLSearchParams(query ?? '');

      if (path === '/import' && method === 'POST') {
        // Mirror the server's explicit-source validation (importer registry).
        const source = params.get('source');
        if (source && source !== 'fidelity-positions-csv' && source !== 'fina-history-csv') {
          return { __status: 400, status: 'error', error: `Unknown import source: ${source}` };
        }
        if (params.get('dry_run')) {
          return {
            status: 'ok',
            dry_run: true,
            snapshots: [
              {
                exported_at: new Date().toISOString().slice(0, 19),
                exported_at_estimated: false,
                source: 'fidelity-positions-csv',
                position_count: 46,
                total_value: 812345.67,
                warnings: [],
              },
            ],
          };
        }
        const replace = params.get('replace');
        if (replace) {
          const idx = snapshots.findIndex((s) => s.id === Number(replace));
          if (idx >= 0) snapshots.splice(idx, 1);
        }
        const template = snapshots[snapshots.length - 1] ?? snapshots[0];
        const snap: MockSnapshot = {
          id: nextSnapshotId++,
          exported_at: new Date().toISOString().slice(0, 19),
          exported_at_estimated: false,
          imported_at: new Date().toISOString(),
          source: source ?? 'fidelity-positions-csv',
          source_file: 'upload.csv',
          positions: template ? template.positions.map((p) => ({ ...p })) : [],
        };
        snapshots.push(snap);
        // Same order the server takes: the snapshot commits, then one
        // classification pass fills in what it can and reports the rest.
        const auto = autoClassifyPositions(snap);
        return {
          status: 'ok',
          snapshot_id: snap.id,
          exported_at: snap.exported_at,
          exported_at_estimated: false,
          position_count: snap.positions.length,
          total_value: snap.positions.reduce((a, p) => a + p.value, 0),
          new_accounts: [],
          auto_classified: auto.classified,
          unclassified_symbols: auto.unresolved,
          warnings: [],
          source_file: snap.source_file,
        };
      }

      if (path === '/snapshots' && method === 'GET') {
        return {
          status: 'ok',
          snapshots: [...snapshots]
            .sort((a, b) => (a.exported_at < b.exported_at ? 1 : -1))
            .map(snapshotRow),
        };
      }

      const snapMatch = path.match(/^\/snapshots\/(\d+)$/);
      if (snapMatch) {
        const snap = snapshots.find((s) => s.id === Number(snapMatch[1]));
        if (method === 'GET') {
          if (!snap) return { __status: 404, status: 'error', error: 'no such snapshot' };
          return { status: 'ok', summary: summaryOf(snap, params.get('group')) };
        }
        if (method === 'DELETE') {
          if (!snap) return { __status: 404, status: 'error', error: 'no such snapshot' };
          snapshots.splice(snapshots.indexOf(snap), 1);
          return { status: 'ok', deleted: snap.id };
        }
      }

      if (path === '/summary' && method === 'GET') {
        if (snapshots.length === 0) return { status: 'ok', summary: null };
        const latest = [...snapshots].sort((a, b) => (a.exported_at < b.exported_at ? 1 : -1))[0];
        return { status: 'ok', summary: summaryOf(latest, params.get('group')) };
      }

      if (path === '/history' && method === 'GET') {
        const groupBy = params.get('group_by') ?? 'total';
        const group = params.get('group');
        const series = [...snapshots]
          .sort((a, b) => (a.exported_at > b.exported_at ? 1 : -1))
          .map((snap) => {
            const rows = visiblePositions(snap, group);
            const point: any = {
              snapshot_id: snap.id,
              exported_at: snap.exported_at,
              exported_at_estimated: snap.exported_at_estimated,
              total: Math.round(rows.reduce((a, p) => a + p.value, 0) * 100) / 100,
            };
            if (groupBy !== 'total') {
              const labelFn =
                groupBy === 'group'
                  ? (p: MockPosition) => accountFor(p.account)?.group || 'Ungrouped'
                  : groupBy === 'account_type'
                    ? (p: MockPosition) => accountFor(p.account)?.account_type || 'unspecified'
                    : (p: MockPosition) => classify(p).asset_class;
              point.groups = Object.fromEntries(
                groupSums(rows, labelFn).map((g) => [g.key, g.value]),
              );
            }
            return point;
          });
        return { status: 'ok', group_by: groupBy, series };
      }

      if (path === '/diff' && method === 'GET') {
        const older = snapshots.find((s) => s.id === Number(params.get('older')));
        const newer = snapshots.find((s) => s.id === Number(params.get('newer')));
        if (!older || !newer)
          return { __status: 404, status: 'error', error: 'snapshot not found' };
        const key = (p: MockPosition) =>
          `${p.account}|${p.row_type === 'position' ? p.symbol : 'CASH'}`;
        const agg = (snap: MockSnapshot) => {
          const m = new Map<
            string,
            { symbol: string; account_name: string; quantity: number; value: number }
          >();
          for (const p of visiblePositions(snap)) {
            const k = key(p);
            const e = m.get(k) ?? {
              symbol: p.row_type === 'position' ? p.symbol : 'CASH',
              account_name: p.account,
              quantity: 0,
              value: 0,
            };
            e.quantity += p.quantity ?? 0;
            e.value += p.value;
            m.set(k, e);
          }
          return m;
        };
        const a = agg(older);
        const b = agg(newer);
        const opened = [...b.entries()].filter(([k]) => !a.has(k)).map(([, e]) => e);
        const closed = [...a.entries()].filter(([k]) => !b.has(k)).map(([, e]) => e);
        const changed = [...a.entries()]
          .filter(([k]) => b.has(k))
          .map(([k, e]) => {
            const n = b.get(k)!;
            return {
              symbol: e.symbol,
              account_name: e.account_name,
              quantity_from: e.quantity,
              quantity_to: n.quantity,
              value_from: e.value,
              value_to: n.value,
            };
          })
          .filter(
            (c) =>
              Math.abs(c.quantity_from - c.quantity_to) > 1e-6 ||
              Math.abs(c.value_from - c.value_to) > 0.01,
          );
        return {
          status: 'ok',
          diff: { older_id: older.id, newer_id: newer.id, opened, closed, changed },
        };
      }

      const symMatch = path.match(/^\/symbols\/([^/]+)\/history$/);
      if (symMatch && method === 'GET') {
        const symbol = decodeURIComponent(symMatch[1]).toUpperCase();
        const points = [...snapshots]
          .sort((a, b) => (a.exported_at > b.exported_at ? 1 : -1))
          .map((snap) => {
            const rows = visiblePositions(snap).filter((p) => p.symbol === symbol);
            if (rows.length === 0) return null;
            return {
              snapshot_id: snap.id,
              exported_at: snap.exported_at,
              quantity: rows.reduce((a, p) => a + (p.quantity ?? 0), 0),
              price: rows[0].price,
              value: Math.round(rows.reduce((a, p) => a + p.value, 0) * 100) / 100,
            };
          })
          .filter((p) => p !== null);
        return { status: 'ok', history: { symbol, points } };
      }

      if (path === '/accounts' && method === 'GET') {
        return { status: 'ok', accounts };
      }

      const acctMatch = path.match(/^\/accounts\/(\d+)$/);
      if (acctMatch && method === 'PATCH') {
        const acct = accounts.find((a) => a.id === Number(acctMatch[1]));
        if (!acct) return { __status: 404, status: 'error', error: 'no such account' };
        const allowed = ['group', 'account_type', 'excluded'];
        const unknown = Object.keys(body ?? {}).filter((k) => !allowed.includes(k));
        if (unknown.length)
          return { __status: 400, status: 'error', error: `unknown fields: ${unknown.join(', ')}` };
        Object.assign(acct, body);
        return { status: 'ok', account: acct };
      }

      if (path === '/classifications' && method === 'GET') {
        return { status: 'ok', classifications };
      }

      if (path === '/classifications/auto' && method === 'POST') {
        // Mirrors the server: fill in any position symbol still resolving to
        // Unclassified with a plausible lookup result, marked source 'auto',
        // and report the ones neither tier could place.
        const auto = autoClassifyPositions();
        // Reachability, not fidelity: hardcoding `true` left the card's
        // "ticker lookup unavailable — heuristics only" wording unreachable
        // under VITE_MOCK_API=1, which is the failure the mock-mirrors-
        // invariants rule exists to prevent. Once the tier has nothing left
        // to place, a second click shows the degraded branch.
        return {
          status: 'ok',
          ...auto,
          lookups_available: auto.classified.length > 0 || auto.unresolved.length === 0,
        };
      }

      const clsMatch = path.match(/^\/classifications\/([^/]+)$/);
      if (clsMatch) {
        const symbol = decodeURIComponent(clsMatch[1]).replace(/\*+$/, '').toUpperCase();
        if (method === 'PUT') {
          if (!body?.asset_class?.trim())
            return { __status: 400, status: 'error', error: 'asset_class is required' };
          const existing = classifications.find((c) => c.symbol === symbol);
          const record = {
            symbol,
            asset_class: body.asset_class,
            sub_class: body.sub_class ?? '',
            geography: body.geography ?? '',
            source: 'user',
            updated_at: new Date().toISOString(),
          };
          if (existing) Object.assign(existing, record);
          else classifications.push(record);
          return { status: 'ok', classification: record };
        }
        if (method === 'DELETE') {
          const idx = classifications.findIndex((c) => c.symbol === symbol);
          if (idx < 0) return { __status: 404, status: 'error', error: 'no such classification' };
          classifications.splice(idx, 1);
          return { status: 'ok' };
        }
      }

      return undefined;
    };
  })(),

  // Money tax estimate (/money/taxes). The GET 404'd against the mock, so the
  // Taxes tab was uninspectable in dev: it rendered its error state and nothing
  // below it could be worked on. The POST was worse than a 404 — the mock's
  // non-GET fallback answers `{}`, so a recalculation would have replaced the
  // response with an object whose every field is undefined and crashed the
  // page's `toLocaleString` formatting.
  //
  // The arithmetic is a port of `istota/money/core/tax.py` rather than a frozen
  // payload, because every number this page shows is derived from the six
  // inputs beside them — a fixture would let the inputs move while nothing
  // below them changed, which is the one thing the page is for. Ported:
  // `apply_brackets`, `compute_se_tax`, `compute_federal_tax`, `compute_state_tax`,
  // `annualization_months`, `_project_full_year`, `payment_quarter_from_date`
  // and `estimate_quarterly_tax`, including both the annualized and safe-harbor
  // branches and the per-state installment schedule (federal's flat 25%,
  // California's 30/40/0/30).
  //
  // The rate tables are NOT copied. They are read from the same
  // `data/tax_rates.json` the Python module loads, the way the health mocks
  // already read `biomarker_refs.json` — so a bracket cannot be current in one
  // language and stale in the other. Only the arithmetic is a port.
  (() => {
    const PATH = '/istota/api/money/tax/estimate';
    const CONFIG_PATH = '/istota/api/money/config/tax';

    // Set to true to exercise the "no tax configuration found" empty state,
    // which the real API signals with a 404 (`_load_tax_config` returning None).
    const NO_TAX_CONFIG = false;

    type Bracket = [threshold: number, rate: number];

    // Read from the bundled data rather than re-keyed here. The taxes page is
    // the one surface whose numbers a user sends to a government, and a mock
    // holding its own copy is how a bracket comes to be current in Python and
    // stale in TypeScript.
    const TAX_RATES: any = (() => {
      try {
        const path = resolve(__mockDir, '../src/istota/money/data/tax_rates.json');
        return JSON.parse(readFileSync(path, 'utf-8'));
      } catch {
        return { jurisdictions: [], federal: {}, states: {} };
      }
    })();

    /** Newest bundled year at or before `year`, else the oldest — mirrors `_resolve_year`. */
    function resolveYear(block: Record<string, any>, year: number): string | null {
      const available = Object.keys(block)
        .map(Number)
        .sort((a, b) => a - b);
      if (!available.length) return null;
      const atOrBefore = available.filter((y) => y <= year);
      return String(atOrBefore.length ? Math.max(...atOrBefore) : Math.min(...available));
    }

    function federalYear(year: number): any {
      const key = resolveYear(TAX_RATES.federal ?? {}, year);
      return key === null ? null : { ...TAX_RATES.federal[key], __year: Number(key) };
    }

    function stateYear(code: string, year: number): any {
      const st = (TAX_RATES.states ?? {})[code];
      if (!st) return null;
      const key = resolveYear(st.years ?? {}, year);
      return key === null ? null : { ...st.years[key], __year: Number(key) };
    }

    function jurisdictionOf(code: string): any {
      return (TAX_RATES.jurisdictions ?? []).find((j: any) => j.code === code) ?? null;
    }

    /**
     * True when `value` is a well-formed ISO date on or after `floor`.
     *
     * A plain string compare reads a malformed date (`2026/01/01` — exactly the
     * typo a hand-edit produces) as *fresh*, where Python's `date.fromisoformat`
     * raises and the staleness check treats it as stale. Getting the safety
     * signal itself backwards in the mock is worth the extra function.
     */
    function isoDateBefore(value: string, floor: string): boolean {
      if (!/^\d{4}-\d{2}-\d{2}$/.test(value ?? '')) return false;
      return value >= floor;
    }

    function provenance(block: any, requestedYear: number, overridden: boolean) {
      if (!block) {
        return {
          year: null,
          requested_year: null,
          is_fallback: false,
          is_stale: false,
          overridden,
          source: '',
          source_url: '',
          verified_on: '',
        };
      }
      const verified = block.verified_on ?? '';
      return {
        year: block.__year,
        requested_year: requestedYear,
        is_fallback: block.__year !== requestedYear,
        is_stale: !isoDateBefore(verified, `${requestedYear}-01-01`),
        overridden,
        source: block.source ?? '',
        source_url: block.source_url ?? '',
        verified_on: verified,
      };
    }

    const DEFAULT_INSTALLMENT: number[] = [0.25, 0.5, 0.75, 1.0];
    function installmentSchedule(code: string): number[] {
      return (TAX_RATES.states ?? {})[code]?.installment_schedule ?? DEFAULT_INSTALLMENT;
    }

    // Statutory rather than indexed, so these stay here — the Python module
    // keeps them as constants for the same reason.
    const SAFE_HARBOR_AGI_THRESHOLD = 150_000;
    const FED_CUMULATIVE_PCT: Record<number, number> = { 1: 0.25, 2: 0.5, 3: 0.75, 4: 1.0 };
    const ANNUALIZATION_PERIOD_END_MONTH: Record<number, number> = { 1: 3, 2: 5, 3: 8, 4: 12 };

    function applyBrackets(taxableIncome: number, brackets: Bracket[]): number {
      if (taxableIncome <= 0) return 0;
      let tax = 0;
      for (let i = 0; i < brackets.length; i++) {
        const [threshold] = brackets[i];
        const rate = brackets[i][1];
        const bracketIncome =
          i + 1 < brackets.length
            ? Math.min(taxableIncome, brackets[i + 1][0]) - threshold
            : taxableIncome - threshold;
        if (bracketIncome <= 0) break;
        tax += bracketIncome * rate;
      }
      return tax;
    }

    function computeSeTax(seNetIncome: number, payroll: any): { seTax: number; halfSe: number } {
      if (seNetIncome <= 0) return { seTax: 0, halfSe: 0 };
      const taxableSe = seNetIncome * payroll.se_taxable_fraction;
      const ssTax = Math.min(taxableSe, payroll.ss_wage_base) * payroll.ss_rate;
      const medicareTax = taxableSe * payroll.medicare_rate;
      const seTax = ssTax + medicareTax;
      return { seTax, halfSe: seTax / 2 };
    }

    /** Payment-quarter period, capped at the months that have actually elapsed. */
    function annualizationMonths(quarter: number, taxYear: number, today: Date): number {
      const periodEnd = ANNUALIZATION_PERIOD_END_MONTH[quarter] ?? 12;
      let elapsed: number;
      if (today.getFullYear() > taxYear) elapsed = 12;
      else if (today.getFullYear() < taxYear) elapsed = 0;
      else elapsed = today.getMonth(); // the current month is still in progress
      return Math.max(1, Math.min(periodEnd, elapsed));
    }

    /** Scale a YTD figure to `targetMonths`, never below what's already earned. */
    function projectFullYear(ytd: number, monthsElapsed: number, targetMonths: number): number {
      if (ytd <= 0 || monthsElapsed <= 0) return Math.max(ytd, 0);
      const projected = (ytd / monthsElapsed) * Math.min(targetMonths, 12);
      return Math.max(projected, ytd);
    }

    function paymentQuarterFromDate(today: Date, taxYear: number): number {
      if (today.getFullYear() > taxYear) return 4;
      if (today.getFullYear() < taxYear) return 1;
      const month = today.getMonth() + 1;
      const day = today.getDate();
      if (month < 4 || (month === 4 && day <= 15)) return 1;
      if (month < 6 || (month === 6 && day <= 15)) return 2;
      if (month < 9 || (month === 9 && day <= 15)) return 3;
      return 4;
    }

    const round2 = (n: number) => Math.round(n * 100) / 100;

    const today = new Date();
    const TAX_YEAR = today.getFullYear();
    const CURRENT_QUARTER = paymentQuarterFromDate(today, TAX_YEAR);
    const MONTHS = annualizationMonths(CURRENT_QUARTER, TAX_YEAR, today);

    // Stands in for `query_se_income`, which sums the ledger's SE income
    // accounts over the first N months of the year — so it is per-ledger, and
    // the section's ledger picker moves these numbers the way it does in
    // production. An unknown ledger name resolves to nothing, matching
    // `_resolve_user_ledger` returning None.
    const SE_MONTHLY_BY_LEDGER: Record<string, number> = {
      main: 200_000 / 12,
      business: 48_000 / 12,
    };
    const DEFAULT_LEDGER = 'main';

    // Stands in for TAX.md / the money DB's tax config. The W-2 figures are
    // year-to-date amounts the user typed, so they are seeded from a monthly
    // rate over the elapsed period rather than being fixed — otherwise the
    // starting numbers would only look sensible in whichever quarter they were
    // written in. Chosen so the three conditional rows the page can render are
    // all reachable at the defaults: the Social Security wage-base note, the
    // additional Medicare row, and the QBI deduction.
    const config = {
      tax_year: TAX_YEAR,
      filing_status: 'mfj',
      // Flip to '' to exercise the no-state-tax rendering, or to a state we
      // ship no brackets for (e.g. 'NY') to exercise the missing-brackets one.
      state: 'CA',
      enable_qbi_deduction: true,
      prior_year_federal_tax: 62_000,
      prior_year_state_tax: 18_000,
      w2_income: 10_000 * MONTHS,
      w2_federal_withholding: 1_800 * MONTHS,
      w2_state_withholding: 600 * MONTHS,
      federal_estimated_paid: 24_000,
      state_estimated_paid: 7_000,
    };

    // Mirrors `save_tax_inputs` / `load_tax_inputs`: the POST persists the
    // user's inputs, so a reload (or a ledger switch, which re-issues the GET)
    // comes back with what was last entered rather than the config defaults.
    const savedInputs: Record<string, number | string | undefined> = {};

    function estimate(opts: {
      seIncomeYtd: number;
      w2Income: number;
      w2FederalWithholding: number;
      w2StateWithholding: number;
      federalEstimatedPaid: number;
      stateEstimatedPaid: number;
      method: string;
      w2Months: number;
      quarter: number;
      months: number;
    }) {
      const filingStatus = config.filing_status;
      const stateCode = (config.state ?? '').toUpperCase();
      const months = Math.max(1, opts.months);

      const fedBlock = federalYear(config.tax_year);
      const fedStatus = fedBlock?.filing_status?.[filingStatus] ?? {};
      const payroll = fedBlock?.payroll ?? {
        ss_wage_base: 0,
        ss_rate: 0.124,
        medicare_rate: 0.029,
        se_taxable_fraction: 0.9235,
        additional_medicare_rate: 0.009,
      };

      const seAnnualized = Math.max(opts.seIncomeYtd, opts.seIncomeYtd * (12 / months));
      const w2Annualized = projectFullYear(opts.w2Income, months, opts.w2Months);
      const fedWithholdingAnnual = projectFullYear(
        opts.w2FederalWithholding,
        months,
        opts.w2Months,
      );
      const stateWithholdingAnnual = projectFullYear(
        opts.w2StateWithholding,
        months,
        opts.w2Months,
      );

      const { seTax, halfSe } = computeSeTax(seAnnualized, payroll);
      const federalAgi = seAnnualized + w2Annualized - halfSe;

      const seTaxableForMedicare = seAnnualized * payroll.se_taxable_fraction;
      // `||` not `??`, matching Python's `or` (tax.py).
      const amtThreshold = fedStatus.additional_medicare_threshold || 200_000;
      const additionalMedicare =
        Math.max(0, w2Annualized + seTaxableForMedicare - amtThreshold) *
        payroll.additional_medicare_rate;

      const fedStdDed = fedStatus.standard_deduction ?? 0;

      let qbiDeduction = 0;
      if (config.enable_qbi_deduction && seAnnualized > 0) {
        qbiDeduction = seAnnualized * 0.2;
        // `||` not `??`, matching Python's `or`: a 0 placeholder in the data
        // must fall through to the default, not be taken as a real range.
        const threshold = fedStatus.qbi_threshold || 0;
        const phaseout = fedStatus.qbi_phaseout_range || 50_000;
        // Section 199A measures against taxable income, not AGI — see tax.py.
        const taxableBeforeQbi = Math.max(0, federalAgi - fedStdDed);
        if (threshold > 0 && taxableBeforeQbi > threshold) {
          if (taxableBeforeQbi >= threshold + phaseout) qbiDeduction = 0;
          else qbiDeduction *= 1 - (taxableBeforeQbi - threshold) / phaseout;
        }
        qbiDeduction = Math.min(qbiDeduction, taxableBeforeQbi * 0.2);
      }

      const fedTaxable = Math.max(0, federalAgi - fedStdDed - qbiDeduction);
      const fedTax = applyBrackets(fedTaxable, fedStatus.brackets ?? []);
      const federalTotalLiability = fedTax + seTax + additionalMedicare;

      // State. California conforms to federal AGI, which already carries the
      // above-the-line half-SE deduction; it does not allow QBI. `starts_from`
      // in the data file is what says so.
      const jurisdiction = stateCode ? jurisdictionOf(stateCode) : null;
      const stateBlock = stateCode ? stateYear(stateCode, config.tax_year) : null;
      const stateStatus = stateBlock?.filing_status?.[filingStatus] ?? {};
      const stateBrackets: Bracket[] = stateStatus.brackets ?? [];

      let stateReason = '';
      if (!stateCode) stateReason = 'no_state';
      else if (!jurisdiction) stateReason = 'unknown_state';
      else if (!jurisdiction.taxes_income) stateReason = 'no_income_tax';
      else if (!stateBrackets.length) stateReason = 'no_brackets';
      const stateAvailable = stateReason === '';

      const startsFrom = (TAX_RATES.states ?? {})[stateCode]?.starts_from ?? 'federal_agi';
      const startingIncome =
        startsFrom === 'federal_taxable_income'
          ? Math.max(0, federalAgi - fedStdDed - qbiDeduction)
          : startsFrom === 'gross_compensation'
            ? seAnnualized + w2Annualized
            : federalAgi;

      const stateStdDed = stateAvailable ? (stateStatus.standard_deduction ?? 0) : 0;
      const stateExemption = stateAvailable ? (stateStatus.personal_exemption ?? 0) : 0;
      const stateTaxable = stateAvailable
        ? Math.max(0, startingIncome - stateStdDed - stateExemption)
        : 0;
      const stateTax = stateAvailable ? applyBrackets(stateTaxable, stateBrackets) : 0;
      const stateAgi = stateAvailable ? startingIncome : 0;
      const stateCumulative = installmentSchedule(stateCode);

      let federalNetDue: number;
      let stateNetDue: number;
      let fedQuarterly: number;
      let stateQuarterly: number;

      if (opts.method === 'safe_harbor') {
        const mult = federalAgi > SAFE_HARBOR_AGI_THRESHOLD ? 1.1 : 1.0;
        federalNetDue = Math.max(0, config.prior_year_federal_tax * mult - fedWithholdingAnnual);
        stateNetDue = Math.max(0, config.prior_year_state_tax * mult - stateWithholdingAnnual);
        fedQuarterly = round2(
          Math.max(0, federalNetDue * FED_CUMULATIVE_PCT[opts.quarter] - opts.federalEstimatedPaid),
        );
        stateQuarterly = round2(
          Math.max(0, stateNetDue * stateCumulative[opts.quarter - 1] - opts.stateEstimatedPaid),
        );
      } else {
        federalNetDue = Math.max(
          0,
          federalTotalLiability - fedWithholdingAnnual - opts.federalEstimatedPaid,
        );
        stateNetDue = Math.max(0, stateTax - stateWithholdingAnnual - opts.stateEstimatedPaid);
        const fedTotalRequired = Math.max(0, federalTotalLiability - fedWithholdingAnnual);
        fedQuarterly = round2(
          Math.max(
            0,
            fedTotalRequired * FED_CUMULATIVE_PCT[opts.quarter] - opts.federalEstimatedPaid,
          ),
        );
        const stateTotalRequired = Math.max(0, stateTax - stateWithholdingAnnual);
        stateQuarterly = round2(
          Math.max(
            0,
            stateTotalRequired * stateCumulative[opts.quarter - 1] - opts.stateEstimatedPaid,
          ),
        );
      }

      return {
        status: 'ok',
        tax_year: config.tax_year,
        quarter: opts.quarter,
        method: opts.method,
        filing_status: filingStatus,
        w2_months: opts.w2Months,
        annualization_months: months,
        se_income_ytd: opts.seIncomeYtd,
        se_income_annualized: seAnnualized,
        w2_income: opts.w2Income,
        w2_income_annualized: w2Annualized,
        se_tax: seTax,
        half_se_deduction: halfSe,
        additional_medicare_tax: additionalMedicare,
        federal_agi: federalAgi,
        federal_standard_deduction: fedStdDed,
        federal_taxable_income: fedTaxable,
        federal_tax: fedTax,
        qbi_deduction: qbiDeduction,
        state_agi: stateAgi,
        state_standard_deduction: stateStdDed,
        state_taxable_income: stateTaxable,
        state_personal_exemption: stateExemption,
        state_tax: stateTax,
        federal_withholding: fedWithholdingAnnual,
        state_withholding: stateWithholdingAnnual,
        federal_estimated_paid: opts.federalEstimatedPaid,
        state_estimated_paid: opts.stateEstimatedPaid,
        federal_total_liability: federalTotalLiability,
        state_total_liability: stateTax,
        federal_net_due: federalNetDue,
        state_net_due: stateNetDue,
        federal_quarterly_amount: fedQuarterly,
        state_quarterly_amount: stateQuarterly,
        quarters_remaining: Math.max(1, 5 - opts.quarter),
        ss_wage_base: payroll.ss_wage_base,
        se_taxable_fraction: payroll.se_taxable_fraction,
        state_installment_schedule: stateCumulative,
        state: stateCode,
        state_name: jurisdiction?.name ?? '',
        state_starts_from: stateCode ? startsFrom : '',
        state_available: stateAvailable,
        state_unavailable_reason: stateReason,
        federal_rates: provenance(fedBlock, config.tax_year, false),
        state_rates: stateCode ? provenance(stateBlock, config.tax_year, false) : null,
      };
    }

    function seIncomeFor(ledger: string | null): number {
      const name = ledger || DEFAULT_LEDGER;
      const monthly = SE_MONTHLY_BY_LEDGER[name.toLowerCase()];
      // An unknown ledger yields no SE income rather than an error, matching
      // the route's `except: pass` around the ledger query.
      return monthly === undefined ? 0 : monthly * MONTHS;
    }

    // --- Tax config (mutable, shared with the estimate above) -------------
    // In the same closure deliberately: picking a state on the settings page
    // has to move the estimate, which is the whole thing the settings page is
    // for. Two closures would give two disconnected `config` objects.

    const schedules: Array<{
      tax_year: number;
      jurisdiction: string;
      filing_status: string;
      brackets: Bracket[] | null;
      standard_deduction: number | null;
    }> = [];
    const yearRates: Record<number, Record<string, number>> = {};

    function findSchedule(year: number, jurisdiction: string, status: string) {
      return schedules.find(
        (r) => r.tax_year === year && r.jurisdiction === jurisdiction && r.filing_status === status,
      );
    }

    function field(value: unknown, overridden: boolean) {
      return { value, overridden };
    }

    function resolvedPayload(year: number, status: string, stateOverride?: string | null) {
      const fedBlock = federalYear(year);
      const fedStatus = fedBlock?.filing_status?.[status] ?? {};
      const fedOverride = findSchedule(year, 'federal', status);

      const payrollOverride = yearRates[year] ?? {};
      const bundledPayroll = fedBlock?.payroll ?? {};
      const payroll: Record<string, unknown> = {};
      for (const key of ['ss_wage_base', 'ss_rate', 'medicare_rate', 'se_taxable_fraction']) {
        const got = payrollOverride[key];
        payroll[key] = field(got ?? bundledPayroll[key] ?? null, got !== undefined);
      }

      const code = (stateOverride ?? config.state ?? '').toUpperCase();
      let statePayload: unknown = null;
      if (code) {
        const jur = jurisdictionOf(code);
        const bundled = jur ? stateYear(code, year) : null;
        const bundledStatus = bundled?.filing_status?.[status] ?? {};
        const override = findSchedule(year, code, status);
        const brackets = override?.brackets ?? bundledStatus.brackets ?? [];
        const stdDed = override?.standard_deduction ?? bundledStatus.standard_deduction ?? null;
        let reason = '';
        if (!jur) reason = 'unknown_state';
        else if (!jur.taxes_income) reason = 'no_income_tax';
        else if (!brackets.length) reason = 'no_brackets';
        statePayload = {
          code,
          name: jur?.name ?? '',
          taxes_income: !!jur?.taxes_income,
          available: reason === '',
          reason,
          starts_from: (TAX_RATES.states ?? {})[code]?.starts_from ?? 'federal_agi',
          installment_schedule: installmentSchedule(code),
          standard_deduction: field(stdDed, override?.standard_deduction != null),
          brackets: field(brackets, override?.brackets != null),
          provenance: provenance(bundled, year, false),
        };
      }

      return {
        status: 'ok',
        tax_year: year,
        filing_status: status,
        federal: {
          standard_deduction: field(
            fedOverride?.standard_deduction ?? fedStatus.standard_deduction ?? null,
            fedOverride?.standard_deduction != null,
          ),
          brackets: field(
            fedOverride?.brackets ?? fedStatus.brackets ?? [],
            fedOverride?.brackets != null,
          ),
          provenance: provenance(fedBlock, year, false),
        },
        payroll,
        state: statePayload,
      };
    }

    /** Mirrors the route's bracket validation, so the 400 class is exercised in dev. */
    function bracketProblem(raw: unknown): string | null {
      if (raw === null) return null;
      if (!Array.isArray(raw) || !raw.length) {
        return 'brackets must be a non-empty array of [threshold, rate] pairs';
      }
      let last: number | null = null;
      for (const pair of raw as any[]) {
        if (
          !Array.isArray(pair) ||
          pair.length !== 2 ||
          pair.some((v) => typeof v !== 'number' || !Number.isFinite(v))
        ) {
          return `malformed bracket: ${JSON.stringify(pair)} (expected [threshold, rate])`;
        }
        const [threshold, rate] = pair as [number, number];
        if (threshold < 0) return `bracket threshold must not be negative: ${threshold}`;
        if (rate < 0 || rate > 1) {
          return `bracket rate must be a fraction between 0 and 1: ${rate}`;
        }
        if (last !== null && threshold <= last) {
          return 'bracket thresholds must ascend and not repeat';
        }
        last = threshold;
      }
      return null;
    }

    function handleConfig(url: string, method: string, body: any): unknown | undefined {
      const path = url.split('?')[0];
      const params = new URLSearchParams(url.split('?')[1] ?? '');

      if (path === `${CONFIG_PATH}/jurisdictions` && method === 'GET') {
        const bundled = new Set(Object.keys(TAX_RATES.states ?? {}));
        return {
          status: 'ok',
          jurisdictions: (TAX_RATES.jurisdictions ?? []).map((j: any) => ({
            code: j.code,
            name: j.name,
            taxes_income: j.taxes_income,
            has_bundled_data: bundled.has(j.code),
            note: j.note ?? '',
          })),
        };
      }

      if (path === `${CONFIG_PATH}/resolved` && method === 'GET') {
        const year = Number(params.get('year')) || config.tax_year;
        const status = params.get('filing_status') ?? config.filing_status;
        if (status !== 'mfj' && status !== 'single') {
          return { __status: 400, status: 'error', error: `unknown filing status: ${status}` };
        }
        return resolvedPayload(year, status, params.get('state'));
      }

      if (path === `${CONFIG_PATH}/schedules` && method === 'GET') {
        return { status: 'ok', schedules: [...schedules] };
      }

      const scheduleMatch = path.match(
        /^\/istota\/api\/money\/config\/tax\/schedules\/(\d+)\/([^/]+)\/([^/]+)$/,
      );
      if (scheduleMatch) {
        const year = Number(scheduleMatch[1]);
        const jurisdiction =
          scheduleMatch[2].toLowerCase() === 'federal' ? 'federal' : scheduleMatch[2].toUpperCase();
        const status = scheduleMatch[3];
        if (status !== 'mfj' && status !== 'single') {
          return { __status: 400, status: 'error', error: `unknown filing status: ${status}` };
        }
        if (jurisdiction !== 'federal' && !jurisdictionOf(jurisdiction)) {
          return { __status: 400, status: 'error', error: `unknown jurisdiction: ${jurisdiction}` };
        }
        const idx = schedules.findIndex(
          (r) =>
            r.tax_year === year && r.jurisdiction === jurisdiction && r.filing_status === status,
        );
        if (method === 'DELETE') {
          if (idx >= 0) schedules.splice(idx, 1);
          return { status: 'ok', removed: idx >= 0 };
        }
        if (method === 'PUT') {
          const allowed = new Set(['brackets', 'standard_deduction']);
          const bad = Object.keys(body ?? {}).filter((k) => !allowed.has(k));
          if (bad.length) {
            return { __status: 400, status: 'error', error: `unknown keys: ${bad.sort()}` };
          }
          if ('brackets' in (body ?? {})) {
            const problem = bracketProblem(body.brackets);
            if (problem) return { __status: 400, status: 'error', error: problem };
          }
          const std = body?.standard_deduction;
          if (std !== undefined && std !== null) {
            if (typeof std !== 'number' || !Number.isFinite(std)) {
              return {
                __status: 400,
                status: 'error',
                error: 'standard_deduction must be a number',
              };
            }
            if (std < 0) {
              return {
                __status: 400,
                status: 'error',
                error: 'standard_deduction must not be negative',
              };
            }
          }
          const existing = idx >= 0 ? schedules[idx] : null;
          const brackets =
            'brackets' in (body ?? {}) ? body.brackets : (existing?.brackets ?? null);
          const standardDeduction =
            'standard_deduction' in (body ?? {})
              ? (body.standard_deduction ?? null)
              : (existing?.standard_deduction ?? null);
          if (brackets === null && standardDeduction === null) {
            if (idx >= 0) schedules.splice(idx, 1);
            return { status: 'ok', state: existing ? 'updated' : 'noop' };
          }
          const row = {
            tax_year: year,
            jurisdiction,
            filing_status: status,
            brackets,
            standard_deduction: standardDeduction,
          };
          if (idx >= 0) schedules[idx] = row;
          else schedules.push(row);
          return { status: 'ok', state: existing ? 'updated' : 'created' };
        }
      }

      const yearMatch = path.match(/^\/istota\/api\/money\/config\/tax\/years\/(\d+)$/);
      if (yearMatch && method === 'PUT') {
        const year = Number(yearMatch[1]);
        // An explicit null clears the field, matching upsert_tax_year_rates.
        // The mock used to store the null, so the revert appeared to work here
        // and silently failed against the real server — a mock more permissive
        // than the thing it stands in for is how that ships.
        const merged: Record<string, number> = { ...(yearRates[year] ?? {}) };
        for (const [k, v] of Object.entries(body ?? {})) {
          if (v === null) delete merged[k];
          else merged[k] = v as number;
        }
        yearRates[year] = merged;
        return { status: 'ok', state: 'updated' };
      }
      if (yearMatch && method === 'DELETE') {
        const removed = yearRates[Number(yearMatch[1])] !== undefined;
        delete yearRates[Number(yearMatch[1])];
        return { status: 'ok', removed };
      }
      if (path === `${CONFIG_PATH}/years` && method === 'GET') {
        return {
          status: 'ok',
          years: Object.entries(yearRates).map(([y, v]) => ({ tax_year: Number(y), ...v })),
        };
      }

      if (path === CONFIG_PATH && method === 'GET') {
        return {
          status: 'ok',
          tax: {
            filing_status: config.filing_status,
            tax_year: config.tax_year,
            state: config.state,
            w2_income: config.w2_income,
            w2_federal_withholding: config.w2_federal_withholding,
            w2_state_withholding: config.w2_state_withholding,
            federal_estimated_paid: config.federal_estimated_paid,
            state_estimated_paid: config.state_estimated_paid,
            enable_qbi_deduction: config.enable_qbi_deduction,
            prior_year_federal_tax: config.prior_year_federal_tax,
            prior_year_state_tax: config.prior_year_state_tax,
          },
        };
      }

      if (path === CONFIG_PATH && method === 'PUT') {
        const allowed = new Set([
          'filing_status',
          'tax_year',
          'state',
          'w2_income',
          'w2_federal_withholding',
          'w2_state_withholding',
          'federal_estimated_paid',
          'state_estimated_paid',
          'enable_qbi_deduction',
          'prior_year_federal_tax',
          'prior_year_state_tax',
        ]);
        const bad = Object.keys(body ?? {}).filter((k) => !allowed.has(k));
        if (bad.length) {
          return { __status: 400, status: 'error', error: `unknown keys: ${bad.sort()}` };
        }
        if ('state' in (body ?? {})) {
          const code = String(body.state ?? '')
            .trim()
            .toUpperCase();
          if (code && !jurisdictionOf(code)) {
            return { __status: 400, status: 'error', error: `unknown state: ${code}` };
          }
          config.state = code;
        }
        if ('filing_status' in (body ?? {})) {
          if (body.filing_status !== 'mfj' && body.filing_status !== 'single') {
            return {
              __status: 400,
              status: 'error',
              error: `unknown filing status: ${body.filing_status}`,
            };
          }
          config.filing_status = body.filing_status;
        }
        for (const [k, v] of Object.entries(body ?? {})) {
          if (k === 'state' || k === 'filing_status') continue;
          (config as any)[k] = v;
        }
        return { status: 'ok' };
      }

      return undefined;
    }

    return ({ url, method, body }) => {
      if (url.startsWith(CONFIG_PATH)) return handleConfig(url, method, body);
      if (!url.startsWith(PATH)) return undefined;
      if (NO_TAX_CONFIG) return { __status: 404, error: 'no tax config' };

      const params = new URLSearchParams(url.split('?')[1] ?? '');
      const ledger = params.get('ledger');

      const pick = (key: string, fallback: number): number => {
        const v = savedInputs[key];
        return typeof v === 'number' ? v : fallback;
      };

      if (method === 'GET') {
        // A `method` query param only wins when it isn't the default, so a
        // saved safe-harbor choice survives the page's plain reload.
        const requested = params.get('method') ?? 'annualized';
        const useMethod =
          requested !== 'annualized' ? requested : ((savedInputs.method as string) ?? requested);
        return estimate({
          seIncomeYtd: seIncomeFor(ledger),
          w2Income: pick('w2_income', config.w2_income),
          w2FederalWithholding: pick('w2_federal_withholding', config.w2_federal_withholding),
          w2StateWithholding: pick('w2_state_withholding', config.w2_state_withholding),
          federalEstimatedPaid: pick('federal_estimated_paid', config.federal_estimated_paid),
          stateEstimatedPaid: pick('state_estimated_paid', config.state_estimated_paid),
          method: useMethod,
          w2Months: pick('w2_months', 12),
          quarter: CURRENT_QUARTER,
          months: MONTHS,
        });
      }

      if (method === 'POST') {
        const bval = (key: string, fallback: number): number => {
          const v = body?.[key];
          return typeof v === 'number' && Number.isFinite(v) ? v : fallback;
        };
        const inputs = {
          method: typeof body?.method === 'string' ? body.method : 'annualized',
          w2_income: bval('w2_income', config.w2_income),
          w2_federal_withholding: bval('w2_federal_withholding', config.w2_federal_withholding),
          w2_state_withholding: bval('w2_state_withholding', config.w2_state_withholding),
          federal_estimated_paid: bval('federal_estimated_paid', config.federal_estimated_paid),
          state_estimated_paid: bval('state_estimated_paid', config.state_estimated_paid),
          w2_months: bval('w2_months', 12),
        };
        Object.assign(savedInputs, inputs);

        return estimate({
          seIncomeYtd: seIncomeFor(ledger),
          w2Income: inputs.w2_income,
          w2FederalWithholding: inputs.w2_federal_withholding,
          w2StateWithholding: inputs.w2_state_withholding,
          federalEstimatedPaid: inputs.federal_estimated_paid,
          stateEstimatedPaid: inputs.state_estimated_paid,
          method: inputs.method,
          w2Months: inputs.w2_months,
          quarter: CURRENT_QUARTER,
          months: MONTHS,
        });
      }

      return undefined;
    };
  })(),
];

function readBody(req: any): Promise<any> {
  return new Promise((resolve) => {
    const chunks: Buffer[] = [];
    req.on('data', (c: Buffer) => chunks.push(c));
    req.on('end', () => {
      if (chunks.length === 0) return resolve(undefined);
      const raw = Buffer.concat(chunks).toString('utf8');
      try {
        resolve(JSON.parse(raw));
      } catch {
        resolve(raw);
      }
    });
    req.on('error', () => resolve(undefined));
  });
}

export function mockApi(): Plugin {
  return {
    name: 'istota-mock-api',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        // Money's routes live under /istota/api/money, already covered by the
        // /istota/api/ prefix — kept explicit so the intent survives a refactor.
        if (!req.url?.startsWith('/istota/api/') && !req.url?.startsWith('/istota/money/api/'))
          return next();

        const method = req.method ?? 'GET';
        const respond = (body: unknown) => {
          // A handler signals a non-JSON body (a file stream) by returning
          // `__raw` + `__contentType`. Needed so the health document routes
          // can hand the dev browser something it will actually open.
          if (body && typeof body === 'object' && '__raw' in (body as any)) {
            const { __raw, __contentType, __disposition, __headers } = body as any;
            res.setHeader('Content-Type', __contentType || 'application/octet-stream');
            // `attachment` unless the handler says otherwise: the health
            // document routes want the browser to save the file, an avatar
            // has to render in an `<img>`.
            res.setHeader('Content-Disposition', __disposition || 'attachment');
            res.setHeader('X-Content-Type-Options', 'nosniff');
            for (const [k, v] of Object.entries((__headers ?? {}) as Record<string, string>)) {
              res.setHeader(k, v);
            }
            res.statusCode = 200;
            res.end(__raw);
            return;
          }
          res.setHeader('Content-Type', 'application/json');
          // A handler signals a non-200 by returning `__status`, and response
          // headers by returning `__headers`; both keys are stripped so the
          // payload matches what the real API returns. Without the first,
          // error paths (404 / 409 conflict / 400 validation) can't be
          // exercised against the mock at all.
          let payload = body;
          let code = 200;
          if (body && typeof body === 'object' && '__status' in (body as any)) {
            const { __status, ...rest } = body as any;
            code = Number(__status) || 200;
            payload = rest;
          }
          if (payload && typeof payload === 'object' && '__headers' in (payload as any)) {
            const { __headers, ...rest } = payload as any;
            for (const [k, v] of Object.entries((__headers ?? {}) as Record<string, string>)) {
              res.setHeader(k, v);
            }
            payload = rest;
          }
          res.statusCode = code;
          res.end(JSON.stringify(payload));
        };

        const dispatch = (parsedBody: any) => {
          const ctx: MockReq = { url: req.url!, method, body: parsedBody };
          for (const h of handlers) {
            const body = h(ctx);
            if (body !== undefined) {
              respond(body);
              return;
            }
          }
          if (method !== 'GET') {
            respond({});
            return;
          }
          res.statusCode = 404;
          res.end('mock not implemented');
        };

        if (method === 'GET' || method === 'HEAD') {
          dispatch(undefined);
        } else {
          readBody(req).then(dispatch);
        }
      });
    },
  };
}
