// msgsnap — snapshot the Messages database, and do nothing else.
//
// macOS has no TCC scope for ~/Library/Messages. The narrow "Files and Folders"
// grants only cover Desktop, Documents, Downloads and removable/network volumes;
// Messages sits in the Full Disk Access set alongside ~/Library/Mail and the TCC
// databases themselves. So reading chat.db costs FDA or it costs nothing.
//
// The point of a separate binary is not a narrower permission — none exists — it
// is a narrower holder. FDA granted to Terminal is inherited by everything the
// terminal spawns. FDA granted to this binary stops here.
//
// That is only true if the binary cannot be talked into reading anything else,
// so the source path is compiled in and there is no flag to change it. Accepting
// a path argument would turn an FDA holder into an arbitrary-file-read oracle for
// any process that can exec it.
//
// The copy goes through SQLite's online backup API rather than cp(1): chat.db is
// in WAL mode, and copying the .db alone yields a stale database that still opens
// cleanly — recent messages live in chat.db-wal. The backup API reads through the
// WAL and emits one consistent, self-contained file.

import Foundation
import SQLite3

let PROGRAM = "msgsnap"

// Exit codes are part of the interface — msgsnap.sh branches on them, and
// <config>/cron-jobs/jobs/msgsnap-nightly.job (examples in
// local-cron-jobs/examples/) tells the operator what each one means. 2 is TCC and only TCC: the documented response to it is to regrant
// Full Disk Access, which is the wrong fix for every other way an open can
// fail, so those exit 4 (sd:935).
let EXIT_USAGE: Int32 = 1
let EXIT_NO_ACCESS: Int32 = 2   // FDA not granted to this binary
let EXIT_SQLITE: Int32 = 3
let EXIT_SOURCE: Int32 = 4      // source not openable for a reason that is not TCC

let home = FileManager.default.homeDirectoryForCurrentUser
let sourceURL = home.appendingPathComponent("Library/Messages/chat.db")
let outDirURL = home.appendingPathComponent(".local/state/msgsnap")

func err(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) }

func usage() {
    print("""
    \(PROGRAM) — snapshot ~/Library/Messages/chat.db to ~/.local/state/msgsnap/

    Usage:
      \(PROGRAM) [--out NAME] [--allow-shm]
      \(PROGRAM) --check
      \(PROGRAM) --help

      --out NAME     output basename, [A-Za-z0-9._-] only (default: chat.db)
      --allow-shm    open the source read-write if the WAL index cannot be built
                     read-only. Off by default; see README.
      --check        report whether this binary can read the source, copy nothing

    The source path is compiled in and cannot be overridden. That is deliberate:
    this binary holds Full Disk Access.

    Exit: 0 ok · 1 usage · 2 no access (FDA not granted) · 3 sqlite error
          4 source not openable for a reason that is not TCC (absent, I/O error)
    """)
}

// open(2) returns EINTR when a signal lands before the open completes. That is
// not a failure and the answer is to call again — bounded, so a handler that
// fires on every attempt still ends the run instead of spinning it. Ten:
// msgsnap-nightly saw exactly one EINTR on each night it failed, each after
// the open had stalled for seconds (sd:935), so a burst of ten in a row is a
// fault to report rather than ride through, and at that stall rate ten
// attempts still finish well inside a minute.
let OPEN_ATTEMPTS = 10

// Call `attempt` until it stops failing with EINTR or the bound is spent.
// `error` is errno as it stood right after the last call: read here, not by
// the caller, so nothing between the call and the switch can clobber it.
func retryingOnEINTR(_ attempt: () -> Int32) -> (result: Int32, error: Int32, tries: Int) {
    var result: Int32 = -1
    var error: Int32 = 0
    var tries = 0
    repeat {
        result = attempt()
        error = errno
        tries += 1
    } while result < 0 && error == EINTR && tries < OPEN_ATTEMPTS
    return (result, error, tries)
}

// Distinguish "FDA is missing" from "the file is not there". TCC denies with
// EPERM on open(2) (measured here: errno 1 from an ungranted shell, on a file
// that is mode 0644 and ours); a genuinely absent database gives ENOENT; and
// EACCES is the filesystem's own refusal, mode bits or an ACL, which no regrant
// fixes. Reporting one as another sends you to the wrong fix, so check errno
// rather than guessing, and only EPERM gets the exit code whose documented
// fix is a regrant.
func probeSource() -> (readable: Bool, reason: String, code: Int32) {
    let (fd, error, tries) = retryingOnEINTR { open(sourceURL.path, O_RDONLY) }
    if fd >= 0 { close(fd); return (true, "readable", 0) }
    switch error {
    case EPERM:
        return (false, "denied by TCC — Full Disk Access is not granted to this binary", EXIT_NO_ACCESS)
    case ENOENT:
        return (false, "no database at \(sourceURL.path) — has Messages ever run as this user?", EXIT_SOURCE)
    default:
        // Only EINTR is retried, so more than one try means the bound ran out.
        let after = tries > 1 ? " after \(tries) attempts" : ""
        return (false, "open failed\(after): \(String(cString: strerror(error))) (errno \(error))", EXIT_SOURCE)
    }
}

