# wptserve HTTP/2: from `h2-worker-thread-lifecycle` to a landable series

## Context

`h2-worker-thread-lifecycle` has 47 commits on `origin/master`. Tests are added as xfail and fixed many commits later. Some code is added and then deleted within the branch. 18 tests are still xfail at HEAD. Many commit messages, comments and xfail reasons were model-written and some are wrong. Checking against RFC 9113/8441, the vendored h2 4.1.0 / hpack / hyperframe / pywebsocket3, and three audit agents found:
- wrong root causes;
- tests that can't pass, or that pass for the wrong reason;
- one test whose "fix" would break WebSockets over HTTP/2;
- about 25 further bugs already on `master`.

Most of these bugs come from structural problems, not one-off mistakes:
1. h2 and the socket are shared across threads, and connection-level frames are never flushed.
2. Request bodies go through an `os.pipe` that one thread both writes and reads.
3. Stream lifecycle is tracked only in wptserve, never in h2: streams are never reset.
4. HTTP/2 is forced into `BaseHTTPRequestHandler`.

**Goal:** small, independently reviewable PRs, each a red test followed by its fix. Coverage only grows. The steps fix the structure incrementally, without a big-bang rewrite. Asyncio comes later as its own series.

## Decisions so far

- **Keep the branch unchanged as the archive.** `h2-worker-thread-lifecycle` stays as it is and is the source for cherry-picks and tail tests. Build fresh stacked branches from `origin/master`; don't reorder the whole branch.
- **New base: `h2-server-not-http-server`.** Rebase it onto `origin/master`. It replaces PR 1's `close_connection` fixes.
  - **Why it failed CI before:** the Taskcluster run (`live_backing.log`, 28 Oct 2025 05:49Z) was of `7a97852365`. All 21 failures were `AttributeError: 'Http2WebTestRequestHandler' object has no attribute 'protocol_version'`, raised in `H2HandlerCopy.__init__`. The later fixup `4cca63cc5f` hard-codes `"HTTP/2.0"`, which is exactly that fix.
  - **Static check:** no other `http.server` attribute is used on the HTTP/2 path. `version_string`/`date_time_string` are only used by the HTTP/1 `ResponseWriter`.
  - **Still to do:** a full wptserve suite run on the rebased base, as part of Phase 0.
  - **The same log confirms the structural bugs:** each failing test hung about 30 s on an httpx `ReadTimeout`, because the worker died and its stream was orphaned (S2). Dead workers leaked pipe fds (`ResourceWarning: unclosed file`). pytest's thread-exception warning became the failure. S2a would turn hangs like these into fast, explicit failures.
- **PR 3 (WebSocket pump/pipe changes) is parked.** Step S3c replaces that design.
- **The tail is a holding area, not landed.** Each test moves up, corrected, when its fix exists.
- **`misc-h2-fixes` is retired.** Two of its commits are already upstream (`2835fc2170`, `308f4cfcce`). Its "don't turn an h2 error into a 500" idea goes into S2e.
- **`h2-asyncio` is a reference for a later series, not a base.**
- **`http-server-tls-log` is independent** (HTTP/1 only) and can be sent for review at any time.
- **Test peer:** an h2-based scripted peer over cleartext h2c replaces `H2RawClient` before PR 2. Hypothesis-based tests are wanted.
- **The routing asserts stay.** They are exhaustive for the vendored h2 4.1.0, checked event by event, so they act as tripwires on an h2 upgrade.
- **Markers:** keep `must`/`should`/`may`, but only on wire tests that check a specific RFC 2119 requirement.
- **PR shape:** a PR may contain multiple commits. Group the red/green pairs by theme into a few PRs rather than one PR per pair.
- **Python:** 3.9–3.15. `tools/tox.ini` currently stops at 3.14.
- **Priorities:** there are no field reports driving this; the issues came from code review and the asyncio attempt. So order by dependency and by cost, cheapest first. Since the hangs cost CI time, S1a and S2a come early.
- **Vendored h2 is in scope.** Choose between upgrading the vendored copy, fixing upstream, or forking (see "h2 itself" below).

## Overall order

### 0. Phase 0: check facts before cherry-picking

