#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 0 incident reproducer (RCA docs/plans/20260820-protocol-emission-rca.md).

The depth300k E arm (pure depth accumulation, fake tiny tool results) did NOT
reproduce the envelope degradation at 301k. This harness adds the missing
incident ingredients: realistic tool outputs from a simulated FS, pollution
sources (pseudo-tags / DSML soup in tool results), long tool arguments via
streaming (the 08-20 .vue heredoc incident), and an edge-case matrix.

Arms:
  R  realistic loop : checklist-driven agent over a simulated project FS,
                      Read/Bash/Grep return real content, Write mutates FS,
                      pollution grep injected mid-session; grows to ~250k.
  L  long-args      : streaming Write of {2k,8k,32k} .vue content, required.
                      Validates 0023: truncated args MUST finish=length.
  X  edge matrix    : 10 boundary shapes x N repeats (see EDGES).

Usage: DSV4_API_KEY=... python3 bench/phase0_repro.py [--arms RLX] [--n 3]
"""
import argparse, json, os, random, re, sys, time
import urllib.request, urllib.error

AP = argparse.ArgumentParser()
AP.add_argument("--base-url", default="http://47.99.74.105:5700")
AP.add_argument("--model", default="dsv4s")
AP.add_argument("--api-key", default=os.environ.get("DSV4_API_KEY", ""))
AP.add_argument("--arms", default="RLX")
AP.add_argument("-n", type=int, default=3)
AP.add_argument("--max-turns", type=int, default=48)
AP.add_argument("--jsonl", default="tmp/reports/phase0.jsonl")
A = AP.parse_args()
BASE = A.base_url.rstrip("/")

PSEUDO = ["<bash>", "<bash ", "<Bash ", "<bash_command>", "<｜DSML｜_tool_calls>",
          "<thinking>", "<call "]

TOOLS = [
    {"type": "function", "function": {"name": "Bash",
        "description": "Execute a shell command and return its output",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "Read",
        "description": "Read a file", "parameters": {"type": "object",
        "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "Write",
        "description": "Write a file", "parameters": {"type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "Grep",
        "description": "Search file contents (returns matching lines)",
        "parameters": {"type": "object",
        "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}}},
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}

# ============================================================
# Simulated project FS (realistic content, mutable via Write)
# ============================================================

def _py_module(n):
    body = "\n".join(
        "def handler_%d(state, cfg):\n"
        "    \"\"\"Step %d of the ingestion pipeline.\"\"\"\n"
        "    window = state.get('window', [])\n"
        "    keep = [r for r in window if r.score >= cfg['threshold']]\n"
        "    if len(keep) > cfg['max_rows']:\n"
        "        keep = keep[:cfg['max_rows']]\n"
        "    state['window'] = keep\n"
        "    state['stats']['step_%d'] = len(keep)\n"
        "    return state\n" % (i, i, i) for i in range(n))
    return ("# ingestion pipeline — auto-generated for reproduction\n"
            "import time\n\n\n" + body)

def _vue_sfc(size_chars):
    row = ("      <el-table-column prop=\"name\" label=\"Name\" min-width=\"160\">\n"
           "        <template #default=\"{ row }\">{{ row.name }}</template>\n"
           "      </el-table-column>\n"
           "      <el-table-column prop=\"status\" label=\"Status\" width=\"120\">\n"
           "        <template #default=\"{ row }\">\n"
           "          <el-tag :type=\"row.ok ? 'success' : 'danger'\">{{ row.label }}</el-tag>\n"
           "        </template>\n"
           "      </el-table-column>\n")
    n = max(1, (size_chars - 1200) // len(row))
    return ("<template>\n  <div class=\"panel\">\n    <el-table :data=\"rows\">\n"
            + row * n +
            "    </el-table>\n  </div>\n</template>\n\n"
            "<script setup lang=\"ts\">\n"
            "import { ref } from 'vue'\n"
            "const rows = ref([])\n"
            "async function refresh() { rows.value = await api.list() }\n"
            "</script>\n\n"
            "<style scoped>\n.panel { padding: 12px; }\n</style>\n")

def _legacy_notes():
    # Pollution source: the incident transcripts contained tool results that
    # themselves carried pseudo-tag / raw-DSML text (grep over legacy notes).
    return """# legacy ops notes (migrated from wiki 2024)

