#!/usr/bin/env bash
# Full validation suite for Agentainer.
#
#   tests/validate.sh
#
# Mock agents only -- no model calls, no API keys, nothing to pay for. Every check
# exercises the real code paths (tmux, hooks, locks, queues, sessions).
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SW="$REPO/agentainer"
export PYTHONPATH="$REPO/lib"   # quoted heredocs cannot expand $REPO themselves
T="${TMPDIR:-/tmp}/agentswarm-validate.$$"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf "  \033[32mPASS\033[0m %s\n" "$1"; }
bad()  { FAIL=$((FAIL+1)); printf "  \033[31mFAIL\033[0m %s   %s\n" "$1" "$2"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected [$3] got [$2]"; fi; }

rm -rf "$T"; mkdir -p "$T"; cd "$T" || exit 1
trap 'rm -rf "$T"; tmux kill-server 2>/dev/null' EXIT
tmux kill-server 2>/dev/null

mkjson() { python3 -c "
import json,sys
print(json.dumps({'type':'assistant','isSidechain':False,'message':{'content':[{'type':'text','text':sys.argv[1]}]}}))" "$1"; }
turn() { ( cd "$T/$3/$1" && echo "{\"transcript_path\":\"$2\"}" | SWARM_CONFIG="$T/$4" SWARM_AGENT=$1 "$SW" hook claude 2>&1 ); }

# ---------------------------------------------------------------- config layer
echo "== config =="
cat > bad1.yaml <<'Y'
agents:
  - name: a
    type: nosuchtype
Y
"$SW" validate -c bad1.yaml >/dev/null 2>&1; check "unknown type rejected" "$?" "1"

cat > bad2.yaml <<'Y'
agents:
  - {name: a, type: claude, can_talk_to: [ghost]}
Y
"$SW" validate -c bad2.yaml >/dev/null 2>&1; check "unknown peer rejected" "$?" "1"

cat > bad3.yaml <<'Y'
agents:
  - {name: a, type: claude, can_talk_to: [], forward_responses_to: [b]}
  - {name: b, type: claude}
Y
"$SW" validate -c bad3.yaml >/dev/null 2>&1; check "forward not subset of can_talk_to rejected" "$?" "1"

cat > bad4.yaml <<'Y'
agents:
  - {name: a, type: claude, capture: none, forward_responses_to: [b], can_talk_to: [b]}
  - {name: b, type: claude}
Y
"$SW" validate -c bad4.yaml >/dev/null 2>&1; check "forward with capture:none rejected" "$?" "1"

cat > bad5.yaml <<'Y'
swarm: {message_format: sideways}
agents: [{name: a, type: claude}]
Y
"$SW" validate -c bad5.yaml >/dev/null 2>&1; check "bad message_format rejected" "$?" "1"

cat > bad6.yaml <<'Y'
templates: {comms: "hello {nonsense}"}
agents: [{name: a, type: claude}]
Y
out=$("$SW" validate -c bad6.yaml 2>&1); echo "$out" | grep -q "placeholder is not recognised"
check "bad template placeholder -> ConfigError" "$?" "0"

cat > wd.yaml <<'Y'
swarm: {root: ./wsx, create_workdirs: false}
agents: [{name: a, type: claude, workdir: ./missing}]
Y
"$SW" validate -c wd.yaml >/dev/null 2>&1; check "create_workdir:false + missing dir rejected" "$?" "1"

cat > star.yaml <<'Y'
agents:
  - {name: a, type: claude, can_talk_to: "*"}
  - {name: b, type: claude}
  - {name: c, type: claude}
Y
peers=$(python3 -c "
import sys; sys.path.insert(0,'$REPO/lib'); import config
print(','.join(config.load('$T/star.yaml').get('a').can_talk_to))")
check "can_talk_to '*' expands" "$peers" "b,c"

# ------------------------------------------------------------------ yaml layer
echo "== yaml =="
python3 - <<'PY'
import sys, glob; sys.path.insert(0,'$REPO/lib')
import yaml, minyaml
bad = [f for f in ['$REPO/agents.example.yaml'] + sorted(glob.glob('$REPO/examples/*.yaml'))
       if yaml.safe_load(open(f).read()) != minyaml.load(open(f).read())]
print("PARITY_OK" if not bad else "PARITY_BAD " + str(bad))
PY
[ "$(python3 -c "
import sys, glob; sys.path.insert(0,'$REPO/lib')
import yaml, minyaml
bad=[f for f in ['$REPO/agents.example.yaml']+sorted(glob.glob('$REPO/examples/*.yaml')) if yaml.safe_load(open(f).read())!=minyaml.load(open(f).read())]
print(len(bad))")" = "0" ] && ok "minyaml == pyyaml on every shipped config" || bad "parser parity" ""

python3 -c "
import sys; sys.path.insert(0,'$REPO/lib')
import swarm, yaml
d = swarm.yaml_dump({'a':'q\" \\\\ b','b':None,'c':{'x':1},'d':{}})
assert yaml.safe_load(d) == {'a':'q\" \\\\ b','b':None,'c':{'x':1},'d':{}}, yaml.safe_load(d)
" && ok "yaml emitter round-trips (quotes, backslash, null, empty)" || bad "yaml emitter" ""

# ------------------------------------------------------------- unit: transcript
echo "== transcript extraction =="
cat > two.jsonl <<'J'
{"type":"user","isSidechain":false,"message":{"content":"one"}}
{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"text","text":"TURN1"}]}}
{"type":"user","isSidechain":false,"message":{"content":"two"}}
{"type":"assistant","isSidechain":true,"message":{"content":[{"type":"text","text":"SUBAGENT"}]}}
J
r=$(python3 -c "
import sys; sys.path.insert(0,'$REPO/lib'); import swarm
print(repr(swarm.read_transcript_reply('$T/two.jsonl')))")
check "unflushed turn returns empty, not stale turn 1" "$r" "''"
echo '{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"text","text":"TURN2"}]}}' >> two.jsonl
r=$(python3 -c "
import sys; sys.path.insert(0,'$REPO/lib'); import swarm
print(swarm.read_transcript_reply('$T/two.jsonl'))")
check "sidechain skipped, latest turn returned" "$r" "TURN2"

# ----------------------------------------------------------------- unit: tags
echo "== tag parser =="
python3 - <<'PY'
import sys; sys.path.insert(0,'$REPO/lib'); import swarm
m,r,p = swarm.parse_outbound('x<swarm-send to="a">hi</swarm-send>y')
assert len(m)==1 and m[0].to=="a" and m[0].expects_reply and r=="xy", (m,r)
m,r,p = swarm.parse_outbound('<swarm-broadcast>yo</swarm-broadcast>')
assert m[0].kind=="broadcast" and not m[0].expects_reply
m,r,p = swarm.parse_outbound('<swarm-send to="a" expects-reply="false">fyi</swarm-send>')
assert not m[0].expects_reply
m,r,p = swarm.parse_outbound('<swarm-send to="a">unclosed')
assert not m and p, p
m,r,p = swarm.parse_outbound('<swarm-send to="a"></swarm-send>')
assert not m and p
m,r,p = swarm.parse_outbound("<swarm-send to='a' reply-to='m-1'>b</swarm-send>")
assert m[0].reply_to=="m-1"
# prose that merely names the tag must NOT be read as a botched send (no nudge)
for prose in ("I'll use <swarm-send> blocks to answer.",
              "Use \`<swarm-send>\` blocks.",
              "I will <swarm-broadcast> the result later."):
    m,r,p = swarm.parse_outbound(prose)
    assert not m and not p, ("prose flagged as send:", prose, p)
# a real block opened and never closed MUST still be flagged
m,r,p = swarm.parse_outbound('<swarm-send to="a">body with no closing tag')
assert not m and p, p
print("OK")
PY
[ $? -eq 0 ] && ok "parse_outbound: send/broadcast/expects-reply/unclosed/empty/quotes" || bad "parse_outbound" ""

# --------------------------------------------------------------- runtime setup
echo "== runtime =="
cat > s.yaml <<'Y'
swarm: {name: vt, root: ./ws, session_prefix: "vt-"}
defaults: {type: claude, boot_delay_ms: 200, ready_probe: false, append_agents_that_you_can_talk_to_prompt: false}
agents:
  - {name: A, command: "cat >> received.txt", can_talk_to: [B]}
  - {name: B, command: "cat >> received.txt", can_talk_to: [A]}
  - {name: C, command: "cat >> received.txt", can_talk_to: [], capture: none}
Y
"$SW" up -c s.yaml --no-prompt >/dev/null 2>&1; sleep 1
n=$(tmux ls 2>/dev/null | grep -c "^vt-")
check "three tmux sessions started" "$n" "3"

# ACL
SWARM_AGENT=C "$SW" send -c s.yaml --to A "x" >/dev/null 2>&1
check "ACL denies C -> A" "$?" "1"
SWARM_AGENT=A "$SW" send -c s.yaml --to B "hello" >/dev/null 2>&1
check "ACL allows A -> B" "$?" "0"

# tagged envelope shape
grep -q '<swarm-message from="A" to="B" id="m-' ws/B/received.txt
check "inbound envelope has from/to/id" "$?" "0"

# busy backpressure (B is mid-turn after that message)
SWARM_AGENT=A "$SW" send -c s.yaml --to B "again" >/dev/null 2>&1
check "busy agent refuses a second message" "$?" "1"
SWARM_AGENT=A "$SW" send -c s.yaml --to B --queue "queued one" >/dev/null 2>&1
check "--queue accepted while busy" "$?" "0"
d=$("$SW" queue -c s.yaml B 2>/dev/null | head -1 | grep -o '1 message')
check "queue depth 1" "$d" "1 message"

# C has capture:none -> untracked, always accepts
"$SW" send -c s.yaml --to C "one" >/dev/null 2>&1 && "$SW" send -c s.yaml --to C "two" >/dev/null 2>&1
check "capture:none agent is never 'busy'" "$?" "0"

# turn end drains the queue
mkjson 'done' > done.jsonl
turn B "$T/done.jsonl" ws s.yaml >/dev/null 2>&1
sleep 1
grep -q "queued one" ws/B/received.txt
check "queued message drains on turn end" "$?" "0"
q=$("$SW" queue -c s.yaml B 2>/dev/null | grep -o '0 message')
check "queue empty after drain" "$q" "0 message"

# force idle (--no-drain, since draining would immediately make it busy again)
"$SW" idle -c s.yaml B --no-drain >/dev/null 2>&1
"$SW" status -c s.yaml 2>/dev/null | grep -E "^B " | grep -q idle
check "swarm idle forces an agent idle" "$?" "0"

# concurrency: 5 parallel senders, exactly one wins
"$SW" idle -c s.yaml B >/dev/null 2>&1
before=$(grep -c "swarm-message" ws/B/received.txt)
for i in 1 2 3 4 5; do SWARM_AGENT=A "$SW" send -c s.yaml --to B "race$i" >/dev/null 2>&1 & done
wait; sleep 1
after=$(grep -c 'from="A"' ws/B/received.txt)
delivered=$(grep -c "race" ws/B/received.txt)
check "TOCTOU: exactly one of 5 concurrent sends delivered" "$delivered" "1"
"$SW" down -c s.yaml >/dev/null 2>&1

# ----------------------------------------------------- tags: routing + ACL + busy
echo "== tag routing =="
rm -rf ws; "$SW" up -c s.yaml --no-prompt >/dev/null 2>&1; sleep 1
mkjson '<swarm-send to="B">PING</swarm-send>' > ping.jsonl
turn A "$T/ping.jsonl" ws s.yaml >/dev/null 2>&1
grep -q "PING" ws/B/received.txt; check "outbound tag routed A -> B" "$?" "0"
test -f ws/.swarm/run/B.pending.json; check "opening message creates a reply obligation" "$?" "0"

mkjson '<swarm-send to="A">sneak</swarm-send>' > sneak.jsonl
turn C "$T/sneak.jsonl" ws s.yaml >/dev/null 2>&1
grep -q "sneak" ws/A/received.txt; check "capture:none agent's tags are not routed" "$?" "1"

id=$(python3 -c "
import json
print([json.loads(l)['id'] for l in open('$T/ws/.swarm/logs/B.jsonl') if json.loads(l)['kind']=='received'][-1])")
mkjson "<swarm-send to=\"A\" reply-to=\"$id\">PONG</swarm-send>" > pong.jsonl
turn B "$T/pong.jsonl" ws s.yaml >/dev/null 2>&1
test -f ws/.swarm/run/B.pending.json; check "answering clears the obligation" "$?" "1"
test -f ws/.swarm/run/A.pending.json; check "a reply-to message creates no new obligation" "$?" "1"

# reminder: silent turn
"$SW" idle -c s.yaml A >/dev/null 2>&1; "$SW" idle -c s.yaml B >/dev/null 2>&1
mkjson '<swarm-send to="B">Q2</swarm-send>' > q2.jsonl
turn A "$T/q2.jsonl" ws s.yaml >/dev/null 2>&1
mkjson 'prose only, no tag' > prose.jsonl
turn B "$T/prose.jsonl" ws s.yaml >/dev/null 2>&1
n=$(ls ws/.swarm/inbox/B/*from-swarm* 2>/dev/null | wc -l)
check "silent turn triggers exactly one reminder" "$n" "1"
turn B "$T/prose.jsonl" ws s.yaml >/dev/null 2>&1
turn B "$T/prose.jsonl" ws s.yaml >/dev/null 2>&1
n=$(ls ws/.swarm/inbox/B/*from-swarm* 2>/dev/null | wc -l)
check "gives up after max_reply_reminders" "$n" "1"

# malformed tag -> send_failed
"$SW" idle -c s.yaml A >/dev/null 2>&1
mkjson '<swarm-send to="ghost">x</swarm-send>' > ghost.jsonl
turn A "$T/ghost.jsonl" ws s.yaml >/dev/null 2>&1
sleep 1
grep -ql "not an agent" ws/.swarm/inbox/A/*from-swarm*.md 2>/dev/null
check "unknown recipient -> syntax correction" "$?" "0"
grep -ql "waiting on your answer" ws/.swarm/inbox/A/*from-swarm*.md 2>/dev/null
check "send_failed does NOT claim someone is waiting" "$?" "1"
"$SW" down -c s.yaml >/dev/null 2>&1

# ---------------------------------------------- regressions (bugs found in review)
echo "== regressions =="
# 1. queued mail must be delivered before a nudge; a nudge would mark the agent busy
#    and strand the queue.
rm -rf ws; "$SW" up -c s.yaml --no-prompt >/dev/null 2>&1; sleep 1
SWARM_AGENT=A "$SW" send -c s.yaml --to B "opening question" >/dev/null 2>&1   # B owes a reply, B busy
SWARM_AGENT=A "$SW" send -c s.yaml --to B --queue "queued mail" >/dev/null 2>&1
mkjson 'prose, no tag' > silent.jsonl
turn B "$T/silent.jsonl" ws s.yaml >/dev/null 2>&1
sleep 1
grep -q "queued mail" ws/B/received.txt
check "queued mail beats the reminder" "$?" "0"
n=$(ls ws/.swarm/inbox/B/*from-swarm* 2>/dev/null | wc -l)
check "no reminder while mail was waiting" "$n" "0"
"$SW" down -c s.yaml >/dev/null 2>&1

# 2. a send_failed nudge stores from=None; the next silent turn must not claim
#    "someone is waiting" (only visible when max_reply_reminders > 1)
cat > two_rem.yaml <<'Y'
swarm: {name: tr, root: ./tw, session_prefix: "tw-", max_reply_reminders: 2}
defaults: {type: claude, boot_delay_ms: 200, ready_probe: false, append_agents_that_you_can_talk_to_prompt: false}
agents:
  - {name: A, command: "cat >> received.txt", can_talk_to: [B]}
  - {name: B, command: "cat >> received.txt", can_talk_to: [A]}
Y
rm -rf tw; "$SW" up -c two_rem.yaml --no-prompt >/dev/null 2>&1; sleep 1
mkjson '<swarm-send to="ghost">x</swarm-send>' > ghost2.jsonl
turn A "$T/ghost2.jsonl" tw two_rem.yaml >/dev/null 2>&1   # nudge 1: send_failed, pending has no sender
"$SW" idle -c two_rem.yaml A --no-drain >/dev/null 2>&1
turn A "$T/ghost2.jsonl" tw two_rem.yaml >/dev/null 2>&1   # nudge 2: still malformed
last=$(ls -t tw/.swarm/inbox/A/*from-swarm*.md 2>/dev/null | head -1)
n=$(ls tw/.swarm/inbox/A/*from-swarm* 2>/dev/null | wc -l)
check "second nudge sent (max_reply_reminders=2)" "$n" "2"
grep -q "waiting on your answer" "$last" 2>/dev/null
check "no phantom 'someone is waiting' when nobody asked" "$?" "1"

"$SW" idle -c two_rem.yaml A --no-drain >/dev/null 2>&1
turn A "$T/ghost2.jsonl" tw two_rem.yaml >/dev/null 2>&1   # third: must give up
n=$(ls tw/.swarm/inbox/A/*from-swarm* 2>/dev/null | wc -l)
check "gives up after the last nudge" "$n" "2"

# a quiet turn owing nothing, with nothing broken, is never nudged
"$SW" idle -c two_rem.yaml A --no-drain >/dev/null 2>&1
mkjson 'quiet, nothing to send' > quiet.jsonl
turn A "$T/quiet.jsonl" tw two_rem.yaml >/dev/null 2>&1
n=$(ls tw/.swarm/inbox/A/*from-swarm* 2>/dev/null | wc -l)
check "a quiet turn owing nothing is never nudged" "$n" "2"
"$SW" down -c two_rem.yaml >/dev/null 2>&1

# 3. a queue stranded on an agent whose capture never fires must self-heal: any
#    other agent's turn end sweeps it once it is stale-busy past busy_timeout_ms.
#    (This is the "one agent sent several messages, one recipient wedged" case.)
rm -rf ws; "$SW" up -c s.yaml --no-prompt >/dev/null 2>&1; sleep 1
python3 - <<PY
import sys, time; sys.path.insert(0,'$REPO/lib')
import config, swarm
cfg = config.load('$T/s.yaml')
# B looks busy but its turn started ages ago (its hook never fired) and a message
# sits queued for it. On its own B would stay wedged forever.
swarm.write_turn_state(cfg, "B", {"delivered":1,"completed":0,"since":time.time()-99999,"by":"A"})
swarm.enqueue(cfg, "A", "B", "stranded task", hops=0)
PY
mkjson 'A done' > adone.jsonl
turn A "$T/adone.jsonl" ws s.yaml >/dev/null 2>&1   # unrelated turn end triggers the sweep
sleep 1
grep -q "stranded task" ws/B/received.txt
check "stale-busy agent's stranded queue self-heals on another turn end" "$?" "0"
q=$("$SW" queue -c s.yaml B 2>/dev/null | head -1 | grep -o '0 message')
check "swept queue is emptied" "$q" "0 message"
"$SW" down -c s.yaml >/dev/null 2>&1

# ------------------------------------------------------------------- sessions
echo "== sessions / resume =="
rm -rf ws; "$SW" up -c s.yaml --no-prompt >/dev/null 2>&1; sleep 1
( cd ws/A && echo '{"session_id":"sess-aaa","transcript_path":"'$T'/done.jsonl"}' | SWARM_CONFIG="$T/s.yaml" SWARM_AGENT=A "$SW" hook claude >/dev/null 2>&1 )
grep -q 'session_id: "sess-aaa"' ws/.swarm/sessions.yaml
check "claude session id recorded" "$?" "0"
python3 -c "
import sys; sys.path.insert(0,'$REPO/lib')
import yaml, minyaml
t=open('$T/ws/.swarm/sessions.yaml').read()
assert yaml.safe_load(t)==minyaml.load(t)"
check "sessions.yaml parses with both parsers" "$?" "0"

cmd=$(python3 -c "
import sys; sys.path.insert(0,'$REPO/lib')
import config, swarm
c=config.load('$T/s.yaml')
print(swarm.resume_command(c, c.get('A'), 'sess-aaa'))")
check "claude resume command" "$cmd" "cat >> received.txt --resume sess-aaa"

"$SW" down -c s.yaml >/dev/null 2>&1
"$SW" up -c s.yaml --resume --no-prompt 2>&1 | grep -q "resuming conversation sess-aaa"
check "up --resume reattaches" "$?" "0"
"$SW" down -c s.yaml >/dev/null 2>&1
"$SW" up -c s.yaml --no-prompt >/dev/null 2>&1
python3 -c "
import sys; sys.path.insert(0,'$REPO/lib'); import config, swarm
c=config.load('$T/s.yaml')
assert 'A' not in swarm.read_sessions(c), 'stale entry survived a fresh start'"
check "fresh start clears the stale session id" "$?" "0"
"$SW" down -c s.yaml >/dev/null 2>&1

# ---------------------------------------------------------------- pane watcher
echo "== pane capture (gemini/hermes path) =="
cat > pane.yaml <<'Y'
swarm: {name: pn, root: ./pw, session_prefix: "pw-", pane_idle_ms: 800, pane_poll_ms: 250}
agent_types:
  echoer: {command: "bash -c 'while IFS= read -r l; do echo \"REPLY\"; done'", capture: pane, boot_delay_ms: 300}
  sink:   {command: "cat >> received.txt", capture: none, boot_delay_ms: 300}
agents:
  - {name: talker, type: echoer, can_talk_to: [peer], forward_responses_to: [peer], append_agents_that_you_can_talk_to_prompt: false, ready_probe: false}
  - {name: peer,   type: sink, can_talk_to: [], append_agents_that_you_can_talk_to_prompt: false}
Y
"$SW" up -c pane.yaml --no-prompt >/dev/null 2>&1; sleep 1
"$SW" send -c pane.yaml --to talker "go" >/dev/null 2>&1
for _ in $(seq 15); do grep -q "talker" pw/peer/received.txt 2>/dev/null && break; sleep 1; done
grep -q "talker" pw/peer/received.txt 2>/dev/null
check "pane watcher captures and forwards" "$?" "0"
grep -q "swarm-message from=\"user\"" pw/peer/received.txt 2>/dev/null
check "pane watcher does not relay its own inbox back" "$?" "1"
"$SW" down -c pane.yaml >/dev/null 2>&1

# -------------------------------------------------------------- hook discovery
echo "== hook discovery =="
"$SW" up -c s.yaml --no-prompt >/dev/null 2>&1; sleep 1
out=$( cd ws/A && echo '{"transcript_path":"'$T'/done.jsonl"}' | SWARM_CONFIG="$REPO/agents.example.yaml" "$SW" hook claude 2>&1; echo "rc=$?" )
echo "$out" | grep -q "rc=0"
check "hook ignores a wrong SWARM_CONFIG and finds itself from cwd" "$?" "0"
"$SW" down -c s.yaml >/dev/null 2>&1
tmux kill-server 2>/dev/null

echo
echo "=================================================="
printf "  passed: %s   failed: %s\n" "$PASS" "$FAIL"
echo "=================================================="
[ "$FAIL" -eq 0 ]