**Inputs:** an implementer needs only `origin/master`, the archive branch, and two commits from `h2-server-not-http-server` (`7a97852365`, `4cca63cc5f`). They don't need `h2-asyncio` (only relevant to §5), `misc-h2-fixes` (retired; its one useful idea is in S2e), or `http-server-tls-log` (independent).

Work in this worktree. Switch it to new branches; the `h2-worker-thread-lifecycle` ref doesn't move. Read the archive with `git show h2-worker-thread-lifecycle:<path>`. Rebase `h2-server-not-http-server` onto a new branch name instead of rewriting it. Install the newer h2 releases for the survey in a venv outside the repo.

Set up tox envs from `tools/` for the oldest and newest available interpreters (target range 3.9–3.15): `tox -e py3XX -- <paths>`. System `python3` lacks `hypothesis`.

Then:
1. **Rebase `h2-server-not-http-server`** (with its fixup) onto `origin/master`, and run the full `wptserve` suite on it. Expect it to pass now that `protocol_version` is fixed. If anything else fails, it's another `http.server` dependency on the HTTP/2 path.
2. **h2 survey.** Read the h2 changelog from 4.1.0 to the latest release, and test whether each of these is fixed in a newer release:
   - `TooManyStreamsError` raised as a connection error rather than a stream error;
   - `CLOSED` after receiving GOAWAY, rejecting the peer's DATA;
   - `:scheme`/`:path` required on a plain CONNECT (RFC 9113 §8.5).

   Also check new event types against the routing asserts, the minimum Python version against 3.9, and API changes. Decide between upgrading the vendored copy, fixing upstream (send PRs to python-hyper/h2 and vendor the release), or forking. Prefer upgrade, then upstream; fork only if upstream is unresponsive.
3. Confirm `test_handler_observes_data_in_same_batch_as_rst_stream` passes on `origin/master`. It's a regression guard, so it needs no red step.
4. Confirm `test_headers_after_goaway_closes_connection_without_handler` and both clean-EOF tests pass on `origin/master`.
5. For each cherry-picked fix, confirm its test fails without the fix and passes with it. A strict xfail with `raises=` must fail in the call phase, for the stated reason.

   Watch for this: `tools/pytest.ini` has `filterwarnings = error`, and pytest's unhandled-thread-exception warning then counts as the failure. So an xfail with no `raises=` passes even if its assertions would hold.

### 1. Base PR: `h2-server-not-http-server` + `handle()` tests

1. Red tests on a socketpair, with no client.
   - For each exit path, `handler.handle()` returns, never re-enters, and the socket is closed: reset during the initial `sendall`, `OSError` from `recv`, an unexpected exception, clean EOF.
   - On `master` the stdlib loop re-enters forever, so give the mocks a bounded `side_effect` list.
   - These replace the tests in `8213217f6a`/`7fc07b9fff`. Keep `217fcff6e8` (BrokenPipeError/ConnectionAbortedError) as xfail if it doesn't turn green.
2. Rebase `7a97852365` and its fixup `4cca63cc5f`. Expect small conflicts with `d3340200cb`/`a1bc9dea46`/`308f4cfcce`/`69ca0a1363`.
3. Explicit `break`s in the receive loop, from `85f7a16f8d`.
4. Split `finish_handling` so its HTTP/1-only tail (`close_connection`, `raw_input.read()`) goes to `Http1WebTestRequestHandler`. Document `response.close_connection` as unsupported on HTTP/2 for now.

### 2. PR 2: test peer + guard tests + small fixes from the branch

1. The peer, with thread naming (`h2-conn-<uid>`, `h2-stream-<uid>-<id>`).
2. Green guard tests on the peer over h2c:
   - DATA + RST in one batch;
   - HEADERS after GOAWAY;
   - clean EOF;
   - split HEADERS;
   - concurrent interleaved streams;
   - the server's first SETTINGS frame carries `ENABLE_CONNECT_PROTOCOL=1`;
   - WebSocket over HTTP/2: handshake, echo, server close, client close.
3. Cherry-picked fixes, each after its test:
   - `5dbaa9b314`: route by event type.
   - `0c1ac724fb` + `2a491decfa`: the assert, plus the functional test.
   - `0b3927f943`, with `343558f5d4` folded away: reap by liveness. Rewrite the message: on `master` the closed-stream sweep `del`s workers without signalling or joining them.
   - `b1c99a8c4b`: wake on the sentinel rather than polling. Must come after `0b3927f943`.
   - `9c8357f7c5` → `4cc5d081fb`: pipe fd cleanup.
   - `ecb7998ff0`: finalize on StreamEnded.