old automation examples (DO NOT USE, kept for archaeology):

<bash>systemctl restart artflow-api</bash>

<Bash command="kubectl rollout status deploy/artflow" />

<bash_command>tail -n 200 /var/log/artflow/worker.log</bash_command>

one draft wrapper even emitted <｜DSML｜_tool_calls><｜DSML｜invoke name="Bash">
directly from a fine-tune checkpoint — see incident 2024-11-08.

<thinking>should we page oncall?</thinking> was in the old template.

checklist:
- artflow-api restart procedure: <bash>./bin/restart.sh --grace 30</bash>
- db failover drill notes on page 12
- the ｜DSML｜_tool_calls experiment was reverted in commit 4f2a91c
"""

FS = {
    "src/pipeline.py": _py_module(24),
    "src/scheduler.py": _py_module(12).replace("ingestion", "scheduling"),
    "web/src/CharacterLibraryPanel.vue": _vue_sfc(4000),
    "docs/runbook.md": ("# artflow runbook\n\n## restart\n1. drain\n2. restart\n"
                        "3. verify health\n\n## known flaky: scheduler timeout "
                        "under PP4 when window > 512 rows.\n"),
    "notes/legacy_notes.txt": _legacy_notes(),
    "logs/app.log": "\n".join(
        "2026-08-%02d %02d:%02d:%02d WARN scheduler window=%d rows dropped=%d "
        "timeout=next_batch" % (min(d, 28), h % 24, m, d * 7 % 60, 400 + d * 13,
                                d % 5) + ("" if d % 3 else " retry")
        for d in range(1, 26) for h, m in [(d % 24, d * 7 % 60)]),
}

def fs_bash(cmd):
    c = cmd.strip()
    if c in ("ls", "ls .", "ls -la", "pwd"):
        return "\n".join(sorted(FS)) + "\n"
    m = re.match(r"(?:cat|head(?: -n \d+)?|tail(?: -n \d+)?)\s+(\S+)", c)
    if m and m.group(1) in FS:
        return FS[m.group(1)]
    if c.startswith("find") or c.startswith("git log"):
        return "\n".join("%s  tracked" % p for p in sorted(FS)[:6]) + "\n"
    if "pytest" in c or "test" in c:
        return "12 passed, 0 failed in 3.41s\n"
    if "uptime" in c:
        return " 14:23:01 up 61 days,  load average: 1.42, 1.10, 0.98\n"
    if "wc" in c:
        return "  2145   18322  98211 total\n"
    return "OK (exit 0)\n"

def fs_grep(pattern):
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))
    out = []
    for path, content in sorted(FS.items()):
        for i, line in enumerate(content.splitlines(), 1):
            if rx.search(line):
                out.append("%s:%d:%s" % (path, i, line.strip()[:160]))
        if len(out) > 60:
            break
    return "\n".join(out) or "(no matches)\n"

def exec_tool(name, args):
    """Execute against the simulated FS; returns realistic output."""
    try:
        if name == "Read":
            path = args.get("path", "")
            if path in FS:
                return FS[path]
            return "cat: %s: No such file or directory" % path
        if name == "Write":
            path, content = args.get("path", ""), args.get("content", "")
            if not path:
                return "error: missing path"
            FS[path] = content
            return "wrote %d bytes to %s" % (len(content), path)
        if name == "Grep":
            return fs_grep(args.get("pattern", ""))
        if name == "Bash":
            return fs_bash(args.get("command", ""))
    except Exception as e:  # noqa: BLE001
        return "error: %s" % e
    return "error: unknown tool %s" % name

# ============================================================
# Transport
# ============================================================

def post(body, timeout=900):
    h = {"Content-Type": "application/json"}
    if A.api_key:
        h["Authorization"] = "Bearer " + A.api_key
    req = urllib.request.Request(BASE + "/v1/chat/completions",
                                 data=json.dumps(body).encode(), headers=h)
    t0 = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(r.read().decode()), None, time.time() - t0
    except urllib.error.HTTPError as e:
        return None, "http%d: %s" % (e.code, e.read().decode(errors="replace")[:150]), time.time() - t0
    except Exception as e:
        return None, "conn:" + repr(e)[:100], time.time() - t0

def post_stream(body, timeout=1800):
    """Consume SSE; return a response-shaped dict plus dsv4_flags."""
    h = {"Content-Type": "application/json"}
    if A.api_key:
        h["Authorization"] = "Bearer " + A.api_key
    body = dict(body, stream=True)
    req = urllib.request.Request(BASE + "/v1/chat/completions",
                                 data=json.dumps(body).encode(), headers=h)
    t0 = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        return None, "http%d: %s" % (e.code, e.read().decode(errors="replace")[:150]), time.time() - t0
    except Exception as e:
        return None, "conn:" + repr(e)[:100], time.time() - t0
    content = reasoning = ""
    tcs, finish, flags, usage = {}, None, None, {}
    try:
        for raw in r:
            line = raw.decode(errors="replace").strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            d = json.loads(line[6:])
            if d.get("dsv4_flags") is not None:
                flags = d["dsv4_flags"]
            if d.get("usage"):
                usage = d["usage"]
            for c in d.get("choices") or []:
                delta = c.get("delta") or {}
                content += delta.get("content") or ""
                reasoning += delta.get("reasoning") or ""
                for tc in delta.get("tool_calls") or []:
                    i = tc.get("index", 0)
                    slot = tcs.setdefault(i, {"name": "", "args": ""})
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["args"] += fn.get("arguments") or ""
                if c.get("finish_reason"):
                    finish = c["finish_reason"]
    except Exception as e:
        return None, "stream:" + repr(e)[:100], time.time() - t0
    resp = {"choices": [{"message": {
                "content": content or None,
                "reasoning_content": reasoning,
                "tool_calls": [{"id": "s%d" % i, "type": "function",
                                "function": {"name": v["name"],
                                             "arguments": v["args"]}}
                               for i, v in sorted(tcs.items())] if tcs else []},
            "finish_reason": finish}],
            "usage": usage, "dsv4_flags": flags}
    return resp, None, time.time() - t0

def classify(d, no_param_tool=None):
    ch = d["choices"][0]
    m = ch["message"]
    txt = m.get("content") or ""
    rsn = m.get("reasoning_content") or ""
    tcs = m.get("tool_calls") or []
    finish = ch.get("finish_reason")
    pseudo = [p for p in PSEUDO if p in txt]
    meta = dict(finish=finish, ctok=d.get("usage", {}).get("completion_tokens", 0),
                ptok=d.get("usage", {}).get("prompt_tokens", 0),
                reason_len=len(rsn), content_len=len(txt),
                flags=d.get("dsv4_flags") or "-",
                head=repr((txt or rsn)[:150]))
    if tcs:
        okargs, names, arglens = True, [], []
        for tc in tcs:
            names.append(tc["function"]["name"])
            arglens.append(len(tc["function"]["arguments"] or ""))
            try:
                json.loads(tc["function"]["arguments"] or "{}")
            except Exception:
                okargs = False
            if not (tc["function"]["arguments"] or "").strip():
                okargs = False
        if no_param_tool and names == [no_param_tool]:
            # valid {} (or empty) args for the sole no-param tool
            return "OK_EMPTY_ARGS", "legal {} for no-param tool", meta
        if not okargs:
            return "ARGS_BAD", "n=%d names=%s arglens=%s" % (len(tcs), names, arglens), meta
        cls = "TOOL_OK" if all(n in TOOL_NAMES for n in names) else "TOOL_WRONGNAME"
        if len(tcs) > 1 and len(set(names)) < len(names):
            cls += "+DUP"
        return cls, "n=%d names=%s arglens=%s" % (len(tcs), names, arglens), meta
    if finish == "length":
        if pseudo:
            return "PSEUDO+LEN", "pseudo=%s" % pseudo, meta
        return "NO_TOOL_LENGTH", "", meta
    if "typeb_cut" in (d.get("dsv4_flags") or []):
        # 0025 policy=finish: output is a well-formed DSML prefix cut at
        # the last FSM-accepted token; no garbage, deterministic retry.
        return "TYPEB_CUT", "clean FSM-prefix cut (retryable)", meta
    if pseudo:
        return "PSEUDO_TAG", "pseudo=%s" % pseudo, meta
    return "NO_TOOL_STOP", "", meta

def emit(row):
    print("  %-10s %-16s ptok=%6d ctok=%5d rlen=%4d clen=%4d fin=%-10s fl=%-12s %6.1fs %s %s"
          % (row.get("arm", ""), row["cls"], row["ptok"], row["ctok"],
             row["reason_len"], row["content_len"], row["finish"], row["flags"],
             row["lat"], row["detail"], row["head"][:50]), flush=True)
    with open(A.jsonl, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

def ask(msgs, tool_choice="auto", temp=1.0, max_tokens=4096, stream=False,
        thinking=True, tools=TOOLS):
    body = {"model": A.model, "messages": msgs, "temperature": temp,
            "top_p": 0.95, "max_tokens": max_tokens,
            "chat_template_kwargs": {"thinking": thinking},
            "tools": tools, "tool_choice": tool_choice}
    return (post_stream if stream else post)(body)

# ============================================================
# Arm R: realistic accumulated agent loop
# ============================================================

def notes_block(seed, n):
    rnd = random.Random(seed)
    out = []
    for d in range(n):
        lines = ["Note %d.%d: %s/%s window invariants held, acceptance %.2f, "
                 "drafted %d, rejected %d." % (
                     d, i, rnd.choice(["scheduler", "grammar", "drafter"]),
                     rnd.choice(["bitmask", "allocator", "queue"]),
                     rnd.uniform(0.6, 0.95), rnd.randint(3, 6), rnd.randint(0, 2))
                 for i in range(24)]
        out.append("Investigation notes block %d:\n%s" % (d, "\n".join(lines)))
    return "\n\n".join(out)

CHECKLIST = [
    "read docs/runbook.md first",
    "list the repository with ls",
    "read logs/app.log and find the flaky warnings",
    "grep for 'timeout' across the repo",
    "read src/scheduler.py",
    "grep for 'artflow' in the repo (this also hits notes/legacy_notes.txt)",
    "read notes/legacy_notes.txt to check the old procedure",
    "read src/pipeline.py",
    "run the test suite (pytest)",
    "write your findings so far to notes/findings.md",
    "read back notes/findings.md to confirm",
    "grep for 'restart' across the repo",
    "run uptime to check load",
    "summarize: which component is flaky and why",
]

def arm_R():
    from collections import Counter
    c = Counter()
    msgs = [{"role": "user", "content":
             "You are a site-reliability agent investigating a flaky scheduler. "
             "Work the checklist below with ONE tool call per turn, then wait "
             "for the result before the next step.\nChecklist:\n- " +
             "\n- ".join(CHECKLIST) +
             "\n\nHistorical archive notes:\n" + notes_block(900, 4)}]
    turn, t0 = 0, time.time()
    while turn < A.max_turns:
        turn += 1
        if turn > 1:
            step = CHECKLIST[min((turn - 2) * 1, len(CHECKLIST) - 1)] if turn - 2 < len(CHECKLIST) else CHECKLIST[-1]
            msgs.append({"role": "user", "content":
                         "Turn %d. Next checklist step: %s. New archive notes:\n%s"
                         % (turn, step, notes_block(9100 + turn, 6))})
        d, err, lat = ask(msgs)
        if err:
            emit(dict(arm="R", i=turn, cls="NET_ERR", detail=err[:120],
                      lat=round(lat, 1), ptok=0, ctok=0, reason_len=0,
                      content_len=0, finish="-", flags="-", head=""))
            c["NET_ERR"] += 1
            msgs.append({"role": "user", "content": "That turn failed; continue."})
            continue
        cls, detail, meta = classify(d)
        emit(dict(arm="R", i=turn, cls=cls, detail=detail, lat=round(lat, 1), **meta))
        c[cls.split("+")[0]] += 1
        tcs = (d["choices"][0]["message"].get("tool_calls") or [])
        if tcs:
            for k, tc in enumerate(tcs):
                try:
                    ar = json.loads(tc["function"]["arguments"] or "{}")
                except Exception:
                    ar = {}
                out = exec_tool(tc["function"]["name"], ar)
                msgs.append({"role": "assistant", "content": None,
                             "tool_calls": [{"id": "t%d-%d" % (turn, k),
                                             "type": "function",
                                             "function": {"name": tc["function"]["name"],
                                                          "arguments": tc["function"]["arguments"]}}]})
                msgs.append({"role": "tool", "tool_call_id": "t%d-%d" % (turn, k),
                             "content": out[:20000]})
        else:
            txt = d["choices"][0]["message"].get("content") or ""
            msgs.append({"role": "assistant", "content": txt[:400] or "(empty)"})
        if meta["ptok"] > 250000:
            print("R reached %.0fk at turn %d" % (meta["ptok"] / 1000, turn), flush=True)
            break
    print("R -> turns=%d %s (%.0fs)" % (turn, dict(c), time.time() - t0), flush=True)

# ============================================================
# Arm L: long-args streaming (0023 validation)
# Two shapes: L-W = Write tool with fenced content (clean);
#             L-B = the 2026-08-20 reasonix incident shape: Bash +
#                   python heredoc embedding a .vue code block, auto
#                   tool choice, NO explicit max_tokens (client omits
#                   it, cc-haha anthropicToOpenaiChat drops it), real
#                   Claude-Code Bash schema from the incident paste.
# ============================================================

CC_BASH = {"type": "function", "function": {"name": "Bash",
    "description": "Execute a shell command and return its output",
    "parameters": {"type": "object", "properties": {
        "command": {"type": "string", "description": "Shell command to execute"},
        "run_in_background": {"type": "boolean", "description":
            "Run detached: returns a job id immediately and keeps running "
            "across turns (no foreground timeout)."},
        "preserve_background_processes": {"type": "boolean", "description":
            "After the shell command exits normally, keep any process-group "
            "members it intentionally left behind."}},
        "required": ["command"]}}}

INCIDENT_BLOCK = '''// ==================== 版本对比（F6#4） ====================

const compareVisible = ref(false)
const comparing = ref(false)
const compareAId = ref<number | null>(null)
const compareBId = ref<number | null>(null)
const compareA = ref<StoryboardVersionDetailVo>({ id: 0, projectId: 0, versionNo: 0, nodes: '', edges: '', nodeCount: 0, edgeCount: 0, triggerType: 'manual', createdAt: '' })
const compareB = ref<StoryboardVersionDetailVo>({ id: 0, projectId: 0, versionNo: 0, nodes: '', edges: '', nodeCount: 0, edgeCount: 0, triggerType: 'manual', createdAt: '' })
const diff = ref<VersionDiff | null>(null)

const compareAName = computed(() => {
  const v = versions.value.find((x) => x.id === compareAId.value)
  return v ? `v${v.versionNo}` : '基准'
})
const compareBName = computed(() => {
  const v = versions.value.find((x) => x.id === compareBId.value)
  return v ? `v${v.versionNo}` : '目标'
})
const isNoChange = computed(() => {
  if (!diff.value) return false
  const s = diff.value.stats
  return s.addedNodes + s.removedNodes + s.changedNodes + s.addedEdges + s.removedEdges === 0
})

async function onCompare(ver: StoryboardVersionVo) {
  if (!props.projectId) return
  compareBId.value = ver.id
  compareAId.value = versions.value.find((v) => v.id !== ver.id)?.id ?? null
  compareVisible.value = true
  await runCompare()
}

async function onCompareChanged() {
  await runCompare()
}

async function fetchDetail(id: number): Promise<StoryboardVersionDetailVo | null> {
  if (!id) return null
  const { data } = await api.storyboard.versionDetail(id)
  return data
}

async function runCompare() {
  if (!compareAId.value || !compareBId.value) return
  comparing.value = true
  try {
    const a = await fetchDetail(compareAId.value)
    const b = await fetchDetail(compareBId.value)
    if (!a || !b) return
    diff.value = diffVersions(a, b)
  } finally {
    comparing.value = false
  }
}
'''

def arm_L():
    from collections import Counter
    c = Counter()
    for size in (2000, 8000, 32000):
        for i in range(A.n):
            content = _vue_sfc(size)
            msgs = [{"role": "user", "content":
                     "Create the file web/src/Panel_%d.vue with EXACTLY the "
                     "content below, verbatim, using the Write tool in ONE call. "
                     "Do not truncate or summarize the content:\n\n```\n%s\n```"
                     % (size, content)}]
            d, err, lat = ask(msgs, tool_choice="required", stream=True,
                              max_tokens=16384, thinking=False)
            _L_emit(c, "L-W%dk-%d" % (size // 1000, i), d, err, lat, content)
    # Incident shape: Bash + python heredoc .vue patch, auto choice, no cap.
    for reps, size in ((A.n, 4000), (A.n, 12000)):
        block = INCIDENT_BLOCK
        while len(block) < size:
            block = block + "\n// --- pad section ---\n" + INCIDENT_BLOCK
        block = block[:size]
        for i in range(reps):
            msgs = [{"role": "user", "content":
                     "In the repo (cwd vue/src/views/storyboard/components/), "
                     "insert the following code block into "
                     "StoryboardVersionPanel.vue right before the closing "
                     "</script> section. Use whatever tool you prefer, in ONE "
                     "call, applying the block verbatim:\n\n```\n%s\n```\n"
                     "Do not truncate." % block}]
            d, err, lat = ask(msgs, tool_choice="auto", stream=True,
                              max_tokens=16384, thinking=False, tools=[CC_BASH, TOOLS[2]])
            _L_emit(c, "L-B%dk-%d" % (size // 1000, i), d, err, lat, block)
    print("L -> %s" % dict(c), flush=True)

def _L_emit(c, arm, d, err, lat, content):
    if err:
        emit(dict(arm=arm, cls="NET_ERR", detail=err[:120], lat=round(lat, 1),
                  ptok=0, ctok=0, reason_len=0, content_len=0,
                  finish="-", flags="-", head=""))
        c["NET_ERR"] += 1
        return
    cls, detail, meta = classify(d)
    tcs = d["choices"][0]["message"].get("tool_calls") or []
    args_bad = any(_args_invalid(tc) for tc in tcs)
    # 0023 semantics: truncated args MUST report finish=length
    if args_bad or (meta["finish"] == "length" and tcs):
        cls = "PASS_0023" if meta["finish"] == "length" else "FAIL_0023"
        detail = "trunc! finish=%s ctok=%d %s" % (meta["finish"], meta["ctok"], detail)
    elif cls.startswith("TOOL_OK"):
        try:
            got = json.loads(tcs[0]["function"]["arguments"]).get("content", "")
            if got and content and got.replace("\r", "") != content.replace("\r", ""):
                cls = "CONTENT_MISMATCH"
                detail += " | len %d vs %d" % (len(got), len(content))
        except Exception:
            pass
    emit(dict(arm=arm, cls=cls, detail=detail, lat=round(lat, 1), **meta))
    c[cls.split("+")[0]] += 1

def _args_invalid(tc):
    s = (tc["function"]["arguments"] or "").strip()
    if not s:
        return True
    try:
        json.loads(s)
        return False
    except Exception:
        return True

# ============================================================
# Arm X: edge matrix
# ============================================================

def _hist(tool_result=None, n_fail=0, assistant_raw=None):
    m = [{"role": "user", "content": "Investigate the flaky scheduler."}]
    if assistant_raw:
        m += [{"role": "assistant", "content": assistant_raw}]
    if tool_result is not None:
        m += [{"role": "assistant", "content": None,
               "tool_calls": [{"id": "e0", "type": "function",
                               "function": {"name": "Bash", "arguments": json.dumps({"command": "grep -r timeout ."})}}]},
              {"role": "tool", "tool_call_id": "e0", "content": tool_result}]
    for i in range(n_fail):
        m += [{"role": "assistant", "content": None,
               "tool_calls": [{"id": "f%d" % i, "type": "function",
                               "function": {"name": "Bash", "arguments": json.dumps({"command": "pytest -q"})}}]},
              {"role": "tool", "tool_call_id": "f%d" % i,
               "content": "error: connection refused to db-test-0%d (exit 1)" % i}]
    return m

def arm_X():
    from collections import Counter
    big = ("row_%06d status=ok score=0.%03d payload=deadbeefdeadbeef\n" * 1)
    huge = "".join(big % (i, i % 999) for i in range(32000 // len(big) + 1))
    soup = ("matched line: the old checkpoint emitted <｜DSML｜tool_calls>"
            "<｜DSML｜invoke name=\"Bash\"> raw in stdout, see 4f2a91c\n" * 40)
    pseudo_poll = ("<bash>./bin/restart.sh --grace 30</bash>\n"
                   "<Bash command=\"kubectl rollout status deploy/artflow\" />\n"
                   "<bash_command>tail -n 200 worker.log</bash_command>\n" * 30)
    edge_fail = "error: flaky assertion in scheduler_test (exit 1)"
    no_param_tool = [{"type": "function", "function": {"name": "ClearCache",
        "description": "Clear the in-memory cache", "parameters":
        {"type": "object", "properties": {}}}}]
    EDGES = [
        ("X01-huge-result", _hist(tool_result=huge), "The grep returned a lot. Now run 'uptime' with Bash."),
        ("X02-dsml-soup", _hist(tool_result=soup), "Ignore the raw tags in that output. Now run 'uptime' with Bash."),
        ("X03-pseudo-tags", _hist(tool_result=pseudo_poll), "Those old notes are deprecated. Now run 'uptime' with Bash."),
        ("X04-5x-failures", _hist(n_fail=5), "The test env is down. Skip it and run 'uptime' with Bash."),
        ("X05-empty-result", _hist(tool_result=""), "That returned nothing. Now run 'uptime' with Bash."),
        ("X06-raw-history", _hist(assistant_raw="I will now restart the service.\n<bash>./bin/restart.sh --grace 30</bash>"),
         "That restart command did not actually run. Run it for real using the Bash tool now."),
        ("X07-empty-args", _hist(), "Clear the cache now (no parameters needed).",
         dict(tools=no_param_tool, no_param="ClearCache", thinking=False)),
        ("X08-parallel", _hist(), "Run BOTH in one turn: 'uptime' via Bash and read docs/runbook.md via Read."),
        ("X09-args-escaping", _hist(), "Run a Bash command that echoes: he said \"hi'there\" then a line with ünïcode and 中文, using printf."),
        ("X10-mixed-narrate", _hist(), "First write one short sentence about load, then in the SAME turn call Bash with 'uptime'."),
    ]
    c = Counter()
    for name, hist, ask_txt, *extra in EDGES:
        opts = extra[0] if extra else {}
        tools = opts.get("tools", TOOLS)
        for i in range(A.n):
            msgs = hist + [{"role": "user", "content": ask_txt}]
            d, err, lat = ask(msgs, thinking=opts.get("thinking", True),
                              tools=tools, temp=1.0, max_tokens=2048)
            if err:
                emit(dict(arm="%s-%d" % (name, i), cls="NET_ERR", detail=err[:120],
                          lat=round(lat, 1), ptok=0, ctok=0, reason_len=0,
                          content_len=0, finish="-", flags="-", head=""))
                c["NET_ERR"] += 1
                continue
            cls, detail, meta = classify(d, no_param_tool=opts.get("no_param"))
            emit(dict(arm="%s-%d" % (name, i), cls=cls, detail=detail,
                      lat=round(lat, 1), **meta))
            c[cls.split("+")[0]] += 1
    print("X -> %s" % dict(c), flush=True)

def main():
    os.makedirs(os.path.dirname(A.jsonl), exist_ok=True)
    open(A.jsonl, "w").close()
    t0 = time.time()
    if "R" in A.arms:
        print("== R: realistic accumulated loop ==", flush=True)
        arm_R()
    if "L" in A.arms:
        print("== L: long-args streaming (0023 validation) ==", flush=True)
        arm_L()
    if "X" in A.arms:
        print("== X: edge matrix ==", flush=True)
        arm_X()
    print("DONE %.0fs" % (time.time() - t0), flush=True)

sys.exit(main())
