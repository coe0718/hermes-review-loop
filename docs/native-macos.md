# Native macOS experiment

This branch starts issue #256's native alternative with a standalone Seatbelt
profile builder, a native staged-Hermes launcher and executable boundary probes. It does **not** enable native
production Hermes turns. `contained.py`, setup, doctor and the manifest retain
the existing Linux requirement.

The host generates a default-deny profile, passes filesystem paths as parameters,
and launches through `/usr/bin/sandbox-exec`. The profile permits narrow runtime
and disposable working directories, system executable/library reads, and exact
per-turn Unix socket connections. It grants no TCP/UDP, socket binding, general
Mach-service lookup, Apple Events or Launch Services authority. macOS system
paths are canonicalized before use. The host chooses these paths; this builder
must never accept a PR-supplied access list.

## First acceptance gate

The dedicated workflow runs on pinned macOS 15 Apple Silicon and Intel images.
The initial 11 profile/boundary tests passed on both architectures in
[run 38066847221](https://github.com/coe0718/diaktoros/actions/runs/38066847221).
`DIAKTOROS_REQUIRE_SEATBELT=1` makes missing macOS prerequisites a failure rather
than a skipped success. No GitHub/model credentials or live services are used.

```sh
python tests/leakguard.py discover -v -s tests -p 'test_seatbelt.py'
```

The probes exercise Python startup, a scrubbed child environment, writable
scratch, read-only source, denied host-secret access, symlink escapes, descendant
inheritance, TCP/UDP denial, exact socket access and socket replacement denial.
Linux runs validate profile construction and fail-closed launch behavior; the
native probes skip there. Linux results do not prove macOS containment.

## Native Hermes vertical

The additional `hermes-turn` job installs the same pinned Hermes commit used by
Linux CI into a disposable native venv. It stages committed source for its
fixture and starts the real Hermes CLI under Seatbelt. A model fixture requests
a terminal command that verifies host-secret/network denial and submits a review
to a real scoped broker backed by fake GitHub responses. The test verifies model
authentication stays host-side and exactly one scoped fake review is posted.
The Python-only Hermes fixture passed on both architectures in
[run 38068669256](https://github.com/coe0718/diaktoros/actions/runs/38068669256).

The next fixture also runs `cargo test --offline --locked --lib` using Rust
1.85.1 and a dedicated read-only vendor snapshot of memchr 2.7.4. A generated
Cargo build script verifies host-secret/network denial and vendor-write denial;
the test verifies an actual dependency rlib and a passing Rust unit test. The
host resolves the selected Apple SDK and compiler toolchain before launch.
Only those SDK/toolchain roots are granted, not all of Xcode, `/Applications`,
Homebrew or the operator's Cargo profile. Cargo gets a fresh scratch home and
direct compiler/linker paths; dependency downloads happen before containment.

`seatbelt_wire.py` supplies an OpenAI client with an httpx Unix-domain transport
through Hermes's provider hook. There is no localhost listener or TCP allowance;
only Chat Completions is supported in this first native vertical. Host inference
capability quotas, byte limits, upstream selection and credential handling remain
in force. `broker_client.py` accepts portable path settings from the rebuilt
child environment; these do not change broker authority.

`native_macos.run` remains experimental and is not selected by production code.
It uses shared bounded output/time capture through an independent native
watchdog and requires a host-created fixed-capacity workspace. The watchdog
cleans up the original process group after supervisor death, timeout, output
limit or normal leader exit. Detached descendants remain outside that guarantee.
The vertical uses the existing committed source exporter: it reads a pinned
commit, filters credential-shaped data, bounds the export and checks each blob's
hash. Native turn layout and production integration are still experimental.
The Rust fixture covers a small pure-Rust vendored dependency and Apple's
linker/SDK, not arbitrary workspaces, C/C++ dependencies or durable production
review receipts. Do not interpret this as completed native support.

## Pinned source export on macOS

The host opens the repository directory without following a final symlink.
Linux Git continues to use its existing `/proc/self/fd` directory pin. On macOS,
an isolated Python helper inherits only the repository descriptor, calls
`fchdir`, closes that descriptor and replaces itself with `/usr/bin/git`.
The host never changes its working directory or uses a `preexec_fn` in a
multithreaded process. Renaming the repository and replacing its original path
with another repository cannot redirect this export.

The committed-tree parser, export limits, credential filters, blob hash checks
and descriptor-relative destination writes are shared unchanged. A failed
directory pin has no pathname fallback. The native Hermes fixture and Mac CI
exercise this path; the focused helper tests also run on Linux. The source must
still be a trusted credential-free checkout: code and templates are exported
byte for byte, so this filter cannot identify every committed secret.

## Turn-visible path layout

`turn_layout.TurnLayout` describes the paths visible inside one turn. Both
launchers generate the Hermes entry arguments from this layout. The Linux
layout retains its existing mount names and command arguments; the native
layout uses the validated host-created workspace and staged read roots.
Native environment values for home, scratch, Cargo and broker-client files
come from the same layout.

Host-generated role instructions render broker command examples against the
layout's work and scratch paths, with shell quoting for spaces and quotes.
Only fixed host instruction text is rendered; PR and model text is not rewritten.
Relative paths, control characters, backticks and path-list delimiters are
refused. The layout describes paths and supplies no containment or authority.
The native reviewer fixture includes these instructions and verifies that the
real model request contains them. Automatic backend selection still requires
integration and acceptance testing.

## Role-specific native checkout permissions

The native launcher requires an explicit host-selected role from the broker
scope. Adjudicator and triage turns receive read-only work roots; only home and
scratch are writable. Reviewer, fixer and issue-fixer turns retain writable
work roots. Unknown roles fail closed without a writable fallback. The broker
continues to authorize each operation independently of filesystem permissions.

Mac probes verify read access, denied create/overwrite/chmod/rename/unlink for
judging roles, writable home/scratch, descendant write denial and denied access
to host files through a staged symlink. Writing roles must perform the same
checkout mutations successfully while still denying the symlink escape.
These are filesystem-policy probes; full adjudicator/triage/fixer/issue-fixer
Hermes turns, durable receipts and full supervisor integration remain gates.

## Bounded native storage

`native_storage.Workspace` creates a fixed-size UDRW image containing a
case-sensitive APFS filesystem. The host attaches it at a private mountpoint and
verifies image identity, mounted filesystem identity, capacity and case-sensitive
names before launch. All three writable roots (`home`, `work`, `scratch`) share
that filesystem allocation budget. A missing/incorrect mount fails closed.
The backing image, disk devices and capability sockets are outside the child's
filesystem authority; the child cannot resize the image through file access.

The dedicated storage probes fill a small volume until the kernel returns
`ENOSPC`, verify the shared limit across all writable roots, denied image/symlink
writes and unchanged backing-image size, and exercise cleanup after an exception.
The real Hermes/Rust fixture uses a separate 512 MiB volume. The bound covers
allocated filesystem blocks, not sparse-file logical lengths or total process
memory. Production storage sizing still needs representative workspace tests.

Cleanup rediscovers attached devices by the exact private image, detaches, and
verifies removal before deleting the backing files. Busy ejects receive at most
three attempts with 0.25/0.5-second backoffs; image ownership is checked again
before every normal or forced eject. A failed eject can already have unmounted
the filesystem, so CI also records remaining exact-image attachments. This is
a bounded retry, not proof that the intermittent busy-eject cause is resolved.
If detach/verification fails,
it retains the private image directory and raises an error for host recovery.
This is not cleanup after host death: startup reconciliation, detached descendant
termination and lifecycle-safe disposal remain production gates.

## Lifecycle characterization

`tests/test_native_lifecycle.py` deliberately measures two unresolved gaps in
`contained.capture`: a child that calls `setsid()` can survive process-group
cleanup after timeout, and a child can survive `SIGKILL` of its host supervisor.
The probes demand fresh filesystem activity after failure and verify that the
survivor still cannot read a host secret. A green characterization test confirms
these limitations; it is **not** production lifecycle acceptance.

The fixtures are cooperative and time-bounded. Before triggering failure, the
host registers `kqueue` process-exit notifications and waits for actual exit
before disposing of fixture paths. No general process-tree polling/killing
mechanism is introduced. The unmanaged probes remain as a baseline; guarded
probes now verify cleanup after supervisor SIGKILL and normal leader exit,
watchdog signal denial, private-descriptor noninheritance and bounded capture. Production needs an enforceable descendant ownership
mechanism and independent host-death recovery, including capability revocation
and safe handling of attached storage. Apple launchd's process-group cleanup
alone does not establish ownership of a descendant that changes its group.

## Independent native watchdog

`native_lifecycle.capture` launches a trusted helper outside the child's Seatbelt
profile, in a separate session with a scrubbed environment. An anonymous pipe
has a writer in the supervisor and a reader in the watchdog; either EOF or an
explicit abort byte requests cleanup. The abort byte works even if a forked host
child retains a writer while the supervisor stays alive. The watchdog also
registers a kernel exit notification for its actual
supervisor, so a forked host child retaining a writer cannot delay cleanup after
supervisor death. Parent identity is checked around registration, before any
sandboxed work starts. Configuration and result pipes
are also host-only. None of these descriptors reach the sandboxed executable.

The watchdog uses kernel notifications for its known direct child, kills the
original process group before reaping its leader, and reports completion over a
private result pipe. Keeping the leader unreaped until cleanup reserves its PID;
the helper does not signal a cached group ID after reaping and possible PID reuse.
The shared capture implementation delegates abort to this lifeline rather than
killing the independent watchdog. Missing/invalid completion or a watchdog that
cannot finish cleanup raises `CleanupIncomplete`; the native launcher retains
its workspace without attempting detach or deletion.

This closes only the original-group supervisor-death gap. A child that changes
its process group can still survive. Watchdog death, machine death, capability
revocation and startup recovery remain unresolved. Production still requires an
enforceable ownership mechanism for every descendant; a polling tree scan or
`kqueue` monitoring of selected PIDs is not equivalent.

## Remaining work before production support

1. Integrate backend selection and the shared turn layout with full production
   orchestration, role-specific write roots, prompts, review context and receipts.
   Source export already has a native directory pin; production runtime/source
   generation ownership still needs integration with the per-turn layout.
2. Replace the fixed localhost inference bridge. Prefer direct Unix-socket HTTP
   transports where Hermes provider clients permit them. Any TCP alternative
   needs exclusive per-turn ports, authentication and exact endpoint permissions.
3. Extend native Rust/SDK/offline dependency coverage to representative workspaces
   and native build dependencies against local model and broker fixtures. Do not allow all of Homebrew,
   the user's home or `/Library` to solve missing-runtime failures.
4. Validate bounded-volume sizing with representative workspaces and integrate
   crash recovery/reconciliation. Seatbelt alone supplies no sized tmpfs or disk
   quota; a directory-size watcher is not an equivalent bound.
5. Design and test supervisor death and detached descendant cleanup. Process
   groups and profile inheritance alone do not reproduce bubblewrap's PID
   namespace and parent-death semantics.
6. Extend doctor/selftest and verify full install/review operation with a Mac
   tester. Keep production support experimental until the acceptance gate passes.

`sandbox-exec` is deprecated. Its presence and effective restrictions must be
tested on every supported OS rather than inferred from the OS name. This
experiment is not App Sandbox packaging or an Apple-supported stable SBPL API.

References: [Codex's Seatbelt implementation](https://github.com/openai/codex/tree/main/codex-rs/sandboxing/src),
[Anthropic's macOS sandbox implementation](https://github.com/anthropics/sandbox-runtime/blob/main/src/sandbox/macos-sandbox-utils.ts).