4. Commit messages: drop the reference to a nonexistent xfail test (`85f7a16f8d`), the stale hash `e378c813db`, and the "previous implementation" wording.

### 3. Local fixes (independent of the S-steps; one red/green pair each)

- **Header mapping in `H2Headers`:** a regular header named `path`/`method` overrides the pseudo-header, and duplicate headers collapse. h2 already joins cookie crumbs.
- **Non-ASCII response header values:** the H2 writer decodes them as UTF-8, but `ResponseHeaders` stores latin-1.
- **`H2Response.write_content`'s look-ahead** delays each chunk until the next arrives (`?pipe=trickle`, server-sent events).
- **`.asis` over HTTP/2** returns 500: there's no `write_raw_content`.
- **`frame_handler`** loses `base_path`/`url_base` and pipes, and skips `.sub.py`.
- **HEAD** goes through `end_stream` on an already-closed stream.
- **WebSocket handshake crashes:** `HandshakeException` with `status=None` causes a `TypeError`, and `VersionException` escapes.

The writer-related ones come after S4a if it touches the same code.

### 4. Structural steps (small; each 5–60 lines plus a red test)

Red tests come from the tail (corrected and rewritten on the peer) and from the audit.

- **S1: send what h2 queues**
  - **S1a.** Flush `data_to_send()` after `receive_data`, still under the guard, as the writers already do. Also flush before closing on `ProtocolError`.
    - Turns green: SETTINGS ACK (`51f16bfc68`), PING ACK (`51a65a0018`), GOAWAY on connection error (`c0744d87d2`), missing `:scheme` (`11439cbb61`, corrected to expect GOAWAY).
  - **S1b.** Route every send through one helper that runs under the lock. Fixes `write_raw_data_frame` sending without the lock, and raw HEADERS/CONTINUATION frames being written in separate lock acquisitions.
  - **S1c.** A per-connection writer thread. Bytes are enqueued under the lock, so order is preserved, and `sendall` happens outside it. A slow reader then no longer freezes the receive loop. *Needs a concurrency reviewer.*
- **S2: never orphan a stream**
  - **S2a.** In `_stream_thread`'s `finally`, if h2 still has the stream open, send RST(INTERNAL_ERROR) and flush.
    - Turns green: the `handle_data` exception test (`6af10bed30`), and 101 leaked streams killing the connection.
  - **S2b.** The same for the WebSocket worker, with CANCEL (RFC 8441 §5).
    - Turns green: the WebSocket handler exception (`8d53d149c8`) and `AbortedByUserException`.
  - **S2c.** Invalid `:protocol` gets a RST (or 4xx) on its stream instead of dropping the connection (`61df89549d`, corrected).
  - **S2d.** A client RST marks the stream cancelled, the writer raises, and the worker stops early. *Needs a concurrency reviewer.*
  - **S2e.** `FunctionHandler`/`finish_handling` stop turning h2 errors into 500s.
- **S3: request bodies**
  - **S3a.** `_stream_thread` buffers the body in a `SpooledTemporaryFile` instead of a pipe. Also fix the case-sensitive `content-length`/`host` lookups in `request.py`, and allow a body with no length (END_STREAM ends it).
    - Turns green: `2cdb0029e9`, `ab82d9a32c`, and the `request.url` host bug.
  - **S3b.** Return credit with `acknowledge_received_data(flow_controlled_length)` as each frame is buffered, then flush.
    - Turns green: `c08c228c31`, `5686d12536`.
  - **S3c.** WebSockets only: a blocking reader backed by a queue replaces the pipe and the pump thread, and credit is returned on read. pywebsocket3 needs no change, since it only uses `connection.read`/`write`. This makes PR 3 and the pipe unit tests obsolete.
    - Turns green: `5610673300`, `e5b4e9c299`.
    - *Needs a concurrency reviewer; gets a stateful test.*
- **S4: send-side flow control**
  - **S4a.** Size and send within one lock acquisition, and drop the `-9`. While the window is 0, wait on a condition variable that the receive loop signals on `WindowUpdated`, `RemoteSettingsChanged`, a reset or connection close.
    - Turns green: `fbe041bbb8` (corrected), the empty-DATA flood, truncated bodies, and the race between streams.
    - *Needs a concurrency reviewer; gets a stateful test.*
  - **S4b.** A fixed `recv` size instead of `initial_window_size`.