func validBasename(_ s: String) -> Bool {
    if s.isEmpty || s.count > 64 { return false }
    if s == "." || s == ".." { return false }
    return s.allSatisfy { $0.isLetter || $0.isNumber || $0 == "." || $0 == "_" || $0 == "-" }
}

// MARK: - arguments

var outName = "chat.db"
var allowShm = false
var checkOnly = false

var args = Array(CommandLine.arguments.dropFirst())
while let arg = args.first {
    args.removeFirst()
    switch arg {
    case "-h", "--help", "help":
        usage(); exit(0)
    case "--check":
        checkOnly = true
    case "--allow-shm":
        allowShm = true
    case "--out":
        guard let v = args.first else { err("\(PROGRAM): --out needs a value"); exit(EXIT_USAGE) }
        args.removeFirst()
        guard validBasename(v) else {
            err("\(PROGRAM): --out must be a bare filename, [A-Za-z0-9._-], max 64 chars")
            exit(EXIT_USAGE)
        }
        outName = v
    default:
        err("\(PROGRAM): unknown argument '\(arg)'")
        usage()
        exit(EXIT_USAGE)
    }
}

// MARK: - check

let probe = probeSource()
if checkOnly {
    print(probe.readable ? "ok: \(probe.reason)" : "blocked: \(probe.reason)")
    exit(probe.code)
}
guard probe.readable else {
    err("\(PROGRAM): \(probe.reason)")
    exit(probe.code)
}

// MARK: - snapshot

try? FileManager.default.createDirectory(
    at: outDirURL, withIntermediateDirectories: true,
    attributes: [.posixPermissions: 0o700])

let finalURL = outDirURL.appendingPathComponent(outName)
let tmpURL = outDirURL.appendingPathComponent(".\(outName).tmp.\(getpid())")
try? FileManager.default.removeItem(at: tmpURL)

func fail(_ msg: String, _ handle: OpaquePointer?) -> Never {
    let detail = handle.map { String(cString: sqlite3_errmsg($0)) } ?? "no detail"
    try? FileManager.default.removeItem(at: tmpURL)
    err("\(PROGRAM): \(msg): \(detail)")
    exit(EXIT_SQLITE)
}

var src: OpaquePointer?
var openFlags = SQLITE_OPEN_READONLY
var rc = sqlite3_open_v2(sourceURL.path, &src, openFlags, nil)

// A read-only open of a WAL database needs the -shm index to already exist; if
// Messages.app has never opened the db this session, SQLite wants to create it
// and cannot while read-only. Escalating to read-write is a real (small) risk to
// the user's own database, so it is opt-in and always announced — never silent.
if rc == SQLITE_CANTOPEN && allowShm {
    sqlite3_close(src); src = nil
    err("\(PROGRAM): note — read-only open failed, retrying read-write to build the WAL index")
    openFlags = SQLITE_OPEN_READWRITE
    rc = sqlite3_open_v2(sourceURL.path, &src, openFlags, nil)
}
guard rc == SQLITE_OK else {
    if rc == SQLITE_CANTOPEN && !allowShm {
        err("""
            \(PROGRAM): cannot open the source read-only. chat.db is in WAL mode and its
            -shm index is missing, which SQLite can only create with write access.
            Fix either way: open Messages.app once, or rerun with --allow-shm.
            """)
        sqlite3_close(src)
        exit(EXIT_SQLITE)
    }
    fail("open source", src)
}

var dst: OpaquePointer?
guard sqlite3_open_v2(tmpURL.path, &dst, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE, nil) == SQLITE_OK else {
    fail("open destination", dst)
}

guard let backup = sqlite3_backup_init(dst, "main", src, "main") else {
    fail("backup_init", dst)
}
let step = sqlite3_backup_step(backup, -1)
sqlite3_backup_finish(backup)
guard step == SQLITE_DONE else { fail("backup_step", dst) }

sqlite3_close(dst)
sqlite3_close(src)

try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: tmpURL.path)
do {
    _ = try FileManager.default.replaceItemAt(finalURL, withItemAt: tmpURL)
} catch {
    try? FileManager.default.removeItem(at: tmpURL)
    err("\(PROGRAM): could not place snapshot at \(finalURL.path): \(error.localizedDescription)")
    exit(EXIT_SQLITE)
}

let bytes = (try? FileManager.default.attributesOfItem(atPath: finalURL.path)[.size] as? Int) ?? nil
print("\(finalURL.path)\(bytes.map { "  \($0) bytes" } ?? "")")