- **S5: limits and teardown**
  - **S5a.** Count live workers per connection and refuse new streams with REFUSED_STREAM beyond the advertised `SETTINGS_MAX_CONCURRENT_STREAMS`. Fixes Rapid Reset.
  - **S5b.** A bounded join on teardown, and `WebTestHttpd.stop()` fixes. Its startup race swallows `AttributeError`. Daemon connection threads aren't joined: confirmed in stdlib `socketserver._Threads.append`.
  - **S5c (optional).** A lazily growing, reused executor instead of a thread per stream.
    - **Not a small hard global cap.** WPT handlers coordinate (stash waits, long-poll), so a small cap can deadlock them. Bound per connection instead.
    - WebSocket sessions get their own allowance.
    - Add a test with two handlers that wait on each other at the limit.

### 5. Later series: asyncio

Re-measure first: after S1c and S5, the lock-contention motivation from `h2-asyncio` is mostly gone. If thread count still matters, move one piece at a time:
1. the writer;
2. the receive loop;
3. a single server-wide event loop for accept, which replaces `ThreadingHTTPServer` for HTTP/2.

Handlers stay in an executor. Extract an event-driven connection core only if and when that series needs it. `h2-asyncio` is a reference, not a rebase base: its loop-per-connection-thread design doesn't cut threads.

### Not landed

- **PR 3 commits:** `24aa949087`, `c70eaf471f`, `48ef3787c0`, `84dfa662e4`, `9cfc93c879`. Parked, and land only if S3c slips a long way. `34e3b14d8c`/`844b648514` are superseded.
- **The tail on the archive branch.** `125e3915ac` is wrong; drop it. The SETTINGS guard test in PR 2 replaces it.
- **h2 itself (now in scope).** h2 4.1.0 deviates from RFC 9113 in three places:
  - `TooManyStreamsError` is raised as a connection error rather than a stream error;
  - it goes `CLOSED` after receiving GOAWAY, rejecting the peer's DATA, so `938b068eb5` can't pass;
  - it requires `:scheme`/`:path` on a plain CONNECT.

  After the Phase 0 survey, either upgrade the vendored copy, land fixes upstream and vendor the release, or fork. Each fix gets a red test in the tail first. An h2 upgrade is its own PR: update the vendored copy, re-check the routing asserts against the new event list, and run the full suite on 3.9–3.15.

## Test strategy

**Rules**
1. **Coverage only grows.** Every removed test has a named replacement that lands earlier or in the same commit.
2. **Green wire-level guards come before a structural step.** Unit tests that poke internals each need a wire-level twin.
3. **Measure:** `--cov=wptserve --cov-report=term-missing` on `origin/master` versus each PR. HTTP/2 line coverage must not drop. Record the numbers in the PR description.

**The peer** (`tools/wptserve/tests/functional/`, replacing `h2_raw_client.py`)
- h2-based for well-formed traffic: correct HPACK, flow control and ACKs from the library, and an implicit check that the server's output is valid.
- A raw-send escape hatch for provoking error paths. Header blocks are encoded with `conn.encoder`, so HPACK stays in sync.
- Read results are typed: event, EOF, reset or timeout. The socket timeout is always restored.
- Control over how writes are split, for batching, partial frames and stalls.
- A frame tap only for testing the raw-frame API.
- The API is in HTTP terms: `request`, `expect_response`, `expect_reset`, `ping`.

**Transport:** h2c via `WebTestHttpd(http2=True, use_ssl=False)` for all wire tests; this works today with no server change. A small TLS+ALPN set covers ALPN, ragged EOF, record splitting, and the existing httpx tests.

**Determinism**
- Test handlers block on test-controlled events, never on sleeps.
- Thread naming allows leak checks by count. fd counts come from `/dev/fd` (macOS) or `/proc/self/fd` (Linux).
- A PING barrier works once S1a lands.
- A full settle hook (everything stopped and waiting) only once all blocking points are known primitives. Optional.

**Concurrency boundaries**
- `RuleBasedStateMachine` plus explicit pause points for the new boundary objects: the S3c WebSocket reader (push, read, EOF, abort, credit) and the S4a credit wait (send, window update, reset, close).
- Invariants: no missed wakeups, no stuck readers, no double-counted credit.

**Optional, after S4:** an independent RFC monitor run as a hypothesis state machine over h2c:
- safety rules from RFC 9113: stream states (§5.1), windows (§6.9), frame size (§4.2), SETTINGS/PING/GOAWAY;
- liveness checked at settle points.

It's new territory, so keep it small.

**Sources of red tests**
- WPT-in-browser HTTP/2 failures;
- the audit findings;
- the attack-class CVE checklist (CVE-2019-9511–9518, Rapid Reset, CONTINUATION flood), prioritised by "can this hang or flake CI".

h2spec may be run occasionally against h2c as a local smoke check, not in CI. It's RFC 7540-era and mostly exercises h2 itself.

**Coverage gaps to fill:**
- concurrent streams;
- a response larger than the peer's window;
- trailers;
- HEAD;
- inbound CONTINUATION;
- PRIORITY / WINDOW_UPDATE / RST for never-opened streams;
- an early response;
- a client RST in the middle of a response;
- `stop()` with a live connection;
- header mapping;
- WebSocket over HTTP/2 messages larger than 64 KiB;
- no fd growth when handlers raise.

## Making it reviewable without deep HTTP/2 knowledge

- **Commit message template:**
  1. what the RFC requires (quoted, with section);
  2. what h2 does for us;
  3. what wptserve gets wrong, and what a user sees;
  4. the fix, and why it's enough.

  No speculative prose.
- **Tests read as behaviour,** with short factual docstrings.
- **`tools/wptserve/docs/` gets a one-page HTTP/2 design note:** threads, what each lock protects, stream lifecycle, where credit is returned. Every structural step updates it.
- **Steps marked "concurrency reviewer"** (S1c, S2d, S3c, S4a, S5a/b) get a reviewer with threading experience, plus a pause-point or stateful test that shows the interleaving.
- **Each PR description is self-contained.** It links the RFC sections and lists which tail tests stay xfail.

## Corrections to carry when promoting tail tests (from the RFC/library checks and audits)

- **Missing `:scheme`.** h2 raises a connection-level `ProtocolError` (it validates the headers), so RST_STREAM is impossible. Accept RST or GOAWAY(PROTOCOL_ERROR), assert that no handler ran, and tag it `should`. RFC 9113 §5.4.1 lets an endpoint escalate a stream error to a connection error.
- **Connection-error GOAWAY.** A `connection` header is a malformed message, which is a stream error (§8.2.2 → §8.1.1). h2 escalates it. Fix the wording.
- **Invalid `:protocol`.** wptserve itself raises the `ProtocolError`, so RST or a 4xx is achievable. Accept either.
- **Small POST.** The real cause is the case-sensitive `Content-Length` lookup. The buffered pipe would block, not return empty.
- **INITIAL_WINDOW_SIZE=0.** The client must send WINDOW_UPDATE, or a correct server can't respond.
- **In-flight request after a client GOAWAY.** `last_stream_id` should be 0: it refers to peer-initiated streams. Blocked by h2's `CLOSED` state anyway.
- **WebSocket pump credit test.** Mock `handler.request`; otherwise a correct fix still xfails.
- **Thread-death xfails.** Use `raises=AssertionError` and handle thread exceptions explicitly. Move preconditions out of xfail tests.
- **Drop `strict=True` everywhere:** `xfail_strict = true` is global.
- **Markers.** `119be95d1d` tags wptserve-behaviour tests (404/500/503) as `must`. Audit them, and widen the marker descriptions to RFC 9110–9114, 7541, 9204 and 8441.
- **Remove history-dependent text** ("at this commit…", `line ~NNN`, `server.py:506`).

## Verification

- Every new branch passes `git rebase --keep-base origin/master -x '<tox h2 + test_handlers + test_response> && <tox flake8 wptserve>'`, on the oldest and newest available interpreters (target range 3.9–3.15). Strict xfail enforces red; removing the marker enforces green. Each PR also gets a full CI run on all versions.
- Use `--runxfail` on each red commit to confirm the failure is in the call phase, for the stated reason.
- `./wpt lint` on the touched files.
- The coverage report per PR shows no drop in HTTP/2 line coverage.
- The archive branch stays untouched, so it's always available as a reference.
