using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.IO.Compression;
using System.Net.Http;
using System.Runtime;
using System.Runtime.InteropServices;
using CUE4Parse.Compression;
using CUE4Parse.Encryption.Aes;
using CUE4Parse.FileProvider;
using CUE4Parse.FileProvider.Objects;
using CUE4Parse.MappingsProvider;
using CUE4Parse.MappingsProvider.Usmap;
using CUE4Parse.UE4.Assets;
using CUE4Parse.UE4.Assets.Exports;
using CUE4Parse.UE4.Assets.Exports.Animation;
using CUE4Parse.UE4.Assets.Exports.Material;
using CUE4Parse.UE4.Assets.Exports.SkeletalMesh;
using CUE4Parse.UE4.Assets.Exports.Sound;
using CUE4Parse.UE4.Assets.Exports.StaticMesh;
using CUE4Parse.UE4.Assets.Exports.Texture;
using CUE4Parse.UE4.Objects.Core.Misc;
using CUE4Parse.UE4.Versions;
using CUE4Parse_Conversion;
using CUE4Parse_Conversion.Animations;
using CUE4Parse_Conversion.Meshes;
using CUE4Parse_Conversion.Sounds;
using CUE4Parse_Conversion.Textures;
using CUE4Parse.UE4.Readers;
using CUE4Parse.UE4.Wwise;
using CUE4Parse.UE4.Wwise.Objects;
using CUE4Parse.UE4.Wwise.Objects.HIRC;
using CUE4Parse.UE4.Wwise.Objects.HIRC.Containers;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Newtonsoft.Json.Serialization;

namespace CUE4ParseCLI;

internal static class Program
{
    private static DefaultFileProvider? _provider;
    private static CancellationTokenSource _cts = new();
    private static string _textureFormat = "png"; // png | tga | webp
    private static string _audioFormat = "wav";   // wav | ogg | mp3 | webm
    private static string _meshFormat = "psk";    // psk | gltf
    private static bool _webpLossless = true;     // WebP encode mode; lossless preserves pixel fidelity
    private static int _webpQuality = 90;         // WebP quality 1-100 in lossy mode (ignored by lossless)
    private static string _gameDir = "";          // current game directory (for loose file reads)
    private static Dictionary<string, string> _looseFiles = new(StringComparer.OrdinalIgnoreCase); // VFS path → disk path for files CUE4Parse doesn't discover
    private static string? _vgmstreamPath;         // cached path to vgmstream-cli
    private static string? _ffmpegPath;            // cached path to ffmpeg (MP3/WebM encoding)
    private static readonly object _ffmpegInitLock = new(); // serializes the one-shot ffmpeg download
    private static Dictionary<string, string> _wemIdToName = new(StringComparer.OrdinalIgnoreCase); // WEM numeric ID → debug name
    private static Dictionary<string, string> _fmodBankCache = new(StringComparer.OrdinalIgnoreCase); // bank VFS path → materialized temp file
    private static string? _wemNameCachePath;       // per-game disk cache for WEM name map
    private static bool _wemMapBuilt;               // true once the map has been built/loaded this session
    private static readonly object _wemMapLock = new(); // serializes the lazy WEM-map build across export workers
    private static Task? _exportTask;               // currently running export (if any)
    private static Task<string?>? _pendingReadTask;   // parked stdin read from RunExportWithCancelSupport
    private static readonly Queue<JObject> _pendingGetProps = new(); // get_props commands queued during export
    private static readonly Queue<JObject> _pendingCommands = new(); // whole-export commands queued during a concurrent export/scan
    private static readonly object _respondLock = new(); // guards stdout writes during concurrent export
    // Per-export warning counter: object-level export failures (e.g. a Texture2D
    // that can't decode) surface as warnings while the asset still exports its
    // props file, so "export_done" would otherwise read a clean success. The
    // count rides along on export_done so the parent can say "finished with N
    // asset warnings" instead of unqualified success.
    private static int _exportWarningCount;
    // Worker count for the active export command. 0 until an export command
    // parses its "threads" field; drives the bounded-serialize-thread cap.
    private static int _exportThreads;

    // ─── Memory budget / gate ─────────────────────────────────────────
    // Per-export memory policy from the parent's "memory_limit_percent" /
    // "physical_only" fields (0 bytes = unlimited). Enforced layerwise:
    //   1. a GC heap hard limit (AppContext AppContext.SetData +
    //      GC.RefreshMemoryLimit) — the GC refuses to grow the managed heap
    //      past the cap and collects more aggressively as it nears it;
    //   2. a per-asset gate that backs off as process memory (which includes
    //      the native Skia/GPU buffers the GC can't see) nears the budget;
    //   3. batch barriers that run a blocking compacting GC and, in
    //      physical-only mode, trim the working set back to the OS.
    private static long _memoryBudgetBytes;   // 0 = unlimited
    private static bool _physicalOnly;        // keep the working set inside physical RAM
    private static long _totalPhysicalBytes;  // physical RAM snapshot (physical-only clamp)
    private static long _availableMemoryBytes; // RAM available right now (percentage basis)
    private static long _softGateBytes;       // workers back off above 80% of budget
    private static long _hardGateBytes;       // collect + give-up threshold, 95% of budget
    private static bool _gcHeapLimitActive;   // AppContext GCHeapHardLimit currently applied
    private static readonly Process _selfProcess = Process.GetCurrentProcess();
    private static long _lastGateGcMs;        // rate-limits in-gate global collections
    private static readonly object _gateGcLock = new();
    private static int _configuredDop;        // parent-configured worker count
    private static int _effectiveDop;         // adaptive worker count (memory-budgeted)
    private static string _lastPolicyNoticeKey = "";

    // Files at/above this size stream to disk instead of buffering in managed
    // memory. Videos (.bk2/.mp4) are the real driver — a 2 GB cutscene must
    // never exist whole inside a byte[].
    private const long LargeRawFileBytes = 64 * 1024 * 1024;
    // Physical-only mode leaves this much RAM to the OS/GUI: 10% of physical,
    // floored at 512 MB — paging is the failure mode the checkbox exists to
    // prevent, and an exporter that trusts the page file defeats it.
    private const long PhysicalOnlyReservePercent = 10;
    private const long PhysicalOnlyReserveFloor = 512L * 1024 * 1024;
    // Long texture decode+encode lanes peak around O(100 MB) per worker
    // (CTexture pixels + SKBitmap + encode output). Bleeding generous so a
    // tight budget parks workers instead of tripping the hard gate.
    private const long PerWorkerPeakBytes = 192 * 1024 * 1024;
    // Gate give-up: no measurable progress for this long at the hard wall and
    // the asset is skipped with a clear error rather than wedging its worker.
    private const int HardGateGiveUpMs = 10_000;
    // Export commands process assets in modest batches — the budget only ever
    // has to cover the in-flight LOD/decode peaks, never the whole run, and
    // each batch barrier reclaims buffers between them.
    private const int ExportBatchSize = 64;

    // Version-mismatch hint: when a stream of warnings/errors look like the UE
    // struct layout doesn't match the data on disk, surface a one-shot hint to
    // the GUI so the user can pick a different UE version and re-mount.
    private static int _versionMismatchCount;
    private static bool _versionMismatchHintEmitted;
    private static string _currentUeVersion = "";
    private static readonly string[] _versionMismatchPatterns =
    {
        "Read size is bigger than remaining archive length",
        "Read size is smaller than zero",
        "Invalid FString length",
        "Invalid compression flags",
        "unable to read FString",
    };
    private const int VersionMismatchHintThreshold = 3;

    private static readonly JsonSerializerOptions _writeOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull
    };

    // ─── Path containment ─────────────────────────────────────────────

    /// <summary>
    /// Combine <paramref name="root"/> with <paramref name="parts"/> and verify the
    /// resulting absolute path stays inside <paramref name="root"/>. Throws if a
    /// part contains traversal (../) that escapes the root. Use at every write
    /// site that takes attacker-influenced names.
    /// </summary>
    private static string SafeJoin(string root, params string[] parts)
    {
        var combined = Path.Combine(new[] { root }.Concat(parts).ToArray());
        var fullRoot = Path.GetFullPath(root);
        if (!fullRoot.EndsWith(Path.DirectorySeparatorChar.ToString()))
            fullRoot += Path.DirectorySeparatorChar;
        var full = Path.GetFullPath(combined);
        // Allow `full == fullRoot` (no separator) for the root itself.
        var fullWithSep = full + Path.DirectorySeparatorChar;
        if (!full.StartsWith(fullRoot, StringComparison.OrdinalIgnoreCase) &&
            !fullWithSep.Equals(fullRoot, StringComparison.OrdinalIgnoreCase))
        {
            var label = parts.Length > 0 ? parts[0] : "<empty>";
            throw new InvalidOperationException($"Path escapes root: {label}");
        }
        return full;
    }

    /// <summary>
    /// Split <paramref name="folder"/> on '/' and '\\', sanitize each segment,
    /// reject traversal (.., .) segments, and SafeJoin the result onto
    /// <paramref name="root"/>. Used for parent-supplied directory layouts.
    /// </summary>
    private static string SafeJoinFolderSegments(string root, string folder)
    {
        if (string.IsNullOrEmpty(folder)) return SafeJoin(root);

        var segments = folder.Split(new[] { '/', '\\' },
            StringSplitOptions.RemoveEmptyEntries);
        var safe = new List<string>(segments.Length);
        foreach (var raw in segments)
        {
            if (raw == "." || raw == "..")
                throw new InvalidOperationException("traversal segment");
            var clean = SanitizeName(raw);
            if (string.IsNullOrEmpty(clean))
                throw new InvalidOperationException("empty segment after sanitize");
            safe.Add(clean);
        }
        return SafeJoin(root, safe.ToArray());
    }

    /// <summary>
    /// Strip drive letters and absolute paths from messages forwarded to the
    /// parent so warnings/errors don't leak the user's filesystem layout.
    /// </summary>
    private static string SanitizeMessage(string? msg)
    {
        if (string.IsNullOrEmpty(msg)) return "";

        // CUE4Parse says "Can't load legacy UE3 file" whenever the package
        // summary's LegacyFileVersion is outside the range it knows — which is
        // also what a package NEWER than this build looks like (UE 5.7 uses -9).
        // Reporting that verbatim sends users hunting for a UE3 problem.
        if (msg.Contains("Can't load legacy UE3 file", StringComparison.Ordinal))
        {
            msg = "Unsupported package format — the header version is outside the range this " +
                  "build of CUE4Parse understands. This usually means the game is built on a " +
                  "newer Unreal Engine than the bundled parser supports (newest supported: " +
                  $"{MaxSupportedUe5Version()}).";
        }

        // Shipping UE5 builds cook properties unversioned, so their field names
        // live only in a .usmap. Name the profile field that fixes it instead of
        // leaving the user to guess what a "mapping file" is.
        if (msg.Contains("unversioned properties but mapping file is missing", StringComparison.Ordinal))
        {
            msg = "This game cooks its assets with unversioned properties, so a .usmap mappings " +
                  "file is required to read them. Set the profile's 'Mappings' field to a .usmap " +
                  "in Manage Profiles, then re-mount.";
        }

        // Replace Windows drive letters and POSIX absolute paths with <path>.
        var withoutDrives = System.Text.RegularExpressions.Regex.Replace(
            msg, @"[A-Za-z]:[\\/][^\s'""]+", "<path>");
        var withoutPosix = System.Text.RegularExpressions.Regex.Replace(
            withoutDrives, @"(?<![A-Za-z0-9])/(?:[^\s'""/]+/)+[^\s'""/]+", "<path>");
        return withoutPosix;
    }

    /// <summary>
    /// Human-readable, sanitized single-line description of an exception chain:
    /// outer type name + message, then the inner exception type (which is where
    /// native-load failures actually live — TypeInitializationException alone
    /// tells the user nothing about WHY Skia/Oodle failed to load).
    /// </summary>
    private static string DescribeException(Exception ex)
    {
        var sb = new System.Text.StringBuilder();
        sb.Append(ex.GetType().Name).Append(": ").Append(SanitizeMessage(ex.Message));
        if (ex.InnerException != null)
        {
            sb.Append(" (inner: ").Append(ex.InnerException.GetType().Name).Append(": ")
               .Append(SanitizeMessage(ex.InnerException.Message)).Append(')');
        }
        return sb.ToString();
    }

    /// <summary>
    /// Run <paramref name="action"/> on a pool of dedicated 4 MB-stack background
    /// threads to avoid StackOverflow on deeply-nested CUE4Parse object graphs.
    /// The pool is created lazily on first use; worker count grows to
    /// <see cref="MaxConcurrentSerializeThreads"/> so N parallel export workers
    /// can each hold one serialization thread without tripping the cap. Each
    /// request waits up to <paramref name="timeoutMs"/> for a completion;
    /// on timeout the item is orphaned (the action keeps running to
    /// completion on its worker — results are discarded), a warning is
    /// emitted and null is returned. capturedError is populated if the action
    /// threw before completing.
    /// </summary>
    private static T? RunOnBoundedThread<T>(Func<T> action, int timeoutMs, out Exception? capturedError) where T : class
    {
        EnsureSerializeWorkers();
        var work = new SerializeWork(() => (object?)action());
        _serializeQueue.Add(work);

        if (!work.Completion.Task.Wait(timeoutMs))
        {
            // The action keeps running on its orphaned worker; we just stop
            // waiting for it. The pool (worker count) still bounds concurrent
            // 4 MB stacks, so saturation degrades to waiting, never to growth.
            Respond(new { type = "warning", message = $"Serialize worker pool saturated; skipped serialization after {timeoutMs} ms" });
            capturedError = null;
            return null;
        }

        // Always Use results, never let the TCS exception surface raw.
        var task = work.Completion.Task;
        if (task.Status == TaskStatus.RanToCompletion)
        {
            capturedError = null;
            return task.Result as T;
        }
        // Faulted — unwrap the original exception.
        capturedError = task.Exception?.InnerException;
        return null;
    }

    private static string? RunSerializeOnBoundedThread(Func<string> action, int timeoutMs)
        => RunOnBoundedThread(action, timeoutMs, out _);

    // ─── Pooled serialization workers ─────────────────────────────────
    // Replacing the previous thread-per-request model: a fresh 4 MB-stack
    // Thread was constructed for EVERY serialized UObject (mesh/material props
    // on every export). Thread construction + teardown cost dominates on
    // texture-light/mesh-heavy batches, and the cap logic churned the
    // _serializeThreads HashSet once per request. A persistent pool amortizes
    // all of that while preserving the memory bound: at most
    // MaxConcurrentSerializeThreads live workers, each holding exactly one
    // 4 MB stack.
    private sealed class SerializeWork
    {
        public SerializeWork(Func<object?> action)
        {
            Action = action;
            Completion = new TaskCompletionSource<object?>(TaskCreationOptions.RunContinuationsAsynchronously);
        }
        public Func<object?> Action { get; }
        public TaskCompletionSource<object?> Completion { get; }
    }

    private static readonly System.Collections.Concurrent.BlockingCollection<SerializeWork> _serializeQueue =
        new(new System.Collections.Concurrent.ConcurrentQueue<SerializeWork>());
    private static int _serializeWorkers;
    private static readonly object _serializePoolLock = new();
    private const int SerializeStackSizeBytes = 4 * 1024 * 1024;

    /// <summary>Spawn serialized-work up to
    /// <see cref="MaxConcurrentSerializeThreads"/> persistent 4 MB-stack
    /// workers. Called lazily on first <see cref="RunOnBoundedThread{T}"/>
    /// and on every request, so doubling DOP at a new export picks up the
    /// larger cap without a restart.</summary>
    private static void EnsureSerializeWorkers()
    {
        var want = Math.Min(MaxConcurrentSerializeThreads, 64);
        if (_serializeWorkers >= want) return;
        lock (_serializePoolLock)
        {
            while (_serializeWorkers < want)
            {
                _serializeWorkers++;
                var worker = new Thread(SerializeWorkerLoop, SerializeStackSizeBytes)
                {
                    IsBackground = true,
                    Priority = ThreadPriority.BelowNormal
                };
                worker.Start();
            }
        }
    }

    private static void SerializeWorkerLoop()
    {
        // Note: TryTake blocks indefinitely; items added after a saturating
        // caller stops waiting are still executed (their result is discarded),
        // preserving the prior orphaned-thread behavior. The CLI dies when the
        // parent exits, and IsBackground threads don't block shutdown.
        while (true)
        {
            if (!_serializeQueue.TryTake(out var work, Timeout.Infinite))
                return; // collection completed
            try
            {
                work.Completion.TrySetResult(work.Action());
            }
            catch (Exception ex)
            {
                // Deliver the original exception rather than wrapping in
                // AggregateException — callers surface ex.Message directly.
                try { work.Completion.TrySetException(ex); } catch { /* racing Wait timeout */ }
            }
        }
    }

    // ─── Constants ────────────────────────────────────────────────────

    // Pin downloaded toolchain artifacts by SHA-256. If GitHub serves a different
    // payload (compromise or version drift), download is rejected. Update these
    // when bumping the URL/tag.
    private const string OodleZipSha256Win =
        "2c7b9350b1a396690ef233fc79f945b62453e7f9b026645e00b75a2d8569f283";
    private const string OodleZipUrlWin =
        "https://github.com/WorkingRobot/OodleUE/releases/download/2026-01-25-1223/clang-cl-x64-release.zip";

    // Linux artifact from the same OodleUE release (gcc build). Contains
    // lib/liboodle-data-shared.so — the .so form CUE4Parse's OodleHelper
    // expects on Linux. Pin verified against the immutable release tag.
    private const string OodleZipSha256Linux =
        "c2a92f52e1d41119c0248857fb3790757159a42009d6d419a5dafd65ec159661";
    private const string OodleZipUrlLinux =
        "https://github.com/WorkingRobot/OodleUE/releases/download/2026-01-25-1223/gcc-x64-release.zip";

    public static string OodleZipUrlActive =>
        OperatingSystem.IsWindows() ? OodleZipUrlWin : OodleZipUrlLinux;
    public static string OodleZipSha256Active =>
        OperatingSystem.IsWindows() ? OodleZipSha256Win : OodleZipSha256Linux;

    private const string VgmstreamZipSha256Win =
        "7a8f9556df7706e5dca74169ace817c1eda6bb8d8f0ab68810e6ec4c0d300573";
    private const string VgmstreamZipUrlWin =
        "https://github.com/vgmstream/vgmstream/releases/download/r1980/vgmstream-win64.zip";

    // Same tagged r1980 release ships a statically linked Linux CLI.
    // Contains vgmstream-cli at the zip root (no trailing libs needed).
    private const string VgmstreamZipSha256Linux =
        "e5fadc668c68184eec1b3593de9e3ba9b7a61c396aaf90aa8680a5f7c2caf2dd";
    private const string VgmstreamZipUrlLinux =
        "https://github.com/vgmstream/vgmstream/releases/download/r1980/vgmstream-linux-cli.zip";

    public static string VgmstreamZipUrlActive =>
        OperatingSystem.IsWindows() ? VgmstreamZipUrlWin : VgmstreamZipUrlLinux;
    public static string VgmstreamZipSha256Active =>
        OperatingSystem.IsWindows() ? VgmstreamZipSha256Win : VgmstreamZipSha256Linux;

    // ffmpeg (MP3/WebM audio encoding): BtbN's GPL shared build. Pinned to a
    // dated autobuild tag — the rolling "latest" release replaces its assets,
    // which would silently invalidate a pinned SHA256. The "-shared" variant is
    // used instead of the full static build: same encoders (libmp3lame,
    // libopus, webm muxer all verified in this artifact), roughly half the
    // download. Hashes cover the raw archive bytes as published upstream;
    // extraction streams to disk rather than buffering the ~90 MB archive in
    // managed memory.
    private const string FfmpegArchiveSha256Win =
        "817ae73c5e6aeba48ce998be093f55db80389ffcdeb855bc712ebe54301719f2";
    private const string FfmpegArchiveUrlWin =
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-05-13-07/ffmpeg-n8.1.3-14-g330caae0c1-win64-gpl-shared-8.1.zip";
    private const string FfmpegArchiveSha256Linux =
        "032d9dbc214ec10fb8d455c96fffcadca6d88c9d818644413c734a19ca2439a9";
    private const string FfmpegArchiveUrlLinux =
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-05-13-07/ffmpeg-n8.1.3-14-g330caae0c1-linux64-gpl-shared-8.1.tar.xz";

    public static string FfmpegArchiveUrlActive =>
        OperatingSystem.IsWindows() ? FfmpegArchiveUrlWin : FfmpegArchiveUrlLinux;
    public static string FfmpegArchiveSha256Active =>
        OperatingSystem.IsWindows() ? FfmpegArchiveSha256Win : FfmpegArchiveSha256Linux;

    // Hard cap on a single NDJSON command line. Anything bigger is treated as
    // adversarial input (stuck/runaway parent) — drop the line and respond with
    // an error rather than buffering forever.
    private const int MaxStdinLineBytes = 4 * 1024 * 1024;

    // Bound concurrent serialization work. Each worker is a dedicated 4 MB
    // stack so unbounded growth = OOM. If the pool (and its waiters) exceed
    // the cap, requests drop with a timeout warning rather than growing.
    // Scales with the export worker count so N parallel workers can each hold
    // one serialization thread without tripping the cap (floor of 8 preserves
    // the original limit for scan/list commands).
    private static int MaxConcurrentSerializeThreads => Math.Max(8, _exportThreads);

    // ─── Entry ────────────────────────────────────────────────────────
    private static async Task Main(string[] args)
    {
        // Force UTF-8 stdout with LF newlines so NDJSON consumers (the Python
        // parent) get bytes that decode cleanly even on Windows code pages.
        Console.OutputEncoding = new System.Text.UTF8Encoding(false);
        Console.Out.NewLine = "\n";

        // --help / -h / /? — print usage and exit
        if (args.Length > 0 && args[0] is "--help" or "-h" or "/?")
        {
            Console.WriteLine("CUE4ParseCLI — NDJSON IPC over stdio.");
            Console.WriteLine("");
            Console.WriteLine("Usage: CUE4ParseCLI[.exe] [--version | --help]");
            Console.WriteLine("");
            Console.WriteLine("Commands are read from stdin as one JSON object per line.");
            Console.WriteLine("Replies are emitted to stdout as one JSON object per line.");
            Console.WriteLine("Commands: init, browse, export, export_folder, inspect,");
            Console.WriteLine("          list_exports, get_props, scan_wwise_events,");
            Console.WriteLine("          scan_types, export_wwise_audio, export_video,");
            Console.WriteLine("          scan_fmod_banks, export_fmod_audio,");
            Console.WriteLine("          rebuild_wem_cache, cancel, quit");
            return;
        }

        // If --version flag, print and exit
        if (args.Length > 0 && args[0] == "--version")
        {
            Console.WriteLine("CUE4ParseCLI 1.0.0");
            return;
        }

        try
        {
            await RunMainLoop();
        }
        finally
        {
            // Best-effort provider release. We intentionally don't await long
            // teardown here — the process is exiting anyway, and CUE4Parse's
            // Dispose can wedge on partially-mounted providers. Force exit if
            // Dispose hasn't returned within 2s.
            var disposeThread = new Thread(() =>
            {
                try { _provider?.Dispose(); } catch { /* best effort */ }
            }) { IsBackground = true };
            disposeThread.Start();
            disposeThread.Join(2_000);
            _provider = null;
        }
    }

    private static async Task RunMainLoop()
    {
        // NDJSON stdin loop. Do NOT wrap in `using` — closing the Console
        // stdin stream during shutdown can wedge the process on Windows when
        // the parent hasn't closed its write end yet. We're about to exit, so
        // the OS cleans this up for us.
        var reader = new StreamReader(Console.OpenStandardInput());
        while (true)
        {
            // Drain commands that arrived during an export/scan and were
            // queued by RunExportWithCancelSupport rather than dropped.
            if (_pendingCommands.Count > 0)
            {
                var queued = _pendingCommands.Dequeue();
                try
                {
                    if (!await DispatchCommand(queued, reader)) return;
                }
                catch (Exception ex)
                {
                    RespondError(SanitizeMessage(ex.Message));
                }
                continue;
            }

            // Consume any parked read left by RunExportWithCancelSupport
            string? line;
            if (_pendingReadTask != null)
            {
                line = await _pendingReadTask;
                _pendingReadTask = null;
            }
            else
            {
                line = await ReadLineCappedAsync(reader);
            }
            if (line == null) break;
            if (string.IsNullOrWhiteSpace(line)) continue;
            try
            {
                var cmd = JObject.Parse(line);
                if (!await DispatchCommand(cmd, reader)) return;
            }
            catch (Exception ex)
            {
                RespondError(SanitizeMessage(ex.Message));
            }
        }
    }

    /// <summary>Route one command object. Returns false when the loop should exit (quit).</summary>
    private static async Task<bool> DispatchCommand(JObject cmd, StreamReader reader)
    {
        var cmdName = cmd.Value<string>("cmd") ?? "";
        switch (cmdName)
        {
            case "init":
                HandleInit(cmd);
                break;
            case "browse":
                HandleBrowse(cmd);
                break;
            case "detect_ue_version":
                HandleDetectUeVersion(cmd);
                break;
            case "export":
                await RunExportWithCancelSupport(reader, () => HandleExport(cmd), reclaimAfter: true);
                break;
            case "export_folder":
                await RunExportWithCancelSupport(reader, () => HandleExportFolder(cmd), reclaimAfter: true);
                break;
            case "inspect":
                HandleInspect(cmd);
                break;
            case "list_exports":
                await RunExportWithCancelSupport(reader, () => HandleListExports(cmd));
                break;
            case "get_props":
                HandleGetProps(cmd);
                break;
            case "rebuild_wem_cache":
                HandleRebuildWemCache();
                break;
            case "scan_wwise_events":
                await RunExportWithCancelSupport(reader, () => HandleScanWwiseEvents(cmd));
                break;
            case "scan_types":
                await RunExportWithCancelSupport(reader, () => HandleScanTypes(cmd));
                break;
            case "export_wwise_audio":
                await RunExportWithCancelSupport(reader, () => HandleExportWwiseAudio(cmd));
                break;
            case "scan_fmod_banks":
                await RunExportWithCancelSupport(reader, () => HandleScanFmodBanks(cmd));
                break;
            case "export_fmod_audio":
                await RunExportWithCancelSupport(reader, () => HandleExportFmodAudio(cmd));
                break;
            case "export_video":
                await RunExportWithCancelSupport(reader, () => HandleExportVideo(cmd));
                break;
            case "cancel":
                _cts.Cancel();
                // Swap then dispose: a cancelled CTS keeps its registration
                // lists alive until finalized otherwise. This path only runs
                // in the main loop (between cancel-supported operations), so
                // no task still holds a token from the disposed source.
                var cancelledCts = _cts;
                _cts = new CancellationTokenSource();
                cancelledCts.Dispose();
                Respond(new { type = "cancelled" });
                break;
            case "quit":
                Respond(new { type = "quit_ack" });
                return false;
            default:
                RespondError($"Unknown command: {cmdName}");
                break;
        }
        return true;
    }

    /// <summary>
    /// Read a line from stdin with a hard length cap. On overflow, drains
    /// characters until the next newline, emits a structured error, and
    /// returns "" so the caller skips this line. Returns null on EOF.
    /// Uses char-by-char reads so a runaway parent cannot OOM us via an
    /// unbounded buffered line.
    /// </summary>
    private static async Task<string?> ReadLineCappedAsync(StreamReader reader)
    {
        var sb = new System.Text.StringBuilder(256);
        var buf = new char[1];
        bool overflow = false;
        while (true)
        {
            int n = await reader.ReadAsync(buf, 0, 1);
            if (n == 0)
            {
                if (overflow)
                {
                    RespondError("input line too large");
                    return "";
                }
                return sb.Length == 0 ? null : sb.ToString();
            }
            char c = buf[0];
            if (c == '\r') continue;
            if (c == '\n')
            {
                if (overflow)
                {
                    RespondError("input line too large");
                    return "";
                }
                return sb.ToString();
            }
            if (overflow) continue; // drain until newline
            if (sb.Length >= MaxStdinLineBytes)
            {
                overflow = true;
                sb.Clear();
                continue;
            }
            sb.Append(c);
        }
    }

    /// <summary>
    /// Run an export action on a background thread while continuing to read
    /// stdin for cancel/quit commands.  Browse commands are processed inline.
    /// Only ONE reader touches the StreamReader at a time (no concurrent reads).
    /// </summary>
    private static async Task RunExportWithCancelSupport(StreamReader reader, Action exportAction, bool reclaimAfter = false)
    {
        _exportTask = Task.Run(exportAction);

        // Single-threaded async read loop — avoids concurrent StreamReader access.
        // NOTE: cancel only fires between assets; CUE4Parse's LoadPackage is not
        // interruptible mid-call, so a pathological asset can extend the cancel
        // latency. Quit handlers cap that wait at 5s, killing the process if
        // the export thread is still wedged.
        while (true)
        {
            var lineTask = _pendingReadTask ?? ReadLineCappedAsync(reader);
            _pendingReadTask = null;

            var first = await Task.WhenAny(lineTask, _exportTask);
            if (first == _exportTask && !lineTask.IsCompleted)
            {
                // Export finished but a read is still pending — park it for the main loop
                _pendingReadTask = lineTask;
                break;
            }

            var cmdLine = await lineTask;
            if (cmdLine == null) break;
            if (string.IsNullOrWhiteSpace(cmdLine)) { if (_exportTask.IsCompleted) break; continue; }

            try
            {
                var subCmd = JObject.Parse(cmdLine);
                var subName = subCmd.Value<string>("cmd") ?? "";
                switch (subName)
                {
                    case "cancel":
                        _cts.Cancel();
                        // A cancel must stop the whole pending run, not just
                        // the current leg. The parent pipelines one export
                        // command per selected folder, and those queue here
                        // while an export runs; RunMainLoop drains them the
                        // moment this operation completes. Left in the queue
                        // they would start full-speed exports with a freshly
                        // reset token — exactly as if the cancel never
                        // happened, which reads as "cancel takes forever"
                        // while the Cancel button is already back to
                        // disabled. Discard them; pending get_props (leaf
                        // reads) stay queued as they are quick and harmless.
                        var droppedCommands = _pendingCommands.Count;
                        _pendingCommands.Clear();
                        Respond(new { type = "cancelled" });
                        if (droppedCommands > 0)
                            Respond(new
                            {
                                type = "warning",
                                message = $"Cancel also discarded {droppedCommands} queued export command(s) that were still waiting to run",
                            });
                        break;
                    case "quit":
                        _cts.Cancel();
                        Respond(new { type = "quit_ack" });
                        // Bound the wait — if the export is wedged inside a
                        // non-interruptible LoadPackage, hard-exit so the
                        // parent isn't stranded waiting on us. Either way the
                        // parent asked us to exit, so we always Environment.Exit
                        // here rather than returning to the main loop, which
                        // would otherwise block on the next stdin read.
                        _exportTask.Wait(TimeSpan.FromSeconds(5));
                        try { _provider?.Dispose(); } catch { /* best effort */ }
                        _provider = null;
                        Environment.Exit(0);
                        return; // unreachable
                    case "browse":
                        // Browse is safe during export (read-only, no LoadPackage)
                        HandleBrowse(subCmd);
                        break;
                    case "get_props":
                        // Queue for after export — multiple get_props calls
                        // during a long export must all be honored, in order.
                        _pendingGetProps.Enqueue(subCmd);
                        break;
                    case "scan_types":
                        // Heuristic scan is fast (<1 s); run it inline so it
                        // is not silently dropped when it arrives during a
                        // concurrent wwise scan or export.
                        HandleScanTypes(subCmd);
                        break;
                    default:
                        // Whole-export commands (exports, audio scans) can't run
                        // concurrently — queue them for the main loop to run
                        // after this one completes, never silently drop them.
                        _pendingCommands.Enqueue(subCmd);
                        break;
                }
            }
            catch (OperationCanceledException) { /* swallow — already emitted cancelled */ }
            catch { /* ignore malformed lines during export */ }

            if (_exportTask!.IsCompleted) break;
        }

        try
        {
            await _exportTask!;
        }
        catch (OperationCanceledException) { /* expected on cancel */ }
        _exportTask = null;

        // Flush any queued get_props that arrived during export
        while (_pendingGetProps.Count > 0)
        {
            var queued = _pendingGetProps.Dequeue();
            try { HandleGetProps(queued); }
            catch (Exception ex) { RespondError(SanitizeMessage(ex.Message)); }
        }

        // Reset the CTS for the next operation (in case cancel was used)
        if (_cts.IsCancellationRequested)
        {
            _cts.Dispose();
            _cts = new CancellationTokenSource();
        }

        if (reclaimAfter)
            ReclaimExportMemory();
    }

    // ─── Init ─────────────────────────────────────────────────────────
    private static void HandleInit(JObject cmd)
    {
        var gameDir = cmd.Value<string>("game_dir") ?? throw new ArgumentException("game_dir required");
        var ueVersionStr = cmd.Value<string>("ue_version") ?? "GAME_UE5_4";
        var mappingsPath = cmd.Value<string>("mappings_path");

        // Parse UE version. A name this build's CUE4Parse doesn't know used to
        // fall back to 5.4 in silence, so a profile pinned to a newer engine
        // mounted at the wrong version with no indication anywhere.
        if (!Enum.TryParse<EGame>(ueVersionStr, true, out var ueVersion))
        {
            ueVersion = EGame.GAME_UE5_4;
            Respond(new
            {
                type = "warning",
                message = $"Unknown UE version '{ueVersionStr}' — this build of CUE4Parse does not " +
                          $"define it. Falling back to {ueVersion}. Newest supported is " +
                          $"{MaxSupportedUe5Version()}.",
            });
        }

        // Reset version-mismatch detector for the new mount.
        _versionMismatchCount = 0;
        _versionMismatchHintEmitted = false;
        _currentUeVersion = ueVersionStr;

        // Ensure Oodle decompression DLL is available (required for IoStore .ucas/.utoc)
        InitOodle(gameDir);

        // Reset WEM name map — each new game directory gets a fresh map and its own disk cache
        _wemIdToName = new(StringComparer.OrdinalIgnoreCase);
        _wemMapBuilt = false;
        {
            // SHA-256 truncated to 16 hex chars: stronger collision resistance
            // than FNV-32 with negligible runtime cost.
            var bytes = System.Text.Encoding.UTF8.GetBytes(gameDir.ToLowerInvariant());
            var hash = System.Security.Cryptography.SHA256.HashData(bytes);
            var sb = new System.Text.StringBuilder(16);
            for (int i = 0; i < 8; i++) sb.Append(hash[i].ToString("x2"));
            _wemNameCachePath = Path.Combine(Path.GetTempPath(), "CUE4ParseCLI_wem_names", $"{sb}.json");
        }

        // Dispose old provider if re-initializing
        _provider?.Dispose();
        _provider = null;

        DefaultFileProvider? newProvider = null;
        try
        {
#pragma warning disable CS0618
            newProvider = new DefaultFileProvider(gameDir, SearchOption.AllDirectories,
                isCaseInsensitive: true,
                versions: new VersionContainer(ueVersion));
#pragma warning restore CS0618

            newProvider.Initialize();
            _provider = newProvider;
        }
        catch
        {
            // Initialize failed — dispose the half-built provider before letting
            // the exception propagate to the caller.
            try { newProvider?.Dispose(); } catch { /* best effort */ }
            throw;
        }

        // .upk files (UE3 packages) need no manual VFS registration: CUE4Parse
        // 1.2.2.202607's DefaultFileProvider enumerates every file in the game
        // dir, .upk included (verified against Rocket League — 19579 files,
        // AkAudio.upk browsable and exportable). The old cast-to-mutable
        // registration hack stopped compiling against FileProviderDictionary
        // and only produced a spurious warning per mount.

        // Load mappings file if provided. A path that is set but unusable used
        // to be skipped in silence, so a typo looked identical to having no
        // mappings at all — and the resulting per-asset errors pointed at the
        // profile field the user had already filled in.
        if (!string.IsNullOrEmpty(mappingsPath))
        {
            if (!File.Exists(mappingsPath))
            {
                Respond(new
                {
                    type = "warning",
                    message = "Mappings file not found at the configured path — continuing without " +
                              "mappings. Assets cooked with unversioned properties will not be readable.",
                });
            }
            else
            {
                try
                {
                    _provider.MappingsContainer = new FileUsmapTypeMappingsProvider(mappingsPath);
                    Respond(new { type = "info", message = $"Loaded mappings: {Path.GetFileName(mappingsPath)}" });
                }
                catch (Exception ex)
                {
                    Respond(new
                    {
                        type = "warning",
                        message = $"Mappings file could not be parsed ({SanitizeMessage(ex.Message)}) — " +
                                  "continuing without mappings. Re-dump the .usmap if it is stale or truncated.",
                    });
                }
            }
        }

        // Always submit a zero key for the default GUID to mount unencrypted archives
        // (CUE4Parse requires SubmitKey to actually mount .pak and IoStore .ucas/.utoc)
        var zeroKey = new FAesKey("0x0000000000000000000000000000000000000000000000000000000000000000");
        int keysSubmitted = 0;
        try
        {
            _provider.SubmitKey(new FGuid(), zeroKey);
            keysSubmitted++;
        }
        catch (Exception ex)
        {
            Respond(new { type = "warning", message = $"Zero key: {SanitizeMessage(ex.Message)}" });
        }

        // Submit user-provided AES keys for encrypted archives. The aes_keys
        // entry must be an array; anything else is rejected with a warning so
        // we don't silently lose keys due to a parent-side bug.
        JArray keys;
        var keysToken = cmd["aes_keys"];
        if (keysToken == null || keysToken.Type == JTokenType.Null)
        {
            keys = new JArray();
        }
        else if (keysToken is JArray arr)
        {
            keys = arr;
        }
        else
        {
            Respond(new { type = "warning", message = "aes_keys malformed; ignoring" });
            keys = new JArray();
        }
        foreach (var k in keys)
        {
            // Accept both shapes: {"guid": "...", "key": "0x..."} objects and
            // bare "0x..." strings (common when a parent hands through a value
            // scraped from Google/a key list without building the wrapper).
            // Indexing a JValue used to throw mid-foreach and abort the whole
            // init AFTER Oodle/zero-key setup — treat bad shapes as warnings.
            string guid, hex;
            switch (k)
            {
                case JValue v: // bare hex string
                    guid = "00000000000000000000000000000000";
                    hex = v.Value<string>() ?? "";
                    break;
                case JObject o:
                    guid = o.Value<string>("guid") ?? "00000000000000000000000000000000";
                    hex = o.Value<string>("key") ?? "";
                    break;
                default:
                    Respond(new { type = "warning", message = $"aes_keys entry #{keysSubmitted}: unrecognized shape, skipping" });
                    continue;
            }
            if (string.IsNullOrEmpty(hex)) continue;
            try
            {
                _provider.SubmitKey(new FGuid(guid), new FAesKey(hex));
                keysSubmitted++;
            }
            catch (FormatException)
            {
                // Generic message — never echo the raw exception.Message which
                // may include the malformed key bytes back to the parent.
                Respond(new { type = "warning", message = $"Key {guid}: invalid (length or format)" });
            }
            catch
            {
                Respond(new { type = "warning", message = $"Key {guid}: invalid (length or format)" });
            }
        }

        try
        {
            _provider.PostMount();
        }
        catch
        {
            try { _provider?.Dispose(); } catch { /* best effort */ }
            _provider = null;
            throw;
        }

        EnsureConcurrentReaders();

        var archiveCount = _provider.MountedVfs.Count;
        var fileCount = _provider.Files.Count;
        // Count archives still requiring keys
        int unmounted = 0;
        try { unmounted = _provider.RequiredKeys.Count(); } catch { }

        // Surface the unmounted archive names + GUIDs so the GUI can offer
        // a targeted "add the missing key" prompt. Tolerated as best-effort:
        // older CUE4Parse builds may not expose UnloadedVfs in this shape.
        var unmountedArchives = new List<object>();
        try
        {
            var unloaded = _provider.UnloadedVfs;
            if (unloaded != null)
            {
                foreach (var vfs in unloaded)
                {
                    if (vfs == null) continue;
                    string name;
                    string guid;
                    try { name = vfs.Name ?? string.Empty; } catch { name = string.Empty; }
                    try { guid = vfs.EncryptionKeyGuid.ToString(); } catch { guid = string.Empty; }
                    unmountedArchives.Add(new { name, guid });
                }
            }
        }
        catch { /* older CUE4Parse — fall back to count-only */ }

        // Scan for loose files that CUE4Parse doesn't natively discover
        // (.upk, .wem, .ewem, .bnk — common in UE3 games like pre-F2P Rocket
        // League; .bank — FMOD Studio banks, which the FMOD UE plugin loads
        // from disk outside the pak)
        _gameDir = gameDir;
        _looseFiles.Clear();
        _fmodBankCache.Clear();
        var looseExtensions = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            { ".upk", ".wem", ".ewem", ".bnk", ".bank" };
        var gameDirInfo = new DirectoryInfo(gameDir);
        if (gameDirInfo.Exists)
        {
            try
            {
                foreach (var file in gameDirInfo.EnumerateFiles("*", SearchOption.AllDirectories))
                {
                    if (!looseExtensions.Contains(file.Extension)) continue;
                    var gamePath = Path.GetRelativePath(gameDir, file.FullName).Replace('\\', '/');
                    // Only add if CUE4Parse didn't already register it
                    if (!_provider.Files.ContainsKey(gamePath) &&
                        !_provider.Files.ContainsKey(gamePath.ToLowerInvariant()))
                    {
                        _looseFiles[gamePath] = file.FullName;
                    }
                }
            }
            catch (Exception ex)
            {
                Respond(new { type = "warning", message = $"Loose file scan: {SanitizeMessage(ex.Message)}" });
            }
        }

        // BC7/BC6H/ETC decode: CUE4Parse-Conversion's default path routes these
        // through the native Detex.dll it embeds, which is Windows-only and is
        // never initialized by this CLI — on Linux every TC_BC7 texture used
        // to die with "Detex decompression failed: not initialized" (Islands
        // of Insight: dozens of textures per export batch). Enable the pure-C#
        // AssetRipper.TextureDecoder path instead: same output, no native lib
        // needed, identical behavior to what CUE4Parse master uses on
        // non-Windows hosts (it does Bc7/Bc6h/Bc1..Bc5/ASTC/ETC in managed
        // code).
        TextureDecoder.UseAssetRipperTextureDecoder = true;

        // One-time texture-sdk probe: if Skia's native lib can't load, every
        // texture export degenerates into hundreds of type-initializer errors
        // mid-export. Warn once at mount time so it's diagnosable up front.
        ProbeSkia();

        // GPU texture decode: probe once per mount and report availability so
        // the GUI can show what the upcoming exports will use. The resolved
        // "gpu" value (GUI passes Auto explicitly resolved to bool in the
        // config, or omits it) is honored by exports; the probe itself only
        // asks Vulkan what devices exist.
        EmitGpuStatus(cmd);

        // Echo the GC the process actually launched with — the "use only
        // physical memory" setting relaunches the CLI with workstation GC
        // (env beats runtimeconfig on net9+), and this log line is how the
        // user verifies it took effect.
        Respond(new
        {
            type = "info",
            message = $"Garbage collector: {(GCSettings.IsServerGC ? "server" : "workstation")}",
        });

        Respond(new
        {
            type = "init_done",
            archive_count = archiveCount,
            unmounted_count = unmounted,
            file_count = fileCount + _looseFiles.Count,
            loose_file_count = _looseFiles.Count,
            keys_submitted = keysSubmitted,
            unmounted_archives = unmountedArchives
        });
    }

    private static bool _skiaProbed;

    /// <summary>
    /// GPU texture decode preference for the active/export commands. Set from
    /// the "gpu" field (init/export/export_folder); defaults to true so a
    /// bare CLI run gets GPU acceleration when a suitable device exists, and
    /// export falls back to the CPU decoder per texture whenever the GPU path
    /// cannot handle a texture.
    /// </summary>
    private static bool _gpuDecode = true;

    /// <summary>
    /// Probe the GPU decoder and emit a single gpu_status NDJSON record on
    /// stdout. Called at init so the GUI can report device availability;
    /// cheap after the first call (probe result is cached).
    /// </summary>
    private static void EmitGpuStatus(JObject? cmd)
    {
        if (cmd is not null && cmd["gpu"] is { } flag)
            _gpuDecode = !(flag.Type == JTokenType.Boolean && !flag.Value<bool>());
        if (!_gpuDecode)
        {
            Respond(new { type = "gpu_status", available = false, device = "", reason = "disabled by export settings" });
            return;
        }
        var (available, device, reason) = GpuTextureDecoder.Probe();
        Respond(new { type = "gpu_status", available, device, reason });
    }

    /// <summary>
    /// Cheap warm-up: touches SkiaSharp's static state so the DllImport /
    /// extraction failure (if any) surfaces here, at init, with a full inner-exception
    /// description instead of being repeated after every Texture2D in an export.
    /// </summary>
    private static void ProbeSkia()
    {
        if (_skiaProbed) return;
        _skiaProbed = true;
        try
        {
            // A real P/Invoke path (native surface allocation), not just a
            // managed struct field — struct cctors only run at their first
            // actual call sites, so a field read may dodge the failure.
            using var bmp = new SkiaSharp.SKBitmap(4, 4);
            bmp.Erase(SkiaSharp.SKColors.Transparent);
        }
        catch (Exception ex)
        {
            Respond(new
            {
                type = "warning",
                message = "Skia native library could not be initialized — texture decoding is unavailable " +
                          $"in this build: {DescribeException(ex)}"
            });
        }
    }

    // ─── Detect UE Version ───────────────────────────────────────────
    private static void HandleDetectUeVersion(JObject cmd)
    {
        var gameDir = cmd.Value<string>("game_dir") ?? throw new ArgumentException("game_dir required");
        var detected = DetectUeVersionFromExe(gameDir);

        if (detected != null)
        {
            Respond(new
            {
                type = "version_detected",
                suggested = detected.SuggestedVersion,
                source_exe = detected.SourceExe,
                file_version = detected.FileVersion
            });
        }
        else
        {
            Respond(new
            {
                type = "version_detected",
                suggested = (string?)null,
                reason = "No executable with detectable version found"
            });
        }
    }

    private class VersionDetectionResult
    {
        public string SuggestedVersion { get; set; } = "";
        public string SourceExe { get; set; } = "";
        public string FileVersion { get; set; } = "";
    }

    /// <summary>
    /// Detect UE version from exe FileVersionInfo. Walks game_dir and one parent up
    /// for *-Win64-Shipping.exe, falling back to [GameName].exe and CrashReportClient.exe.
    /// Returns null if no suitable exe found or version parsing fails.
    /// </summary>
    private static VersionDetectionResult? DetectUeVersionFromExe(string gameDir)
    {
        var dirInfo = new DirectoryInfo(gameDir);
        if (!dirInfo.Exists) return null;

        // Collect candidate directories: game dir + parent
        var candidateDirs = new[] { dirInfo, dirInfo.Parent }.Where(d => d != null).ToArray();

        // Ordered exe patterns
        var exePatterns = new[]
        {
            new Regex(@".*-Win64-Shipping\.exe$", RegexOptions.IgnoreCase),
            new Regex(@".*\.exe$", RegexOptions.IgnoreCase),
        };
        var fallbackNames = new[] { "CrashReportClient.exe" };

        foreach (var dir in candidateDirs)
        {
            if (dir == null || !dir.Exists) continue;

            try
            {
                var files = dir.GetFiles("*.exe", SearchOption.TopDirectoryOnly);

                // Try ordered patterns first
                foreach (var pattern in exePatterns)
                {
                    var match = files.FirstOrDefault(f => pattern.IsMatch(f.Name));
                    if (match != null)
                    {
                        var result = TryExtractVersionFromExe(match.FullName);
                        if (result != null) return result;
                    }
                }

                // Try fallback names
                foreach (var name in fallbackNames)
                {
                    var match = files.FirstOrDefault(f => f.Name.Equals(name, StringComparison.OrdinalIgnoreCase));
                    if (match != null)
                    {
                        var result = TryExtractVersionFromExe(match.FullName);
                        if (result != null) return result;
                    }
                }
            }
            catch { /* skip on any error */ }
        }

        return null;
    }

    /// <summary>
    /// Extract and map FileVersionInfo to EGame string. Returns null if parsing fails.
    /// Maps "4.12" → "GAME_UE4_12", clamps out-of-range minors to nearest supported.
    /// </summary>
    private static VersionDetectionResult? TryExtractVersionFromExe(string exePath)
    {
        try
        {
            var info = FileVersionInfo.GetVersionInfo(exePath);
            if (string.IsNullOrEmpty(info.FileVersion)) return null;

            // Parse "4.12.5.0" → 4, 12
            var parts = info.FileVersion.Split('.');
            if (parts.Length < 2) return null;
            if (!int.TryParse(parts[0], out var major)) return null;
            if (!int.TryParse(parts[1], out var minor)) return null;

            var suggested = MapVersionToEGame(major, minor);
            if (suggested == null) return null;

            return new VersionDetectionResult
            {
                SuggestedVersion = suggested,
                SourceExe = Path.GetFileName(exePath),
                FileVersion = $"{major}.{minor}.{(parts.Length > 2 ? parts[2] : "0")}.{(parts.Length > 3 ? parts[3] : "0")}"
            };
        }
        catch { /* parsing failed */ return null; }
    }

    /// <summary>
    /// Highest minor version N for which EGame defines GAME_UE{major}_N.
    /// Probed off the enum rather than hardcoded — a hardcoded ceiling went
    /// stale at 5.4 and silently clamped every newer game down to it.
    /// </summary>
    private static int MaxSupportedMinor(int major)
    {
        var highest = 0;
        for (var minor = 0; minor <= 99; minor++)
        {
            if (Enum.TryParse<EGame>($"GAME_UE{major}_{minor}", true, out _))
                highest = minor;
        }
        return highest;
    }

    private static string MaxSupportedUe5Version() => $"GAME_UE5_{MaxSupportedMinor(5)}";

    /// <summary>
    /// Map Major.Minor to EGame string. Clamps minors to the range this build's
    /// CUE4Parse actually defines. UE4 floors at 11 — earlier minors exist in the
    /// enum but are not meaningfully distinct for asset parsing.
    /// </summary>
    private static string? MapVersionToEGame(int major, int minor)
    {
        if (major == 4)
        {
            minor = Math.Max(11, Math.Min(MaxSupportedMinor(4), minor));
            return $"GAME_UE4_{minor}";
        }
        else if (major == 5)
        {
            minor = Math.Max(0, Math.Min(MaxSupportedMinor(5), minor));
            return $"GAME_UE5_{minor}";
        }
        return null;
    }

    // ─── Browse ───────────────────────────────────────────────────────
    private static void HandleBrowse(JObject cmd)
    {
        EnsureProvider();
        var path = (cmd.Value<string>("path") ?? "/").TrimEnd('/');
        if (string.IsNullOrEmpty(path)) path = "";

        // Normalize: remove leading slash for matching
        var prefix = path.Length > 0 ? path.TrimStart('/') + "/" : "";

        var entries = new List<object>();
        var seenFolders = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var seenFiles = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        // Snapshot the live Files dictionary so a concurrent export that mutates
        // the provider does not raise InvalidOperationException mid-iteration.
        var filesSnapshot = _provider!.Files.ToArray();

        foreach (var file in filesSnapshot)
        {
            var gamePath = file.Value.Path;
            if (prefix.Length > 0 && !gamePath.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                continue;

            var remainder = gamePath[prefix.Length..];
            var slashIdx = remainder.IndexOf('/');
            if (slashIdx >= 0)
            {
                var folderName = remainder[..slashIdx];
                if (seenFolders.Add(folderName))
                    entries.Add(new { name = folderName, is_folder = true });
            }
            else if (seenFiles.Add(remainder.ToLowerInvariant()))
            {
                var assetType = ClassifyAssetType(gamePath);
                entries.Add(new { name = remainder, is_folder = false, asset_type = assetType });
            }
        }

        // Include loose files not registered with CUE4Parse (.upk, .wem, .ewem, .bnk)
        foreach (var (loosePath, _) in _looseFiles)
        {
            if (prefix.Length > 0 && !loosePath.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                continue;

            var remainder = loosePath[prefix.Length..];
            var slashIdx = remainder.IndexOf('/');
            if (slashIdx >= 0)
            {
                var folderName = remainder[..slashIdx];
                if (seenFolders.Add(folderName))
                    entries.Add(new { name = folderName, is_folder = true });
            }
            else if (seenFiles.Add(remainder.ToLowerInvariant()))
            {
                var assetType = ClassifyAssetType(loosePath);
                entries.Add(new { name = remainder, is_folder = false, asset_type = assetType });
            }
        }

        Respond(new
        {
            type = "browse_result",
            path,
            count = entries.Count,
            entries
        });
    }

    /// <summary>Classify a file's asset type using path heuristics only (no package loading).</summary>
    private static string ClassifyAssetType(string gamePath)
    {
        var lower = gamePath.ToLowerInvariant();
        // Non-package files are classified by extension
        if (lower.EndsWith(".wem")) return "Audio";
        if (lower.EndsWith(".ewem")) return "EncryptedAudio";
        if (lower.EndsWith(".bnk")) return "SoundBank";
        if (lower.EndsWith(".bank")) return "FmodBank";
        if (lower.EndsWith(".ubulk")) return "BulkData";
        if (lower.EndsWith(".umap")) return "Map";
        if (lower.EndsWith(".upk")) return "UE3Package";
        if (lower.EndsWith(".bin")) return "Binary";
        if (!lower.EndsWith(".uasset")) return "Unknown";

        // Path-based heuristic classification — safe, instant, no I/O
        if (lower.Contains("/wwiseaudio/"))
        {
            if (lower.Contains("/event/") || lower.Contains("/events/")) return "AkAudioEvent";
            if (lower.Contains("/media/")) return "AkMediaAsset";
            return "WwiseAsset";
        }

        var fileName = Path.GetFileNameWithoutExtension(lower);
        if (fileName.StartsWith("sm_")) return "StaticMesh";
        if (fileName.StartsWith("sk_") || fileName.StartsWith("skm_")) return "SkeletalMesh";
        if (fileName.StartsWith("t_")) return "Texture2D";
        if (fileName.StartsWith("m_")) return "Material";
        if (fileName.StartsWith("mi_")) return "MaterialInstance";
        if (fileName.StartsWith("abp_")) return "AnimBlueprint";
        if (fileName.StartsWith("bp_")) return "Blueprint";
        if (fileName.StartsWith("wbp_")) return "WidgetBlueprint";
        if (fileName.StartsWith("da_")) return "DataAsset";
        if (fileName.StartsWith("dt_")) return "DataTable";
        if (fileName.StartsWith("ns_")) return "NiagaraSystem";

        // Folder-path hints
        if (lower.Contains("/textures/") || lower.Contains("/texture/")) return "Texture2D";
        if (lower.Contains("/meshes/") || lower.Contains("/staticmesh")) return "StaticMesh";
        if (lower.Contains("/skeletal")) return "SkeletalMesh";
        if (lower.Contains("/animation") || lower.Contains("/anim/")) return "AnimSequence";
        if (lower.Contains("/sound/") || lower.Contains("/audio/")) return "SoundWave";
        if (lower.Contains("/material")) return "Material";
        if (lower.Contains("/niagara") || lower.Contains("/particle")) return "ParticleSystem";

        return "Asset";
    }

    // ─── Export single assets ─────────────────────────────────────────
    /// <summary>
    /// Parse the "threads" field of an export command. &gt;0 trusts the parent;
    /// anything else means auto — one worker per core (uncapped).
    /// </summary>
    private static int ResolveExportThreads(JObject cmd)
    {
        var requested = cmd["threads"]?.Value<int>() ?? 0;
        return requested > 0 ? requested : Environment.ProcessorCount;
    }

    // ─── Memory policy ────────────────────────────────────────────────

    /// <summary>
    /// Cross-platform, human-scaled byte formatter for memory notices.
    /// </summary>
    private static string FormatByteAmount(long bytes)
    {
        if (bytes >= 1024L * 1024 * 1024)
            return $"{bytes / (double)(1024L * 1024 * 1024):F1} GB";
        if (bytes >= 1024 * 1024)
            return $"{bytes / (double)(1024 * 1024):F0} MB";
        return $"{bytes} B";
    }

    /// <summary>
    /// Parse "memory_limit_percent" (0 = unlimited, otherwise clamped 1-95)
    /// and "physical_only", then compute the export budget. The percentage
    /// applies to the RAM that is currently available (free) — not the total
    /// — so the exporter stays a well-behaved citizen when the machine is
    /// under memory pressure. The exporter's own resident pages (mounted
    /// archive index, leftovers from previous commands) are added back to the
    /// free-RAM probe, since it can shrink those; without the add-back each
    /// export in a long-lived CLI session would budget against its own
    /// footprint and starve its successor. physical_only still clamps the
    /// budget to total physical RAM minus an OS reserve even when no
    /// percentage is set, so the exporter can never push its working set into
    /// swap. Also establishes the GC heap hard limit (~85% of the budget —
    /// the rest is headroom for native Skia/Vulkan buffers the GC can't see)
    /// and seeds the adaptive worker count.
    /// </summary>
    /// <param name="adaptDop">false for the audio/video handlers, whose local
    /// degree of parallelism is theirs to manage.</param>
    private static void ResolveMemoryPolicy(JObject cmd, bool adaptDop = true)
    {
        var pct = cmd.Value<int?>("memory_limit_percent") ?? 0;
        if (pct is < 0 or > 95) pct = 0;
        _physicalOnly = cmd["physical_only"]?.Value<bool>() ?? false;

        // TotalAvailableMemoryBytes honors container limits; catching here is
        // just belt-and-braces against exotic runtimes.
        try { _totalPhysicalBytes = GC.GetGCMemoryInfo().TotalAvailableMemoryBytes; }
        catch { _totalPhysicalBytes = 0; }
        _availableMemoryBytes = ProbeAvailableMemoryBytes();

        // Our own resident pages are counted as "used" by the OS probe, but
        // they are ours to release — add them back so the budget reflects
        // what is free beyond the footprint we already hold. selfWS is part
        // of the "used" total the free probe excludes, so effectiveFree can
        // never exceed total physical RAM and the budget never exceeds
        // pct% of real RAM.
        long effectiveFree = _availableMemoryBytes > 0
            ? _availableMemoryBytes + Math.Max(0L, CurrentWorkingSetBytes())
            : 0;

        long budget = 0;
        if (pct > 0 && effectiveFree > 0)
            budget = (long)((ulong)effectiveFree * (ulong)Math.Min(pct, 95) / 100UL);

        if (_physicalOnly && _totalPhysicalBytes > 0)
        {
            var reserve = Math.Max(PhysicalOnlyReserveFloor, _totalPhysicalBytes / PhysicalOnlyReservePercent);
            var physBudget = _totalPhysicalBytes - reserve;
            if (physBudget > 0 && (budget == 0 || physBudget < budget))
                budget = physBudget;
        }

        _memoryBudgetBytes = budget;
        _softGateBytes = budget > 0 ? (long)((double)budget * 0.80) : 0;
        _hardGateBytes = budget > 0 ? (long)((double)budget * 0.95) : 0;

        ApplyGcHeapLimit();

        if (adaptDop)
        {
            _configuredDop = Math.Max(1, _exportThreads);
            _effectiveDop = ComputeInitialDop();
        }

        var noticeKey = $"{budget}/{_physicalOnly}";
        if (noticeKey != _lastPolicyNoticeKey)
        {
            _lastPolicyNoticeKey = noticeKey;
            if (budget > 0)
            {
                string basis;
                if (pct > 0)
                {
                    basis = $"{pct}% of {FormatByteAmount(effectiveFree)} RAM currently free";
                }
                else
                {
                    // 100 - reserve = 90% of total RAM in physical-only mode
                    // with no explicit percentage.
                    var total = _totalPhysicalBytes > 0
                        ? $" (90% of {FormatByteAmount(_totalPhysicalBytes)} physical RAM)"
                        : "";
                    basis = $"physical-only cap{total}";
                }
                var mode = _physicalOnly ? ", physical-only mode" : "";
                Respond(new
                {
                    type = "info",
                    message = $"Memory limit active: {FormatByteAmount(budget)} ({basis}{mode})",
                });
            }
        }
    }

    /// <summary>
    /// Establish (or clear) the runtime-adjustable GC heap hard limit. When a
    /// limited export later emits an unlimited command, dropping the
    /// AppContext value and re-refreshing restores the runtime default — the
    /// AppContext reader treats a missing value as "no override"
    /// (``as ulong? ?? ulong.MaxValue`` in GC.CoreCLR), and a literal 0 would
    /// instead be an invalid limit.
    /// </summary>
    private static void ApplyGcHeapLimit()
    {
        try
        {
            if (_memoryBudgetBytes > 0)
            {
                AppContext.SetData("GCHeapHardLimit", (ulong)(_memoryBudgetBytes * 85 / 100));
                GC.RefreshMemoryLimit();
                _gcHeapLimitActive = true;
            }
            else if (_gcHeapLimitActive)
            {
                AppContext.SetData("GCHeapHardLimit", null);
                GC.RefreshMemoryLimit();
                _gcHeapLimitActive = false;
            }
        }
        catch (InvalidOperationException ex)
        {
            // The new limit is below what is already committed (a freshly
            // mounted archive index can be large) or 32-bit refused the call.
            // The export must go on — the per-asset gate still bounds usage.
            _gcHeapLimitActive = false;
            Respond(new
            {
                type = "warning",
                message = $"Memory limit not applied to the GC heap: {SanitizeMessage(ex.Message)}",
            });
        }
    }

    /// <summary>Worker count for a budgeted export: never more than the parent
    /// configured, and never more than the budget can hold one peak per
    /// worker. Always at least one worker — progress never stalls entirely.</summary>
    private static int ComputeInitialDop()
    {
        var configured = Math.Max(1, _configuredDop);
        if (_memoryBudgetBytes <= 0) return configured;
        var affordable = (int)Math.Max(1, _memoryBudgetBytes / PerWorkerPeakBytes);
        return Math.Min(configured, affordable);
    }

    /// <summary>The degree of parallelism the batched runner actually uses.</summary>
    private static int EffectiveDop() =>
        _memoryBudgetBytes > 0 ? Math.Max(1, _effectiveDop) : Math.Max(1, _exportThreads);

    /// <summary>
    /// Process memory the budget is measured against. Working set (RSS on
    /// Linux, WS on Windows) is the right yardstick for physical-only mode
    /// because it counts the native Skia/Vulkan pages the GC is blind to.
    /// Falls back to the GC's own load estimate if private queries fail.
    /// </summary>
    private static long CurrentWorkingSetBytes()
    {
        try
        {
            _selfProcess.Refresh();
            var ws = _selfProcess.WorkingSet64;
            if (ws > 0) return ws;
        }
        catch { /* probing failed — fall through to the GC estimate */ }
        try { return GC.GetGCMemoryInfo().MemoryLoadBytes; }
        catch { return 0; }
    }

    /// <summary>
    /// RAM the OS can hand out right now without swapping — the basis the
    /// "memory_limit_percent" is applied to. Probed per-OS first (Windows
    /// GlobalMemoryStatusEx, Linux /proc/meminfo MemAvailable); if a native
    /// probe fails, falls back to the GC's container-aware estimate
    /// (TotalAvailableMemoryBytes − MemoryLoadBytes keeps cgroup semantics,
    /// so it is correct inside containers too). 0 = unknown.
    /// Called once per export command when the policy resolves — a snapshot,
    /// not a poller; nothing here retains state across calls.
    /// </summary>
    private static long ProbeAvailableMemoryBytes()
    {
        var native = 0L;
        try
        {
            if (OperatingSystem.IsWindows())
                native = WindowsAvailableMemoryBytes();
            else if (OperatingSystem.IsLinux())
                native = LinuxAvailableMemoryBytes();
        }
        catch { native = 0; }   // exotic runtime / stripped kernel interface

        if (native > 0) return native;
        try
        {
            var info = GC.GetGCMemoryInfo();
            var free = info.TotalAvailableMemoryBytes - info.MemoryLoadBytes;
            return free > 0 ? free : 0;
        }
        catch { return 0; }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MEMORYSTATUSEX
    {
        public uint dwLength;
        public uint dwMemoryLoad;
        public ulong ullTotalPhys;
        public ulong ullAvailPhys;
        public ulong ullTotalPageFile;
        public ulong ullAvailPageFile;
        public ulong ullTotalVirtual;
        public ulong ullAvailVirtual;
        public ulong ullAvailExtendedVirtual;
    }

    // ullAvailPhys is the kernel's "memory you can allocate without severely
    // impacting other applications" — includes the standby list, so it is the
    // same notion of "available" as Linux MemAvailable.
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GlobalMemoryStatusEx(ref MEMORYSTATUSEX lpBuffer);

    private static long WindowsAvailableMemoryBytes()
    {
        var stat = new MEMORYSTATUSEX
        {
            dwLength = (uint)Marshal.SizeOf<MEMORYSTATUSEX>(),
        };
        // The ull* fields are 64-bit for a 32-bit caller too; a real reading
        // can never exceed long.MaxValue (8 EB), so the cast is safe.
        return GlobalMemoryStatusEx(ref stat) && stat.ullAvailPhys <= long.MaxValue
            ? (long)stat.ullAvailPhys
            : 0;
    }

    private static long LinuxAvailableMemoryBytes()
    {
        // MemAvailable is the kernel's own estimate of allocatable-without-
        // swapping RAM (free pages + reclaimable cache); MemFree is the
        // fallback on kernels that predate the field (< 3.14).
        if (!File.Exists("/proc/meminfo")) return 0;
        var fallback = 0L;
        foreach (var line in File.ReadLines("/proc/meminfo"))
        {
            if (line.StartsWith("MemAvailable:", StringComparison.Ordinal))
                return ParseMeminfoKib(line);
            if (fallback == 0 && line.StartsWith("MemFree:", StringComparison.Ordinal))
                fallback = ParseMeminfoKib(line);
        }
        return fallback;
    }

    /// <summary>Parse the "value kB" tail of a /proc/meminfo line into bytes.</summary>
    private static long ParseMeminfoKib(string line)
    {
        var colon = line.IndexOf(':');
        if (colon < 0) return 0;
        var tail = line.Substring(colon + 1).Trim();
        var space = tail.IndexOf(' ');
        if (space <= 0) return 0;
        return long.TryParse(tail.AsSpan(0, space), out var kib) ? kib * 1024L : 0;
    }

    /// <summary>
    /// Per-asset memory gate. Healthy (< 80% of budget) → runs immediately.
    /// Approaching → the worker backs off in small sleeps so in-flight decodes
    /// can drain. At the hard wall → one rate-limited worker performs a
    /// blocking compacting collection while the others simply wait. Sustained
    /// overload is shed as a per-asset failure after a bounded wait, never an
    /// unbounded hang. Cancellation-aware: cancel always wins immediately.
    /// </summary>
    private static void WaitUnderMemoryGate(CancellationToken token)
    {
        if (_memoryBudgetBytes <= 0) return;

        var backoffMs = 50;
        var startMs = Environment.TickCount64;

        while (!token.IsCancellationRequested)
        {
            var used = CurrentWorkingSetBytes();

            if (used < _softGateBytes)
                return;

            if (used < _hardGateBytes)
            {
                // Approaching — let whatever is in flight finish and drain.
                Thread.Sleep(backoffMs);
                backoffMs = Math.Min(250, backoffMs * 2);
                continue;
            }

            // At the hard wall — one worker (rate-limited to one collection
            // every 300 ms) runs the blocking compacting GC; others wait so
            // we don't stack point GCs. GC suspension collects the sleeping
            // workers at their safe points as usual.
            if (Monitor.TryEnter(_gateGcLock))
            {
                try
                {
                    var now = Environment.TickCount64;
                    if (now - _lastGateGcMs >= 300)
                    {
                        _lastGateGcMs = now;
                        GC.Collect(2, GCCollectionMode.Aggressive, blocking: true, compacting: true);
                    }
                }
                finally
                {
                    Monitor.Exit(_gateGcLock);
                }
            }

            Thread.Sleep(backoffMs);
            backoffMs = Math.Min(250, backoffMs * 2);

            if (Environment.TickCount64 - startMs > HardGateGiveUpMs)
                throw new MemoryLimitExceededException(
                    $"Could not free memory below the limit ({FormatByteAmount(_memoryBudgetBytes)}) within {HardGateGiveUpMs / 1000} s — asset skipped");
        }
    }

    /// <summary>
    /// Batch barrier: a blocking compacting GC returns LOS UObject graphs,
    /// decoded pixel buffers and mesh LOD arrays to the OS, then — in
    /// physical-only mode — the working set is trimmed so the OS stops
    /// attributing parked-but-free pages to the exporter (no page-file
    /// reliance). Afterwards the adaptive worker count is re-evaluated from
    /// live usage: shed when pressed, restored when comfortable.
    /// </summary>
    private static void CollectAndTrimBetweenBatches()
    {
        if (_memoryBudgetBytes <= 0) return;

        GC.Collect(2, GCCollectionMode.Aggressive, blocking: true, compacting: true);
        GC.WaitForPendingFinalizers();
        GC.Collect(2, GCCollectionMode.Aggressive, blocking: true, compacting: true);

        if (_physicalOnly) TrimWorkingSet();

        _effectiveDop = CurrentWorkingSetBytes() >= _hardGateBytes
            ? Math.Max(1, _effectiveDop / 2)
            : (_effectiveDop < _configuredDop && CurrentWorkingSetBytes() < _softGateBytes
                ? Math.Min(_configuredDop, _effectiveDop + 1)
                : _effectiveDop);
    }

    /// <summary>
    /// Post-export reclaim, run after every completed asset export (success,
    /// failure, or cancel). A blocking compacting collection with a one-shot
    /// LOH compaction returns decoded pixel buffers, mesh LOD arrays and WEM
    /// payloads parked in managed memory to the OS promptly — without it an
    /// idle server-GC CLI can hold region-sized free heaps indefinitely after
    /// a cancel discards in-flight decode work. The unconditional working-set
    /// trim (below) evicts the freed pages even when no memory budget is
    /// active, so the OS sees the release instead of "free list" pages.
    /// Runs after ``await _exportTask`` — the handler's succeeded/failed lists
    /// and every in-flight decode object are already unreferenced, so nothing
    /// survives the collection and no live state is touched. Hygiene must
    /// never surface as an error, hence the blanket catch.
    /// </summary>
    private static void ReclaimExportMemory()
    {
        try
        {
            GCSettings.LargeObjectHeapCompactionMode =
                GCLargeObjectHeapCompactionMode.CompactOnce;  // one-shot; auto-resets to Default
            GC.Collect(2, GCCollectionMode.Aggressive, blocking: true, compacting: true);
            GC.WaitForPendingFinalizers();
            GC.Collect(2, GCCollectionMode.Aggressive, blocking: true, compacting: true);

            TrimWorkingSet();
        }
        catch (Exception) { /* never fail an export over hygiene */ }
    }

    /// <summary>Platform best-effort working-set trim. Windows asks psapi to
    /// evict the process's own resident set; Linux (glibc) releases free
    /// allocator arenas back to the OS via malloc_trim. Load-time guarded,
    /// failure-tolerant — hygiene must never fail an export.</summary>
    private static void TrimWorkingSet()
    {
        try
        {
            if (OperatingSystem.IsWindows())
            {
                EmptyWorkingSet(_selfProcess.Handle);
            }
            else if (OperatingSystem.IsLinux())
            {
                _ = malloc_trim(nint.Zero);
            }
        }
        catch (DllNotFoundException) { }   // exotic libc / stripped image
        catch (EntryPointNotFoundException) { }   // musl et al.
        catch (Exception) { }   // never fail an export over hygiene
    }

    [DllImport("psapi.dll", SetLastError = true)]
    private static extern bool EmptyWorkingSet(nint hProcess);

    // libc malloc_trim — glibc only; other libcs land in the guarded catches.
    [DllImport("libc")]
    private static extern int malloc_trim(nint pad);

    private sealed class MemoryLimitExceededException(string message) : Exception(message);

    /// <summary>
    /// Shared bulk-export driver for HandleExport / HandleExportFolder: asset
    /// paths are processed in modest batches (see ExportBatchSize), gated per
    /// asset, and closed with a collect-and-trim barrier. Returns the counts
    /// and lists the handlers turn into their own export_done payloads.
    /// </summary>
    /// <param name="failOnUnsupported">true for the named-paths export, where
    /// "loaded but nothing exportable" reads as a per-path failure; false for
    /// folder exports, which stay silent about such paths.</param>
    /// <param name="includeExceptionType">Each handler's historical failure
    /// message shape: the named export reports the sanitized message, the
    /// folder export prefixes the exception type name.</param>
    private static (int Done, List<string> Succeeded, List<object> Failed) RunBatchedExport(
        List<string> assetPaths, string outputDir, Dictionary<string, bool> formats,
        ConcurrentSet visited, bool failOnUnsupported, bool includeExceptionType)
    {
        var succeeded = new ConcurrentQueue<string>();
        var failed = new ConcurrentQueue<object>();
        var pacer = new ProgressPacer();
        int done = 0;

        for (var offset = 0; offset < assetPaths.Count; offset += ExportBatchSize)
        {
            if (_cts.Token.IsCancellationRequested) break;

            var batch = assetPaths.GetRange(offset,
                Math.Min(ExportBatchSize, assetPaths.Count - offset));

            try
            {
                // Wiring the token into ParallelOptions makes the scheduler stop
                // taking new iterations the moment cancel lands — previously the
                // whole 64-asset batch was drained one early-return at a time.
                // Iterations already in flight still finish (CUE4Parse's
                // LoadPackage is not interruptible); unstarted work is discarded.
                Parallel.ForEach(batch, new ParallelOptions
                {
                    MaxDegreeOfParallelism = EffectiveDop(),
                    CancellationToken = _cts.Token,
                },
                assetPath =>
                {
                    if (_cts.Token.IsCancellationRequested) return;
                    var d = Interlocked.Increment(ref done);
                    pacer.Emit(d, assetPaths.Count, ProgressMessage(assetPath));

                    try
                    {
                        WaitUnderMemoryGate(_cts.Token);
                        var exported = ExportAsset(assetPath, outputDir, formats, visited);
                        if (exported)
                            succeeded.Enqueue(assetPath);
                        else if (failOnUnsupported)
                            failed.Enqueue(new { path = assetPath, error = "Unsupported asset type or no exportable data" });
                    }
                    catch (Exception ex)
                    {
                        failed.Enqueue(new
                        {
                            path = assetPath,
                            error = includeExceptionType
                                ? $"{ex.GetType().Name}: {SanitizeMessage(ex.Message)}"
                                : SanitizeMessage(ex.Message),
                        });
                    }
                });
            }
            catch (OperationCanceledException)
            {
                // Cancel landed mid-batch — partial results stand and no further
                // batches are scheduled; the post-export reclaim frees buffers.
            }

            if (_cts.Token.IsCancellationRequested) break;

            CollectAndTrimBetweenBatches();
        }

        return (done, succeeded.ToList(), failed.ToList());
    }

    /// <summary>Progress label for an in-flight asset; carries live usage when
    /// a budget is active so the GUI progress bar doubles as a memory gauge.</summary>
    private static string ProgressMessage(string assetPath)
    {
        var name = Path.GetFileName(assetPath);
        return _memoryBudgetBytes <= 0
            ? $"Exporting: {name}"
            : $"Exporting: {name} ({FormatByteAmount(CurrentWorkingSetBytes())} / {FormatByteAmount(_memoryBudgetBytes)})";
    }

    private static Task HandleExport(JObject cmd)
    {
        // Format/policy fields all parse before the provider check so the
        // parent sees its settings echoed (memory notice) even when the
        // command then fails the "not initialized" check.
        _exportThreads = ResolveExportThreads(cmd);
        ResolveMemoryPolicy(cmd);
        EnsureProvider();
        var paths = cmd["paths"]?.ToObject<List<string>>() ?? throw new ArgumentException("paths required");
        var outputDir = cmd.Value<string>("output_dir") ?? throw new ArgumentException("output_dir required");
        var formats = cmd["formats"]?.ToObject<Dictionary<string, bool>>() ?? new Dictionary<string, bool>
        {
            ["mesh"] = true,
            ["texture"] = true,
            ["props"] = true,
            ["animation"] = true,
            ["audio"] = true
        };
        _textureFormat = cmd.Value<string>("texture_format") ?? "png";
        _audioFormat = NormalizeAudioFormat(cmd.Value<string>("audio_format") ?? "wav");
        _meshFormat = (cmd.Value<string>("mesh_format") ?? "psk").ToLowerInvariant() == "gltf" ? "gltf" : "psk";
        _webpLossless = !string.Equals(cmd.Value<string>("webp_mode"), "lossy", StringComparison.OrdinalIgnoreCase);
        var webpQ1 = cmd.Value<int?>("webp_quality");
        if (webpQ1 is >= 1 and <= 100) _webpQuality = webpQ1.Value;
        if (cmd["gpu"] is { } gpuFlag)
            _gpuDecode = gpuFlag.Type == JTokenType.Boolean && !gpuFlag.Value<bool>() ? false : true;

        Directory.CreateDirectory(outputDir);

        var visited = new ConcurrentSet();
        _exportWarningCount = 0;
        int total = paths.Count;

        Respond(new { type = "progress", current = 0, total, message = $"Found {total} assets to export" });

        var (done, succeeded, failed) = RunBatchedExport(
            paths, outputDir, formats, visited,
            failOnUnsupported: true, includeExceptionType: false);

        var cancelled = _cts.Token.IsCancellationRequested;
        Respond(new
        {
            type = "export_done",
            succeeded,
            failed,
            total = paths.Count,
            cancelled,
            remaining = cancelled ? total - done : 0,
            warning_count = _exportWarningCount,
        });
        return Task.CompletedTask;
    }

    // ─── Export folder (bulk) ─────────────────────────────────────────
    private static Task HandleExportFolder(JObject cmd)
    {
        // Format/policy fields parse before the provider check — see HandleExport.
        _exportThreads = ResolveExportThreads(cmd);
        ResolveMemoryPolicy(cmd);
        EnsureProvider();
        var vfsPath = (cmd.Value<string>("path") ?? "").TrimStart('/');
        var outputDir = cmd.Value<string>("output_dir") ?? throw new ArgumentException("output_dir required");
        var formats = cmd["formats"]?.ToObject<Dictionary<string, bool>>() ?? new Dictionary<string, bool>
        {
            ["mesh"] = true,
            ["texture"] = true,
            ["props"] = true,
            ["animation"] = true,
            ["audio"] = true
        };
        _textureFormat = cmd.Value<string>("texture_format") ?? "png";
        _audioFormat = NormalizeAudioFormat(cmd.Value<string>("audio_format") ?? "wav");
        _meshFormat = (cmd.Value<string>("mesh_format") ?? "psk").ToLowerInvariant() == "gltf" ? "gltf" : "psk";
        _webpLossless = !string.Equals(cmd.Value<string>("webp_mode"), "lossy", StringComparison.OrdinalIgnoreCase);
        var webpQ2 = cmd.Value<int?>("webp_quality");
        if (webpQ2 is >= 1 and <= 100) _webpQuality = webpQ2.Value;
        if (cmd["gpu"] is { } gpuFlag)
            _gpuDecode = gpuFlag.Type == JTokenType.Boolean && !gpuFlag.Value<bool>() ? false : true;

        Directory.CreateDirectory(outputDir);

        // Find all assets under this VFS path (including raw audio files)
        var prefix = string.IsNullOrEmpty(vfsPath) ? "" : vfsPath + "/";
        var exportableExtensions = new[] { ".uasset", ".umap", ".upk", ".wem", ".ewem", ".bnk" };
        var assetPaths = _provider!.Files
            .Where(f => prefix.Length == 0 || f.Value.Path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
            .Select(f => f.Value.Path)
            .Where(p => exportableExtensions.Any(ext => p.EndsWith(ext, StringComparison.OrdinalIgnoreCase)))
            .ToList();

        // Also include loose files (not in provider)
        foreach (var (loosePath, _) in _looseFiles)
        {
            if (prefix.Length > 0 && !loosePath.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                continue;
            if (exportableExtensions.Any(ext => loosePath.EndsWith(ext, StringComparison.OrdinalIgnoreCase)))
                assetPaths.Add(loosePath);
        }

        // A provider path and a loose file can name the same package; two export
        // workers racing on one path would write the same output simultaneously,
        // so dedupe now (the visited set only guards dependency re-exports).
        assetPaths = assetPaths.Distinct(StringComparer.OrdinalIgnoreCase).ToList();
        var visited = new ConcurrentSet();
        _exportWarningCount = 0;
        int total = assetPaths.Count;

        Respond(new { type = "progress", current = 0, total, message = $"Found {total} assets to export" });

        var (done, succeeded, failed) = RunBatchedExport(
            assetPaths, outputDir, formats, visited,
            failOnUnsupported: false, includeExceptionType: true);

        var cancelled = _cts.Token.IsCancellationRequested;
        Respond(new
        {
            type = "export_done",
            succeeded,
            failed,
            total = assetPaths.Count,
            cancelled,
            remaining = cancelled ? total - done : 0,
            warning_count = _exportWarningCount,
        });
        return Task.CompletedTask;
    }

    // ─── Core export logic ────────────────────────────────────────────

    /// <summary>
    /// One-add-only set with package paths already exported by the current
    /// command. Replaces the HashSet the sequential exporters used so many
    /// parallel workers can dedupe against each other without a global lock.
    /// </summary>
    private sealed class ConcurrentSet
    {
        private readonly ConcurrentDictionary<string, bool> _dict =
            new(StringComparer.OrdinalIgnoreCase);
        public bool Add(string item) => _dict.TryAdd(item, false);
    }

    // Meshes reference materials in other packages, materials reference parent
    // materials and textures. Exporting one mesh must pull that whole chain or
    // the Python scan pipeline finds a props file pointing at assets that were
    // never extracted. Depth 8 outlasts any real parent chain; the visited set
    // is the actual cycle guard.
    private const int MaxDependencyDepth = 8;

    private static readonly Regex _depRefRe = new(@"^(?:Material\w*|Texture2D)'", RegexOptions.Compiled);

    private static bool ExportAsset(string gamePath, string outputDir, Dictionary<string, bool> formats,
                                    ConcurrentSet? visited = null, int depth = 0)
    {
        // Build nested output directory preserving the game path structure.
        // gamePath is parent-supplied — SafeJoin rejects "../" traversal.
        var assetDir = Path.GetDirectoryName(gamePath.Replace('/', Path.DirectorySeparatorChar)) ?? "";
        var nestedOutputDir = SafeJoin(outputDir, assetDir);
        Directory.CreateDirectory(nestedOutputDir);

        // For non-UE package files (.wem, .bnk, .bin, etc.), do raw binary export
        var lowerPath = gamePath.ToLowerInvariant();
        var isPackage = lowerPath.EndsWith(".uasset") || lowerPath.EndsWith(".umap") || lowerPath.EndsWith(".upk");
        if (!isPackage)
        {
            return ExportRawFile(gamePath, nestedOutputDir);
        }

        // Strip package extension for CUE4Parse — it uses extensionless game paths
        // NOTE: .upk is NOT stripped because FindGameFile only tries .uasset/.umap
        // — the provider auto-registers .upk with its full path so exact match works
        var objectPath = gamePath;
        foreach (var ext in new[] { ".uasset", ".umap" })
        {
            if (objectPath.EndsWith(ext, StringComparison.OrdinalIgnoreCase))
            {
                objectPath = objectPath[..^ext.Length];
                break;
            }
        }

        // One export per package per command — dependency-following would
        // otherwise re-export shared materials/textures for every mesh that
        // references them.
        visited ??= new ConcurrentSet();
        if (!visited.Add(objectPath))
            return true;

        // Load the package and resolve all exports
        var exports = new List<UObject>();
        try
        {
            var pkg = _provider!.LoadPackage(objectPath);
            if (pkg != null)
            {
                foreach (var obj in pkg.GetExports())
                {
                    try { exports.Add(obj); }
                    catch { /* skip unresolvable */ }
                }
            }
        }
        catch when (lowerPath.EndsWith(".upk"))
        {
            // UE3 .upk packages may not be parseable by CUE4Parse — fall back to raw binary export
            return ExportRawFile(gamePath, nestedOutputDir);
        }

        if (exports.Count == 0 && lowerPath.EndsWith(".upk"))
        {
            // No exports found — fall back to raw copy
            return ExportRawFile(gamePath, nestedOutputDir);
        }

        bool exported = false;
        var refPaths = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        foreach (var obj in exports)
        {
            try
            {
                switch (obj)
                {
                    case UStaticMesh sm when formats.GetValueOrDefault("mesh", true):
                        exported |= ExportMesh(sm, nestedOutputDir);
                        break;

                    case USkeletalMesh sk when formats.GetValueOrDefault("mesh", true):
                        exported |= ExportMesh(sk, nestedOutputDir);
                        break;

                    case UTexture2D tex when formats.GetValueOrDefault("texture", true):
                        exported |= ExportTexture(tex, nestedOutputDir);
                        break;

                    case UAnimSequence anim when formats.GetValueOrDefault("animation", true):
                        exported |= ExportAnimation(anim, nestedOutputDir);
                        break;

                    case USoundWave sound when formats.GetValueOrDefault("audio", true):
                        exported |= ExportAudio(sound, nestedOutputDir);
                        break;

                    case UMaterialInstance mat when formats.GetValueOrDefault("props", true):
                        exported |= ExportProps(mat, nestedOutputDir);
                        break;
                }

                // FileMediaSource isn't a typed CUE4Parse UObject we can match
                // via `case U…`, but its export_type is stable. Resolve the
                // embedded FilePath against the VFS and write the bytes flat.
                if (obj.ExportType == "FileMediaSource" &&
                    formats.GetValueOrDefault("video", true))
                {
                    exported |= ExportFileMediaSource(gamePath, nestedOutputDir);
                }
            }
            catch (Exception ex)
            {
                Interlocked.Increment(ref _exportWarningCount);
                Respond(new { type = "warning", message = $"{obj.Name} ({obj.ExportType}): {DescribeException(ex)}" });
            }

            // Serialize once — the JSON feeds both the props file and the
            // dependency walk below.
            string? propsJson = null;
            var followRefs = depth < MaxDependencyDepth &&
                             (obj is UStaticMesh || obj is USkeletalMesh || obj is UMaterialInterface);
            var wantProps = formats.GetValueOrDefault("props", true);
            if (followRefs || wantProps)
            {
                try { propsJson = RunSerializeOnBoundedThread(() => SerializeUObject(obj), 15_000); }
                catch (Exception ex)
                {
                    Respond(new { type = "warning", message = $"Props serialize failed for {obj.Name}: {SanitizeMessage(ex.Message)}" });
                }
            }

            // Export props for every object regardless of type
            if (wantProps)
            {
                exported |= ExportGenericProps(obj, nestedOutputDir, propsJson);
            }

            if (followRefs && propsJson != null)
                CollectAssetRefs(propsJson, refPaths);
        }

        if (refPaths.Count > 0)
            ExportDependencies(refPaths, outputDir, formats, visited, depth + 1);

        if (!exported && exports.Count > 0)
        {
            var typeList = string.Join(", ", exports.Select(o => o.ExportType).Distinct());
            Respond(new { type = "warning", message = $"{Path.GetFileName(gamePath)}: loaded {exports.Count} object(s) [{typeList}] but none were exportable — check if these types need special handling" });
        }

        return exported;
    }

    // ─── Raw binary export (for .wem, .bnk, etc.) ────────────────────
    private static bool ExportRawFile(string gamePath, string nestedOutputDir)
    {
        // Resolve the payload source: provider entry first, loose file fallback.
        _provider!.TryGetGameFile(gamePath, out var providerFile);
        _looseFiles.TryGetValue(gamePath, out var looseDiskPath);
        var looseExists = looseDiskPath != null && File.Exists(looseDiskPath!);
        if (providerFile == null && !looseExists) return false;

        // Large payloads — whole videos, bulk .wem — stream straight to disk.
        // Holding a 2 GB cutscene in one byte[] is exactly what the memory
        // cap exists to prevent; small files keep the simple array path.
        var sourceBytes = providerFile?.Size
                          ?? (looseExists ? new FileInfo(looseDiskPath!).Length : 0);
        if (sourceBytes >= LargeRawFileBytes)
            return ExportLargeRawFileStreaming(providerFile, looseExists ? looseDiskPath : null,
                                               gamePath, nestedOutputDir);

        byte[]? data = null;

        if (providerFile != null)
        {
            // Same semantics as TrySaveAsset(GameFile) — SafeRead is null on
            // unreadable entries, which falls through to the loose check.
            data = providerFile.SafeRead();
            if (data == null && !looseExists) return false;
        }
        if (data == null)
        {
            data = File.ReadAllBytes(looseDiskPath!);
        }

        var name = Path.GetFileName(gamePath);
        var lowerName = name.ToLowerInvariant();

        // Convert .wem files to the target audio format via vgmstream
        if (lowerName.EndsWith(".wem"))
        {
            EnsureWemNameMap();

            // Rename using the debug name from the Wwise event hierarchy
            var wemId = Path.GetFileNameWithoutExtension(name);
            var baseName = _wemIdToName.TryGetValue(wemId, out var debugName)
                ? $"{debugName}_{wemId}"
                : wemId;

            var wemPath = SafeJoin(nestedOutputDir, $"{baseName}.wem");
            File.WriteAllBytes(wemPath, data);
            var converted = ConvertWithVgmstream(wemPath, nestedOutputDir, _audioFormat);
            if (converted)
            {
                try { File.Delete(wemPath); } catch { }
                return true;
            }
            // Keep the .wem as fallback if conversion failed
            return true;
        }

        // Try converting .ewem files — some are just renamed .wem (not actually encrypted)
        if (lowerName.EndsWith(".ewem"))
        {
            var ewemBase = Path.GetFileNameWithoutExtension(name);
            var wemPath = SafeJoin(nestedOutputDir, $"{ewemBase}.wem");
            File.WriteAllBytes(wemPath, data);
            var converted = ConvertWithVgmstream(wemPath, nestedOutputDir, _audioFormat);
            if (converted)
            {
                try { File.Delete(wemPath); } catch { }
                return true;
            }
            // Conversion failed — likely encrypted. Keep as .wem for manual handling.
            Respond(new { type = "warning", message = $"{name}: possibly encrypted audio, conversion failed" });
            return true;
        }

        var outPath = SafeJoin(nestedOutputDir, name);
        File.WriteAllBytes(outPath, data);
        return true;
    }

    /// <summary>
    /// Stream a large raw payload straight to disk — O(1 MB) copy buffers,
    /// never the whole file resident in managed memory. Mirrors the byte-array
    /// path's .wem/.ewem post-processing exactly.
    /// </summary>
    private static bool ExportLargeRawFileStreaming(GameFile? providerFile, string? loosePath,
                                                    string gamePath, string nestedOutputDir)
    {
        bool WriteTo(string outPath)
        {
            if (providerFile != null)
                return StreamGameFileTo(providerFile, outPath);
            if (loosePath == null) return false;

            try
            {
                using var src = File.OpenRead(loosePath);
                using var fs = File.Create(outPath);
                src.CopyTo(fs, 1 << 20);
                return true;
            }
            catch
            {
                // A half-written stream is worse than no file — remove the
                // partial so a later retry starts fresh.
                try { File.Delete(outPath); } catch { }
                return false;
            }
        }

        var name = Path.GetFileName(gamePath);
        var lowerName = name.ToLowerInvariant();

        if (lowerName.EndsWith(".wem"))
        {
            EnsureWemNameMap();

            var wemId = Path.GetFileNameWithoutExtension(name);
            var baseName = _wemIdToName.TryGetValue(wemId, out var debugName)
                ? $"{debugName}_{wemId}"
                : wemId;

            var wemPath = SafeJoin(nestedOutputDir, $"{baseName}.wem");
            if (!WriteTo(wemPath)) return false;
            var converted = ConvertWithVgmstream(wemPath, nestedOutputDir, _audioFormat);
            if (converted)
            {
                try { File.Delete(wemPath); } catch { }
            }
            // Keep the .wem as fallback if conversion failed
            return true;
        }

        if (lowerName.EndsWith(".ewem"))
        {
            var ewemBase = Path.GetFileNameWithoutExtension(name);
            var wemPath = SafeJoin(nestedOutputDir, $"{ewemBase}.wem");
            if (!WriteTo(wemPath)) return false;
            var converted = ConvertWithVgmstream(wemPath, nestedOutputDir, _audioFormat);
            if (converted)
            {
                try { File.Delete(wemPath); } catch { }
                return true;
            }
            // Conversion failed — likely encrypted. Keep as .wem for manual handling.
            Respond(new { type = "warning", message = $"{name}: possibly encrypted audio, conversion failed" });
            return true;
        }

        var outPath = SafeJoin(nestedOutputDir, name);
        return WriteTo(outPath);
    }

    /// <summary>Stream one provider GameFile to *outPath* through its FArchive
    /// reader (a Stream subclass). False (file untouched) when the payload
    /// can't be opened — callers fall back to the buffered path.</summary>
    private static bool StreamGameFileTo(GameFile file, string outPath)
    {
        try
        {
            using var reader = file.SafeCreateReader();
            if (reader == null) return false;
            using var fs = File.Create(outPath);
            reader.CopyTo(fs, 1 << 20);
            return true;
        }
        catch
        {
            // A half-written stream is worse than no file.
            try { File.Delete(outPath); } catch { }
            return false;
        }
    }

    /// <summary>Stream a provider-side payload at/above <see cref="LargeRawFileBytes"/>
    /// to *outPath*. Returns false without touching the file when the entry is
    /// smaller, missing, or unreadable — callers then use the buffered path.</summary>
    private static bool StreamProviderFileTo(string vfsPath, string outPath)
    {
        try
        {
            if (!_provider!.TryGetGameFile(vfsPath, out var file) || file == null) return false;
            if (file.Size < LargeRawFileBytes) return false;
            return StreamGameFileTo(file, outPath);
        }
        catch
        {
            try { File.Delete(outPath); } catch { }
            return false;
        }
    }

    // ─── FileMediaSource export ───────────────────────────────────────
    private static bool ExportFileMediaSource(string gamePath, string outputDir)
    {
        var resolved = ResolveFileMediaSource(gamePath, out var err);
        if (resolved == null)
        {
            Respond(new { type = "warning", message = $"{Path.GetFileName(gamePath)}: {err ?? "FileMediaSource resolve failed"}" });
            return false;
        }
        var name = SanitizeName(Path.GetFileName(resolved));
        if (string.IsNullOrEmpty(name)) return false;
        var outPath = SafeJoin(outputDir, name);

        // Stream large payloads (videos) straight to disk before any buffering
        // happens — a 2 GB cutscene must never be resident as one byte[].
        // StreamProviderFileTo declines small entries, so the buffered write
        // below stays the default path for normal assets.
        if (StreamProviderFileTo(resolved, outPath)) return true;

        if (!_provider!.TrySaveAsset(resolved, out var data) || data == null)
        {
            Respond(new { type = "warning", message = $"{Path.GetFileName(gamePath)}: resolved {resolved} not in VFS" });
            return false;
        }
        File.WriteAllBytes(outPath, data);
        return true;
    }

    // ─── Mesh export ──────────────────────────────────────────────────
    private static bool ExportMesh(UObject mesh, string outputDir)
    {
        // psk → ActorX binaries (.psk/.pskx); gltf → glTF 2.0 binary (.glb) via
        // CUE4Parse-Conversion's SharpGLTF path (geometry, bones, weights,
        // morphs; default-color materials — textures are exported separately).
        var options = new ExporterOptions
        {
            MeshFormat = _meshFormat == "gltf" ? EMeshFormat.Gltf2 : EMeshFormat.ActorX,
        };
        MeshExporter exporter;
        if (mesh is UStaticMesh staticMesh)
            exporter = new MeshExporter(staticMesh, options);
        else if (mesh is USkeletalMesh skelMesh)
            exporter = new MeshExporter(skelMesh, options);
        else
            return false;

        if (exporter.MeshLods.Count == 0)
            return false;

        bool any = false;
        for (int i = 0; i < exporter.MeshLods.Count; i++)
        {
            var lod = exporter.MeshLods[i];
            var ext = Path.GetExtension(lod.FileName);
            var name = exporter.MeshLods.Count > 1
                ? $"{mesh.Name}_LOD{i}{ext}"
                : $"{mesh.Name}{ext}";
            var outPath = SafeJoin(outputDir, name);
            File.WriteAllBytes(outPath, lod.FileData);
            any = true;
        }
        return any;
    }

    // ─── Texture export ───────────────────────────────────────────────
    private static bool ExportTexture(UTexture2D texture, string outputDir)
    {
        // Decode() now yields a CTexture (raw pixels + format) rather than an
        // SKBitmap. Convert once here so the TGA/PNG writers below stay as they
        // were rather than growing a second pixel-format path.
        // GPU decode: when enabled (initialized by init/export "gpu" flag),
        // ask the Vulkan-based decoder first; it returns null (with a reason
        // logged to stderr) for anything it can't handle — virtual textures,
        // volumetrics, unsupported formats, per-texture GPU errors — and the
        // CPU path below takes over.
        CTexture? raw;
        if (_gpuDecode)
        {
            raw = GpuTextureDecoder.TryDecode(texture, out _);
            if (raw == null)
                raw = texture.Decode();
        }
        else
        {
            raw = texture.Decode();
        }
        if (raw == null) return false;
        // SKBitmap/SKData wrap native Skia memory that the GC never sees. Without
        // explicit Dispose the native buffers only get freed when a gen2 GC runs
        // one of SKObject's finalizers — which never happens during a texture-heavy
        // export (the managed heap stays tiny), so RSS grows unbounded at
        // ~16 MB / 2K texture, ~65 MB / 4K texture until the process OOMs.
        using var decoded = TextureEncoder.ToSkBitmap(raw);
        if (decoded == null) return false;

        if (_textureFormat == "tga")
        {
            // Export as raw BGRA TGA
            var outPath = SafeJoin(outputDir, $"{texture.Name}.tga");
            WriteTga(decoded, outPath);
        }
        else if (_textureFormat == "webp")
        {
            var outPath = SafeJoin(outputDir, $"{texture.Name}.webp");
            var wrotePixels = false;
            try
            {
                // Lossless keeps pixels bit-exact (default); lossy trades a
                // tiny quality loss for a much smaller file, quality 1-100.
                using var pixels = decoded.PeekPixels();
                if (pixels != null)
                {
                    var mode = _webpLossless
                        ? new SkiaSharp.SKWebpEncoderOptions(
                            SkiaSharp.SKWebpEncoderCompression.Lossless, 100)
                        : new SkiaSharp.SKWebpEncoderOptions(
                            SkiaSharp.SKWebpEncoderCompression.Lossy, _webpQuality);
                    using var data = pixels.Encode(mode);
                    if (data != null && data.Size > 0)
                    {
                        using var fs = File.Create(outPath);
                        data.SaveTo(fs);
                        wrotePixels = true;
                    }
                }
            }
            finally
            {
                if (!wrotePixels)
                {
                    // Non-raster color type or encoder failure — the generic
                    // Skia image-encode path is still correct, just slower.
                    using var webpData = decoded.Encode(SkiaSharp.SKEncodedImageFormat.Webp, _webpQuality);
                    using var fs = File.Create(outPath);
                    webpData.SaveTo(fs);
                }
            }
        }
        else
        {
            var outPath = SafeJoin(outputDir, $"{texture.Name}.png");
            var wrotePixels = false;
            try
            {
                // Fast PNG: SKPngEncoderOptions lets us pick a cheap zlib level.
                // The generic Encode(format, 100) path (quality is ignored for
                // PNG) runs zlib at its default level (~6) over all five row
                // filters — 3-4x the CPU time of level 1 + the cheap Sub
                // filter, for output roughly 10-20% larger. Texture-heavy
                // batches are encode-bound, so the tradeoff pays off heavily
                // (fast PNG is the agreed default).
                using var pixels = decoded.PeekPixels();
                if (pixels != null)
                {
                    using var data = pixels.Encode(new SkiaSharp.SKPngEncoderOptions(
                        SkiaSharp.SKPngEncoderFilterFlags.Sub, 1));
                    if (data != null && data.Size > 0)
                    {
                        using var fs = File.Create(outPath);
                        data.SaveTo(fs);
                        wrotePixels = true;
                    }
                }
            }
            finally
            {
                if (!wrotePixels)
                {
                    // Non-raster color type or encoder failure — the old
                    // generic path is still correct, just slower.
                    using var pngData = decoded.Encode(SkiaSharp.SKEncodedImageFormat.Png, 100);
                    using var fs = File.Create(outPath);
                    pngData.SaveTo(fs);
                }
            }
        }
        return true;
    }

    private static void WriteTga(SkiaSharp.SKBitmap bmp, string outPath)
    {
        int w = bmp.Width, h = bmp.Height;
        // 1 MB buffer: the pixel block write below lands on the OS in a few
        // big chunks instead of streaming multi-MB driver buffer arrays
        // straight to the pipe (relevant on Windows named-pipe flushes and
        // Linux socket-backed pipes alike).
        using var fs = new BufferedStream(File.Create(outPath), 1024 * 1024);
        // TGA header (18 bytes)
        var header = new byte[18];
        header[2] = 2;  // Uncompressed true-color
        header[12] = (byte)(w & 0xFF);
        header[13] = (byte)((w >> 8) & 0xFF);
        header[14] = (byte)(h & 0xFF);
        header[15] = (byte)((h >> 8) & 0xFF);
        header[16] = 32; // bits per pixel
        header[17] = 0x28; // top-left origin + 8 alpha bits
        fs.Write(header);

        // Skia's default 8888 layout is BGRA on little-endian platforms,
        // matching the TGA output we want, so we can write the backing buffer
        // directly. (Copies bmp.Bytes once — inherent to the SKBitmap wrap.)
        if (bmp.ColorType == SkiaSharp.SKColorType.Bgra8888)
        {
            fs.Write(bmp.Bytes);
            return;
        }

        // Rgba8888: swap R<->B per pixel into a scratch buffer with plain
        // 4-byte-group loops. The old code fell through here to a
        // per-pixel GetPixel+WriteByte fallback — two bounds-checked native
        // calls per pixel, minutes per 4K texture within a batch of
        // thousands. Green/alpha bytes pass through untouched.
        if (bmp.ColorType == SkiaSharp.SKColorType.Rgba8888)
        {
            var src = bmp.Bytes;
            var scratch = System.Buffers.ArrayPool<byte>.Shared.Rent(src.Length);
            try
            {
                for (int i = 0; i + 3 < src.Length; i += 4)
                {
                    scratch[i] = src[i + 2];
                    scratch[i + 1] = src[i + 1];
                    scratch[i + 2] = src[i];
                    scratch[i + 3] = src[i + 3];
                }
                fs.Write(scratch, 0, src.Length);
            }
            finally
            {
                System.Buffers.ArrayPool<byte>.Shared.Return(scratch);
            }
            return;
        }

        // Unexpected color type — keep the exact per-pixel read path so the
        // output stays correct, per-pixel cost is irrelevant at this rarity.
        for (int y = 0; y < h; y++)
        {
            for (int x = 0; x < w; x++)
            {
                var c = bmp.GetPixel(x, y);
                fs.WriteByte(c.Blue);
                fs.WriteByte(c.Green);
                fs.WriteByte(c.Red);
                fs.WriteByte(c.Alpha);
            }
        }
    }

    // ─── Animation export ─────────────────────────────────────────────
    private static bool ExportAnimation(UAnimSequence anim, string outputDir)
    {
        var options = new ExporterOptions();
        var exporter = new AnimExporter(anim, options);
        if (exporter.AnimSequences.Count == 0) return false;

        bool any = false;
        for (int i = 0; i < exporter.AnimSequences.Count; i++)
        {
            var seq = exporter.AnimSequences[i];
            var ext = Path.GetExtension(seq.FileName);
            var name = exporter.AnimSequences.Count > 1
                ? $"{anim.Name}_{i}{ext}"
                : $"{anim.Name}{ext}";
            var outPath = SafeJoin(outputDir, name);
            File.WriteAllBytes(outPath, seq.FileData);
            any = true;
        }
        return any;
    }

    // ─── Audio export ─────────────────────────────────────────────────
    private static bool ExportAudio(USoundWave sound, string outputDir)
    {
        sound.Decode(true, out var format, out var data);
        if (data == null || data.Length == 0)
            return false;

        var isNative = format switch
        {
            "OGG" or "OPUS" => true,
            "WAV" or "ADPCM" or "PCM" => true,
            _ => false
        };

        if (isNative)
        {
            // Already decoded — save in target format (CUE4Parse decoded to WAV/OGG)
            var nativeExt = format switch
            {
                "OGG" or "OPUS" => "ogg",
                _ => "wav"
            };

            // MP3/WebM targets re-encode through ffmpeg (same chain as the
            // WEM path); without ffmpeg the native decode stays as the
            // playable fallback.
            if (_audioFormat is "mp3" or "webm")
            {
                InitFfmpeg();
                if (_ffmpegPath != null)
                {
                    var nativeTempPath = SafeJoin(outputDir, $"{sound.Name}.{nativeExt}");
                    File.WriteAllBytes(nativeTempPath, data);
                    var targetPath = SafeJoin(outputDir, $"{sound.Name}.{_audioFormat}");
                    if (EncodeWithFfmpeg(nativeTempPath, targetPath, _audioFormat))
                    {
                        try { File.Delete(nativeTempPath); } catch { }
                        return true;
                    }
                    // Encode failed — keep the native file.
                    return true;
                }
                Respond(new { type = "warning", message = "ffmpeg unavailable; exporting native audio instead of " + _audioFormat });
            }

            var outPath = SafeJoin(outputDir, $"{sound.Name}.{nativeExt}");
            File.WriteAllBytes(outPath, data);
            return true;
        }

        // WEM / BINKA / unknown — save temp file and convert via vgmstream
        var tempExt = format switch
        {
            "WEM" => "wem",
            "BINKA" => "binka",
            _ => "bin"
        };
        var tempPath = SafeJoin(outputDir, $"{sound.Name}.{tempExt}");
        File.WriteAllBytes(tempPath, data);

        var converted = ConvertWithVgmstream(tempPath, outputDir, _audioFormat);
        if (converted)
        {
            try { File.Delete(tempPath); } catch { }
            return true;
        }
        // Keep the unconverted file as fallback
        return true;
    }

    // ─── Props export ─────────────────────────────────────────────────
    private static readonly Newtonsoft.Json.JsonSerializerSettings _propsSettings = new()
    {
        Formatting = Formatting.Indented,
        ReferenceLoopHandling = ReferenceLoopHandling.Ignore,
        MaxDepth = 64,
        // Pin TypeNameHandling.None so adversarial $type tokens cannot drive
        // the serializer to instantiate arbitrary CLR types.
        TypeNameHandling = TypeNameHandling.None,
        Error = (_, args) => args.ErrorContext.Handled = true
    };

    /// <summary>Serialize a UObject to JSON, including all typed fields (not just .Properties).</summary>
    private static string SerializeUObject(UObject obj)
    {
        // Newtonsoft on the full UObject captures all deserialized C# fields
        // (.Properties alone is often empty for typed exports like UStaticMesh)
        var json = JsonConvert.SerializeObject(obj, _propsSettings);
        return json;
    }

    private static bool ExportProps(UMaterialInstance mat, string outputDir)
    {
        var propsJson = RunSerializeOnBoundedThread(() => SerializeUObject(mat), 15_000);
        if (propsJson == null) return false;

        var outPath = SafeJoin(outputDir, $"{mat.Name}.props.txt");
        File.WriteAllText(outPath, propsJson);
        return true;
    }

    private static bool ExportGenericProps(UObject obj, string outputDir, string? propsJson)
    {
        if (propsJson == null) return false;
        try
        {
            var outPath = SafeJoin(outputDir, $"{obj.Name}.props.txt");
            if (!File.Exists(outPath))
                File.WriteAllText(outPath, propsJson);
            return true;
        }
        catch (Exception ex)
        {
            Respond(new { type = "warning", message = $"Props export failed for {obj.Name}: {SanitizeMessage(ex.Message)}" });
            return false;
        }
    }

    // ─── Dependency-following export ──────────────────────────────────

    /// <summary>
    /// Pull material and texture object references out of a serialized
    /// UObject. Matches ObjectName tags like Material'…',
    /// MaterialInstanceConstant'…', Texture2D'…' anywhere in the JSON tree —
    /// this covers StaticMaterials/SkeletalMaterials on meshes,
    /// TextureParameterValues and Parent on material instances, and
    /// CachedExpressionData.ReferencedTextures on cooked UE5 materials.
    /// </summary>
    private static void CollectAssetRefs(string propsJson, HashSet<string> refPaths)
    {
        JToken root;
        try { root = JToken.Parse(propsJson); }
        catch { return; }
        if (root is not JContainer container) return;

        foreach (var tok in container.DescendantsAndSelf())
        {
            if (tok is not JObject o) continue;
            var objName = o.Value<string>("ObjectName");
            var objPath = o.Value<string>("ObjectPath");
            if (string.IsNullOrEmpty(objName) || string.IsNullOrEmpty(objPath)) continue;
            if (_depRefRe.IsMatch(objName))
                refPaths.Add(objPath);
        }
    }

    private static void ExportDependencies(HashSet<string> refPaths, string outputDir,
        Dictionary<string, bool> formats, ConcurrentSet visited, int depth)
    {
        foreach (var refPath in refPaths)
        {
            if (_cts.Token.IsCancellationRequested) break;

            // ObjectPath is "<Pkg>/Path/Name.<exportIndex or SubObject>" —
            // strip the object part to get the package path CUE4Parse loads.
            var pkgPath = refPath.TrimStart('/');
            var slash = pkgPath.LastIndexOf('/');
            var dot = pkgPath.LastIndexOf('.');
            if (dot > slash) pkgPath = pkgPath[..dot];
            if (pkgPath.Length == 0) continue;

            try
            {
                ExportAsset(pkgPath + ".uasset", outputDir, formats, visited, depth);
            }
            catch (Exception ex)
            {
                Respond(new { type = "warning", message = $"Dependency {Path.GetFileName(pkgPath)}: {SanitizeMessage(ex.Message)}" });
            }
        }
    }

    // ─── Oodle Initialization ─────────────────────────────────────────
    // Shared-library names per OS (game ships, app cache). Windows games
    // carry the DLL next to the shipping binary; Linux native builds ship
    // the Oodle .so under Binaries/Linux.
    private static readonly string[] _oodleNames =
        OperatingSystem.IsWindows()
            ? new[] { "oo2core_9_win64.dll", "oodle-data-shared.dll" }
            : new[] { "liboodle-data-shared.so", "liboo2corelinux64.so", "liboo2corelinux64.so.9" };

    private static void InitOodle(string gameDir)
    {
        // 1. Search for an existing Oodle DLL near the game, the app, or already cached
        var searchDirs = new List<string>
        {
            AppContext.BaseDirectory,
            Directory.GetCurrentDirectory(),
            Path.Combine(Path.GetTempPath(), "CUE4ParseCLI_oodle")
        };

        // Walk up from game paks dir to find the game's Binaries
        // (e.g., …/Binaries/Win64/ or …/Binaries/Linux/ for native builds)
        try
        {
            var dir = new DirectoryInfo(gameDir);
            for (int i = 0; i < 5 && dir?.Parent != null; i++)
            {
                dir = dir.Parent;
                searchDirs.Add(dir.FullName);
                var bin64 = Path.Combine(dir.FullName, "Binaries", "Win64");
                if (Directory.Exists(bin64)) searchDirs.Add(bin64);
                var binLinux = Path.Combine(dir.FullName, "Binaries", "Linux");
                if (Directory.Exists(binLinux)) searchDirs.Add(binLinux);
            }
        }
        catch { }

        foreach (var d in searchDirs)
        {
            foreach (var name in _oodleNames)
            {
                var candidate = Path.Combine(d, name);
                if (File.Exists(candidate))
                {
                    var origin = SameOrUnder(d, AppContext.BaseDirectory) ? "<app-dir>" : "<game-dir>";
                    Respond(new { type = "info", message = $"Found Oodle in {origin}" });
                    LoadOodle(candidate);
                    return;
                }
            }
        }

        // 2. Try CUE4Parse's built-in download
        var cacheDir = Path.Combine(Path.GetTempPath(), "CUE4ParseCLI_oodle");
        Directory.CreateDirectory(cacheDir);
        try
        {
            var tempDll = Path.Combine(cacheDir, OodleHelper.OodleFileName);
            OodleHelper.DownloadOodleDllAsync(ref tempDll, CancellationToken.None)
                       .GetAwaiter().GetResult();
            if (File.Exists(tempDll) && new FileInfo(tempDll).Length > 0)
            {
                Respond(new { type = "info", message = "Oodle downloaded via CUE4Parse" });
                LoadOodle(tempDll);
                return;
            }
        }
        catch { }

        // 3. Manual download from OodleUE GitHub releases (hash-pinned per OS)
        var zipEntryName = OperatingSystem.IsWindows()
            ? "bin/oodle-data-shared.dll"
            : "lib/liboodle-data-shared.so";
        var fileName = Path.GetFileName(zipEntryName);
        var targetPath = Path.Combine(cacheDir, fileName);

        Respond(new { type = "info", message = "Downloading Oodle decompression library..." });
        try
        {
            // Download the zip into a memory stream so we can verify its hash
            // before trusting any of its contents.
            byte[] zipBytes;
            using (var http = new HttpClient())
            {
                http.Timeout = TimeSpan.FromSeconds(30);
                using var response = http.GetAsync(OodleZipUrlActive, HttpCompletionOption.ResponseHeadersRead).GetAwaiter().GetResult();
                response.EnsureSuccessStatusCode();
                zipBytes = response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult();
            }

            var expectedHash = OodleZipSha256Active;
            var actualHash = ComputeSha256Hex(zipBytes);
            if (!string.Equals(actualHash, expectedHash, StringComparison.OrdinalIgnoreCase))
            {
                Respond(new { type = "warning", message = $"Oodle download hash mismatch (expected {expectedHash}, got {actualHash}). Refusing to extract." });
                return;
            }

            using var zipStream = new MemoryStream(zipBytes, writable: false);
            using var zip = new ZipArchive(zipStream, ZipArchiveMode.Read);
            var entry = zip.GetEntry(zipEntryName)
                       ?? throw new FileNotFoundException($"{zipEntryName} not found in zip");
            // SafeJoin guards against malicious entry names ("../foo")
            var safePath = SafeJoin(cacheDir, fileName);
            using (var entryStream = entry.Open())
            using (var fs = File.Create(safePath))
            {
                entryStream.CopyTo(fs);
            }
            Respond(new { type = "info", message = $"Downloaded Oodle ({new FileInfo(safePath).Length} bytes)" });
            LoadOodle(safePath);
        }
        catch (Exception ex)
        {
            Respond(new { type = "warning", message = $"Oodle download failed: {SanitizeMessage(ex.Message)}. IoStore archives won't decompress." });
        }
    }

    /// <summary>True when <paramref name="path"/> is the same as or under <paramref name="other"/>.</summary>
    private static bool SameOrUnder(string path, string other)
    {
        try
        {
            var a = Path.GetFullPath(path).TrimEnd(Path.DirectorySeparatorChar);
            var b = Path.GetFullPath(other).TrimEnd(Path.DirectorySeparatorChar);
            return a.StartsWith(b, StringComparison.OrdinalIgnoreCase);
        }
        catch { return false; }
    }

    private static string ComputeSha256Hex(byte[] data)
    {
        var hash = System.Security.Cryptography.SHA256.HashData(data);
        var sb = new System.Text.StringBuilder(hash.Length * 2);
        foreach (var b in hash) sb.Append(b.ToString("x2"));
        return sb.ToString();
    }

    private static void LoadOodle(string path)
    {
        try
        {
            OodleHelper.Initialize(path);
            Respond(new { type = "info", message = "Oodle initialized" });
        }
        catch (Exception ex)
        {
            Respond(new { type = "warning", message = $"Oodle load failed: {ex.GetType().Name}: {SanitizeMessage(ex.Message)}" });
        }
    }

    // ─── vgmstream (WEM → WAV/OGG conversion) ────────────────────────

    private static void InitVgmstream()
    {
        if (_vgmstreamPath != null) return;

        var cacheDir = Path.Combine(Path.GetTempPath(), "CUE4ParseCLI_vgmstream");
        var exeName = OperatingSystem.IsWindows() ? "vgmstream-cli.exe" : "vgmstream-cli";
        var exePath = Path.Combine(cacheDir, exeName);

        if (File.Exists(exePath))
        {
            _vgmstreamPath = exePath;
            return;
        }

        Directory.CreateDirectory(cacheDir);
        Respond(new { type = "info", message = "Downloading vgmstream for audio conversion..." });

        try
        {
            // Pinned URL+hash: any drift in the upstream artifact is rejected.
            byte[] zipBytes;
            using (var http = new HttpClient())
            {
                http.Timeout = TimeSpan.FromSeconds(60);
                http.DefaultRequestHeaders.UserAgent.ParseAdd("CUE4ParseCLI/1.0");
                using var resp = http.GetAsync(VgmstreamZipUrlActive, HttpCompletionOption.ResponseHeadersRead).GetAwaiter().GetResult();
                resp.EnsureSuccessStatusCode();
                zipBytes = resp.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult();
            }

            var expectedVgmHash = VgmstreamZipSha256Active;
            var actualHash = ComputeSha256Hex(zipBytes);
            if (!string.Equals(actualHash, expectedVgmHash, StringComparison.OrdinalIgnoreCase))
            {
                Respond(new { type = "warning", message = $"vgmstream download hash mismatch (expected {expectedVgmHash}, got {actualHash}). Refusing to extract." });
                return;
            }

            using var zipStream = new MemoryStream(zipBytes, writable: false);
            using var zip = new ZipArchive(zipStream, ZipArchiveMode.Read);
            // Per-entry extraction with SafeJoin protects against zip-slip.
            foreach (var entry in zip.Entries)
            {
                if (string.IsNullOrEmpty(entry.FullName)) continue;
                // Directory entries end with '/'; create the dir then continue.
                var isDirectoryEntry = entry.FullName.EndsWith('/') || entry.FullName.EndsWith('\\');
                string destPath;
                try { destPath = SafeJoin(cacheDir, entry.FullName); }
                catch (InvalidOperationException ex)
                {
                    Respond(new { type = "warning", message = $"vgmstream zip rejected entry: {SanitizeMessage(ex.Message)}" });
                    continue;
                }

                if (isDirectoryEntry)
                {
                    Directory.CreateDirectory(destPath);
                    continue;
                }

                Directory.CreateDirectory(Path.GetDirectoryName(destPath)!);
                using var entryStream = entry.Open();
                using var fs = File.Create(destPath);
                entryStream.CopyTo(fs);
            }

            // Zip extraction does not restore POSIX exec bits; the static
            // Linux binary must be marked executable or Process.Start fails.
            if (!OperatingSystem.IsWindows())
            {
                try
                {
                    File.SetUnixFileMode(exePath,
                        UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute |
                        UnixFileMode.GroupRead | UnixFileMode.GroupExecute |
                        UnixFileMode.OtherRead | UnixFileMode.OtherExecute);
                }
                catch (Exception ex)
                {
                    Respond(new { type = "warning", message = $"Could not make vgmstream-cli executable: {SanitizeMessage(ex.Message)}" });
                }
            }

            if (File.Exists(exePath))
            {
                _vgmstreamPath = exePath;
                Respond(new { type = "info", message = "vgmstream downloaded and ready" });
            }
            else
            {
                Respond(new { type = "warning", message = $"vgmstream zip extracted but {exeName} not found" });
            }
        }
        catch (Exception ex)
        {
            Respond(new { type = "warning", message = $"vgmstream download failed: {SanitizeMessage(ex.Message)}. WEM files won't be converted." });
        }
    }

    /// <summary>Normalize a target audio-format string to a supported kind.</summary>
    private static string NormalizeAudioFormat(string raw)
    {
        var v = (raw ?? "").Trim().ToLowerInvariant();
        return v is "mp3" or "webm" or "ogg" ? v : "wav";
    }

    /// <summary>
    /// Decode an exported audio blob to the target format.
    ///
    /// vgmstream-cli (r1980) only *decodes* — it writes WAV (or OGG for native
    /// pcm/vorbis sources). For MP3/WebM targets the decode lands in an
    /// intermediate WAV that ffmpeg then re-encodes; on any ffmpeg failure the
    /// WAV is kept as a fallback so the user still gets playable audio.
    /// </summary>
    private static bool ConvertWithVgmstream(string inputPath, string outputDir, string targetFormat)
    {
        InitVgmstream();
        if (_vgmstreamPath == null) return false;

        var baseName = Path.GetFileNameWithoutExtension(inputPath);
        // SafeJoin enforces containment in case baseName carries traversal
        // from an upstream filename.
        var outPath = SafeJoin(outputDir, $"{baseName}.wav");

        // MP3/WebM need an ffmpeg pass; without ffmpeg (download failed, no
        // network) fall back to WAV rather than silently producing nothing.
        var format = NormalizeAudioFormat(targetFormat);
        var needsFfmpeg = format is "mp3" or "webm";
        if (needsFfmpeg)
        {
            InitFfmpeg();
            if (_ffmpegPath == null)
            {
                Respond(new { type = "warning", message = "ffmpeg unavailable; exporting WAV instead of " + format });
                needsFfmpeg = false;
            }
        }

        bool decoded;
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = _vgmstreamPath,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            // ArgumentList auto-quotes via CommandLineToArgvW rules, eliminating
            // the prior $"-o \"{outPath}\" \"{inputPath}\"" injection surface.
            psi.ArgumentList.Add("-o");
            psi.ArgumentList.Add(outPath);
            psi.ArgumentList.Add(inputPath);

            using var proc = Process.Start(psi);
            if (proc == null) return false;
            // Drain both pipes as they fill: with RedirectStandardOutput/Error
            // enabled but unread, a child that writes more than the OS pipe
            // buffer (progress/chatter) blocks forever on write — and the
            // waiting parent leaves a live leaked process holding the decoded
            // audio in memory. Draining must overlap the wait, not follow it.
            var soDrain = proc.StandardOutput.ReadToEndAsync();
            var seDrain = proc.StandardError.ReadToEndAsync();
            if (!proc.WaitForExit(30_000))
            {
                // Timed out — the child (and its buffers) must die, or every
                // such conversion permanently leaks a running process.
                try { proc.Kill(entireProcessTree: true); } catch { /* already dead */ }
            }
            // Reading ExitCode on a still-running process throws; settle the
            // drain tasks (and the process) before touching state.
            try
            {
                soDrain.Wait(5_000);
                seDrain.Wait(5_000);
            }
            catch { /* drain failures are best-effort */ }
            if (!proc.HasExited)
            {
                try { proc.WaitForExit(5_000); } catch { }
            }
            decoded = proc.HasExited && proc.ExitCode == 0 && File.Exists(outPath);
        }
        catch (Exception ex)
        {
            Respond(new { type = "warning", message = $"vgmstream conversion failed for {Path.GetFileName(inputPath)}: {SanitizeMessage(ex.Message)}" });
            return false;
        }

        if (!decoded) return false;
        if (!needsFfmpeg) return true;

        // Re-encode the intermediate WAV to the requested container. Keeping
        // the WAV on failure doubles as the fallback above; a successful
        // encode deletes it so no stray file lingers next to the MP3/WebM.
        var targetPath = SafeJoin(outputDir, $"{baseName}.{format}");
        var encoded = EncodeWithFfmpeg(outPath, targetPath, format);
        if (encoded)
        {
            try { File.Delete(outPath); } catch { /* keep fallback */ }
        }
        return true;
    }

    /// <summary>Re-encode a WAV to MP3 (libmp3lame VBR q2) or WebM (Opus 192k) with ffmpeg.</summary>
    private static bool EncodeWithFfmpeg(string inputPath, string outputPath, string format)
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = _ffmpegPath!,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            psi.ArgumentList.Add("-hide_banner");
            // Errors only — ffmpeg chatters otherwise and would fill the
            // drained stderr pipe for no gain.
            psi.ArgumentList.Add("-loglevel");
            psi.ArgumentList.Add("error");
            psi.ArgumentList.Add("-y");
            psi.ArgumentList.Add("-i");
            psi.ArgumentList.Add(inputPath);
            if (format == "mp3")
            {
                // VBR quality 2 (~190 kbps): default-quality music rips.
                psi.ArgumentList.Add("-c:a");
                psi.ArgumentList.Add("libmp3lame");
                psi.ArgumentList.Add("-q:a");
                psi.ArgumentList.Add("2");
            }
            else // webm
            {
                // Opus in WebM is the universally playable pair (Vorbis is
                // only "allowed" historically). 192k CBR target; libopus is
                // internally VBR anyway.
                psi.ArgumentList.Add("-c:a");
                psi.ArgumentList.Add("libopus");
                psi.ArgumentList.Add("-b:a");
                psi.ArgumentList.Add("192k");
            }
            psi.ArgumentList.Add(outputPath);

            using var proc = Process.Start(psi);
            if (proc == null) return false;
            // Same drain discipline as the vgmstream child above: pipes are
            // drained concurrently with the wait and the process tree is
            // killed on timeout — no child may leak past the request.
            var soDrain = proc.StandardOutput.ReadToEndAsync();
            var seDrain = proc.StandardError.ReadToEndAsync();
            if (!proc.WaitForExit(120_000))
            {
                try { proc.Kill(entireProcessTree: true); } catch { /* already dead */ }
            }
            try
            {
                soDrain.Wait(5_000);
                seDrain.Wait(5_000);
            }
            catch { /* drain failures are best-effort */ }
            if (!proc.HasExited)
            {
                try { proc.WaitForExit(5_000); } catch { }
            }

            var ok = proc.HasExited && proc.ExitCode == 0 &&
                     File.Exists(outputPath) && new FileInfo(outputPath).Length > 0;
            if (!ok)
            {
                // Never leave a rejected/corrupt target file behind.
                try { File.Delete(outputPath); } catch { }
            }
            return ok;
        }
        catch (Exception ex)
        {
            Respond(new { type = "warning", message = $"ffmpeg encode failed for {Path.GetFileName(inputPath)}: {SanitizeMessage(ex.Message)}" });
            return false;
        }
    }

    // ─── ffmpeg (MP3/WebM encode) ─────────────────────────────────────

    /// <summary>
    /// Ensure ffmpeg + libs are materialized in the shared cache dir, or
    /// leave _ffmpegPath null so callers fall back to WAV.
    /// Serializes the (90 MB) one-shot download across parallel export
    /// workers; the vgmstream equivalent above lacks the lock because it
    /// buffers in memory — the streaming archive file here makes races
    /// matter, so double-check under lock.
    /// </summary>
    private static void InitFfmpeg()
    {
        if (_ffmpegPath != null) return;
        lock (_ffmpegInitLock)
        {
            if (_ffmpegPath != null) return; // double-checked: another worker won

            var cacheDir = Path.Combine(Path.GetTempPath(), "CUE4ParseCLI_ffmpeg");
            var exeName = OperatingSystem.IsWindows() ? "ffmpeg.exe" : "ffmpeg";
            var readyMarker = Path.Combine(cacheDir, "ffmpeg.ready");

            // The marker is written only after a fully verified download +
            // extract, so a half-extracted cache can never be mistaken for
            // a complete one.
            var exePath = LocateFfmpegExe(cacheDir, exeName);
            if (exePath != null && File.Exists(readyMarker))
            {
                _ffmpegPath = exePath;
                return;
            }

            Directory.CreateDirectory(cacheDir);
            Respond(new { type = "info", message = "Downloading ffmpeg (~90 MB) for MP3/WebM audio export — one time, cached next run" });

            var archivePath = Path.Combine(cacheDir, "ffmpeg.archive");
            try
            {
                // Stream to disk while hashing: the ~90 MB archive must not
                // sit in a managed byte[] (one per racing worker would multi-
                // ply the spike), and hash verification still covers the
                // complete bytes before anything is extracted.
                var expectedHash = FfmpegArchiveSha256Active;
                using (var hash = System.Security.Cryptography.IncrementalHash.CreateHash(
                           System.Security.Cryptography.HashAlgorithmName.SHA256))
                {
                    using (var http = new HttpClient())
                    {
                        // Body read isn't covered by ReadAsByteArrayAsync's
                        // short default; a slow 90 MB transfer needs slack.
                        http.Timeout = TimeSpan.FromMinutes(10);
                        http.DefaultRequestHeaders.UserAgent.ParseAdd("CUE4ParseCLI/1.0");
                        using var resp = http.GetAsync(FfmpegArchiveUrlActive, HttpCompletionOption.ResponseHeadersRead).GetAwaiter().GetResult();
                        resp.EnsureSuccessStatusCode();
                        using var content = resp.Content.ReadAsStreamAsync().GetAwaiter().GetResult();
                        using var fs = File.Create(archivePath);
                        content.CopyTo(fs);
                        // Hash must also see the bytes on disk — fs flushes
                        // before HashFinal below.
                    }
                    using (var verify = File.OpenRead(archivePath))
                    {
                        var buffer = new byte[1 << 20];
                        int read;
                        while ((read = verify.Read(buffer, 0, buffer.Length)) > 0)
                            hash.AppendData(buffer, 0, read);
                    }
                    var actualHash = Convert.ToHexString(hash.GetHashAndReset()).ToLowerInvariant();
                    if (!string.Equals(actualHash, expectedHash, StringComparison.OrdinalIgnoreCase))
                    {
                        Respond(new { type = "warning", message = $"ffmpeg download hash mismatch (expected {expectedHash}, got {actualHash}). Refusing to extract." });
                        try { File.Delete(archivePath); } catch { }
                        return;
                    }
                }

                // zip on Windows (ZipArchive, zip-slip checked per entry);
                // tar.xz on Linux via the system tar — no gzip/xz decoder in
                // the BCL, and every Linux desktop has tar+liblzma.
                if (OperatingSystem.IsWindows())
                {
                    using var zipStream = new FileStream(archivePath, FileMode.Open, FileAccess.Read, FileShare.Read, 1 << 20, FileOptions.None);
                    using var zip = new ZipArchive(zipStream, ZipArchiveMode.Read);
                    if (!TryExtractZipEntries(zip, cacheDir, "ffmpeg"))
                    {
                        try { File.Delete(archivePath); } catch { }
                        return;
                    }
                }
                else
                {
                    if (!ExtractTarXz(archivePath, cacheDir))
                    {
                        try { File.Delete(archivePath); } catch { }
                        return;
                    }
                }

                // Re-discover: the versioned root dir name includes the ffmpeg
                // build id, so the exe path can't be assumed up front.
                exePath = LocateFfmpegExe(cacheDir, exeName);

                if (!OperatingSystem.IsWindows() && exePath != null)
                {
                    try
                    {
                        File.SetUnixFileMode(exePath,
                            UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute |
                            UnixFileMode.GroupRead | UnixFileMode.GroupExecute |
                            UnixFileMode.OtherRead | UnixFileMode.OtherExecute);
                    }
                    catch (Exception ex)
                    {
                        Respond(new { type = "warning", message = $"Could not mark ffmpeg executable: {SanitizeMessage(ex.Message)}" });
                    }
                }

                if (exePath != null)
                {
                    File.WriteAllBytes(readyMarker, Array.Empty<byte>());
                    _ffmpegPath = exePath;
                    Respond(new { type = "info", message = "ffmpeg downloaded and ready" });
                }
                else
                {
                    Respond(new { type = "warning", message = $"ffmpeg archive extracted but {exeName} not found" });
                }
            }
            catch (Exception ex)
            {
                Respond(new { type = "warning", message = $"ffmpeg download failed: {SanitizeMessage(ex.Message)}. Audio stays WAV." });
            }
            finally
            {
                // The archive is 90 MB of temp disk nobody reuses: extraction
                // (or its failure) is the only stage that needs it.
                try { File.Delete(archivePath); } catch { }
            }
        }
    }

    /// <summary>
    /// Shared-build archives extract as one versioned root dir
    /// (``ffmpeg-n8.1.3-…-{win64|linux64}/bin/ffmpeg[.exe]``), so the
    /// exe must be located rather than assumed. Returns null when absent.
    /// </summary>
    private static string? LocateFfmpegExe(string cacheDir, string exeName)
    {
        if (!Directory.Exists(cacheDir)) return null;
        foreach (var binDir in Directory.EnumerateDirectories(cacheDir, "bin", SearchOption.AllDirectories))
        {
            var candidate = Path.Combine(binDir, exeName);
            if (File.Exists(candidate)) return candidate;
        }
        return null;
    }

    /// <summary>zip-slip-checked extraction shared with the naive vgmstream flow.</summary>
    private static bool TryExtractZipEntries(ZipArchive zip, string destDir, string logTag)
    {
        foreach (var entry in zip.Entries)
        {
            if (string.IsNullOrEmpty(entry.FullName)) continue;
            // Directory entries end with '/'; create the dir then continue.
            var isDirectoryEntry = entry.FullName.EndsWith('/') || entry.FullName.EndsWith('\\');
            string destPath;
            try { destPath = SafeJoin(destDir, entry.FullName); }
            catch (InvalidOperationException ex)
            {
                Respond(new { type = "warning", message = $"{logTag} zip rejected entry: {SanitizeMessage(ex.Message)}" });
                return false;
            }

            if (isDirectoryEntry)
            {
                Directory.CreateDirectory(destPath);
                continue;
            }

            Directory.CreateDirectory(Path.GetDirectoryName(destPath)!);
            using var entryStream = entry.Open();
            using var fs = File.Create(destPath);
            entryStream.CopyTo(fs);
        }
        return true;
    }

    /// <summary>
    /// Extract a .tar.xz via the system tar. Requires tar+liblzma (present on
    /// every mainstream Linux desktop); failure returns false so the caller
    /// cleans up and falls back to WAV. Exchange: output stays silent —
    /// stderr is drained concurrently with the wait so a chatty tar can't
    /// wedge the pipe.
    /// </summary>
    private static bool ExtractTarXz(string archivePath, string destDir)
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = "/usr/bin/env",
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            psi.ArgumentList.Add("tar");
            psi.ArgumentList.Add("-xJf");
            psi.ArgumentList.Add(archivePath);
            psi.ArgumentList.Add("-C");
            psi.ArgumentList.Add(destDir);

            using var proc = Process.Start(psi);
            if (proc == null) return false;
            var seDrain = proc.StandardError.ReadToEndAsync();
            var soDrain = proc.StandardOutput.ReadToEndAsync();
            // tar on a 70 MB xz payload is fast, but give slow disks room —
            // a stuck extract is killed, not leaked (same discipline as the
            // conversion children).
            if (!proc.WaitForExit(600_000))
            {
                try { proc.Kill(entireProcessTree: true); } catch { /* already dead */ }
            }
            try
            {
                soDrain.Wait(5_000);
                seDrain.Wait(5_000);
            }
            catch { /* drain failures are best-effort */ }
            if (!proc.HasExited)
            {
                try { proc.WaitForExit(5_000); } catch { }
            }
            return proc.HasExited && proc.ExitCode == 0 && Directory.Exists(Path.Combine(destDir, "bin"));
        }
        catch (Exception ex)
        {
            Respond(new { type = "warning", message = $"ffmpeg tar extraction failed: {SanitizeMessage(ex.Message)}" });
            return false;
        }
    }

    // ─── Helpers ──────────────────────────────────────────────────────

    private static void HandleInspect(JObject cmd)
    {
        EnsureProvider();
        var path = cmd.Value<string>("path") ?? throw new ArgumentException("path required");

        // Strip standard UE4/5 extensions (but NOT .upk — registered with full path)
        foreach (var ext in new[] { ".uasset", ".umap", ".ubulk" })
        {
            if (path.EndsWith(ext, StringComparison.OrdinalIgnoreCase))
            {
                path = path[..^ext.Length];
                break;
            }
        }

        try
        {
            var pkg = _provider!.LoadPackage(path);
            var types = new List<object>();
            foreach (var obj in pkg.GetExports())
            {
                try { types.Add(new { name = obj.Name, type = obj.GetType().FullName }); }
                catch (Exception ex) { types.Add(new { name = "?", type = $"Error: {SanitizeMessage(ex.Message)}" }); }
            }
            Respond(new { type = "inspect_result", path, export_count = types.Count, exports = types });
        }
        catch (Exception ex)
        {
            RespondError($"Inspect failed for {path}: {SanitizeMessage(ex.Message)}");
        }
    }

    // ─── List Exports (enumerate exports inside a package) ────────────
    private static void HandleListExports(JObject cmd)
    {
        EnsureProvider();
        var rawPath = cmd.Value<string>("path") ?? throw new ArgumentException("path required");

        // Strip standard UE4/5 extensions (but NOT .upk — registered with full path)
        var path = rawPath;
        foreach (var ext in new[] { ".uasset", ".umap", ".ubulk" })
        {
            if (path.EndsWith(ext, StringComparison.OrdinalIgnoreCase))
            {
                path = path[..^ext.Length];
                break;
            }
        }

        // Use bounded-stack thread to prevent StackOverflowException on complex packages
        var exports = RunOnBoundedThread<List<object>>(() =>
        {
            var pkg = _provider!.LoadPackage(path);
            var result = new List<object>();
            foreach (var obj in pkg.GetExports())
            {
                try
                {
                    result.Add(new
                    {
                        name = obj.Name,
                        export_type = obj.ExportType ?? obj.GetType().Name,
                        outer = obj.Outer?.Name ?? ""
                    });
                }
                catch (Exception ex)
                {
                    result.Add(new { name = "?", export_type = $"Error: {SanitizeMessage(ex.Message)}", outer = "" });
                }
            }
            return result;
        }, 15_000, out var loadError);

        if (exports != null)
        {
            Respond(new { type = "exports_listed", path = rawPath, export_count = exports.Count, exports });
        }
        else
        {
            var errMsg = SanitizeMessage(loadError?.Message) ?? "Timed out loading package (too complex)";
            if (string.IsNullOrEmpty(errMsg)) errMsg = "Timed out loading package (too complex)";
            Respond(new
            {
                type = "exports_listed",
                path = rawPath,
                export_count = 0,
                exports = Array.Empty<object>(),
                error = errMsg
            });
        }
    }

    // ─── Scan Types (bulk-walk every package and emit its export types) ─
    /// <summary>
    /// Walk every .uasset/.umap/.upk in the mounted provider and classify each
    /// using path/filename heuristics (no package loading, no I/O). Finishes
    /// in under a second for any game size. Cancelable between batches.
    /// </summary>
    private static void HandleScanTypes(JObject cmd)
    {
        EnsureProvider();

        List<string> packagePaths;
        try
        {
            // Snapshot the dictionary before filtering so concurrent browse
            // commands cannot invalidate the enumerator mid-scan.
            packagePaths = _provider!.Files
                .ToArray()
                .Select(f => f.Value?.Path)
                .Where(p => !string.IsNullOrEmpty(p))
                .Cast<string>()
                .Where(p =>
                {
                    var lower = p.ToLowerInvariant();
                    return lower.EndsWith(".uasset") || lower.EndsWith(".umap") || lower.EndsWith(".upk");
                })
                .OrderBy(p => p, StringComparer.OrdinalIgnoreCase)
                .ToList();
        }
        catch (Exception ex)
        {
            // Surface the failure to Python so the bar hides and the user sees an error.
            Respond(new { type = "error", message = $"scan_types enumeration failed: {SanitizeMessage(ex.Message)}" });
            Respond(new { type = "types_scan_batch", entries = Array.Empty<object>(), final = true, error_count = 1, total_packages = 0 });
            return;
        }

        var total = packagePaths.Count;
        Respond(new { type = "types_scan_progress", current = 0, total });

        const int BatchSize = 1000;
        const int ProgressInterval = 500;
        var batch = new List<object>(BatchSize);
        var processed = 0;

        try
        {
            foreach (var path in packagePaths)
            {
                if (_cts.IsCancellationRequested) break;

                var assetType = ClassifyAssetType(path);
                var name = Path.GetFileNameWithoutExtension(path) ?? "";
                batch.Add(new
                {
                    path,
                    exports = new[] { new { name, export_type = assetType } },
                });
                processed++;

                if (processed % ProgressInterval == 0)
                    Respond(new { type = "types_scan_progress", current = processed, total });

                if (batch.Count >= BatchSize)
                {
                    Respond(new { type = "types_scan_batch", entries = batch.ToArray(), final = false });
                    batch.Clear();
                }
            }
        }
        catch (Exception ex)
        {
            Respond(new { type = "warning", message = $"scan_types aborted at {processed}/{total}: {SanitizeMessage(ex.Message)}" });
        }

        Respond(new
        {
            type = "types_scan_batch",
            entries = batch.ToArray(),
            final = true,
            error_count = 0,
            total_packages = processed,
        });
    }

    // ─── Get Props (full JSON content of a uasset) ────────────────────
    private static void HandleGetProps(JObject cmd)
    {
        EnsureProvider();
        var path = cmd.Value<string>("path") ?? throw new ArgumentException("path required");

        // Strip standard UE4/5 extensions (but NOT .upk — registered with full path)
        foreach (var ext in new[] { ".uasset", ".umap", ".ubulk" })
        {
            if (path.EndsWith(ext, StringComparison.OrdinalIgnoreCase))
            {
                path = path[..^ext.Length];
                break;
            }
        }

        try
        {
            var pkg = _provider!.LoadPackage(path);
            var exports = new List<object>();
            foreach (var obj in pkg.GetExports())
            {
                try
                {
                    var jsonStr = RunOnBoundedThread(() => SerializeUObject(obj), 15_000, out var serErr);

                    if (jsonStr != null)
                    {
                        var jsonObj = JToken.Parse(jsonStr);
                        exports.Add(new
                        {
                            name = obj.Name,
                            export_type = obj.ExportType ?? obj.GetType().Name,
                            properties = jsonObj
                        });
                    }
                    else
                    {
                        var errMsg = SanitizeMessage(serErr?.Message);
                        if (string.IsNullOrEmpty(errMsg))
                            errMsg = "Serialization timed out (object too complex)";
                        exports.Add(new
                        {
                            name = obj.Name,
                            export_type = obj.ExportType ?? obj.GetType().Name,
                            properties = (object)new { error = errMsg }
                        });
                    }
                }
                catch (Exception ex)
                {
                    exports.Add(new
                    {
                        name = obj.Name,
                        export_type = "Error",
                        properties = (object)new { error = SanitizeMessage(ex.Message) }
                    });
                }
            }

            // Build the response manually to embed the pre-serialized JSON
            var result = new JObject
            {
                ["type"] = "props_result",
                ["path"] = path,
                ["export_count"] = exports.Count,
                ["exports"] = JToken.FromObject(exports, Newtonsoft.Json.JsonSerializer.Create(_propsSettings))
            };
            // Write directly to avoid double-serialization via System.Text.Json
            Console.Out.WriteLine(result.ToString(Formatting.None));
            Console.Out.Flush();
        }
        catch (Exception ex)
        {
            RespondError($"GetProps failed for {path}: {SanitizeMessage(ex.Message)}");
        }
    }

    // ─── WEM Name Mapping ─────────────────────────────────────────────

    /// <summary>
    /// Ensure the WEM ID → debug name map is populated.
    /// Checks a per-game disk cache first; builds from soundbank files on a miss.
    /// </summary>
    private static void EnsureWemNameMap()
    {
        if (_wemMapBuilt) return;
        lock (_wemMapLock)
        {
            if (_wemMapBuilt) return;

            // Try loading from the disk cache (survives across sessions for the same game)
            if (_wemNameCachePath != null && File.Exists(_wemNameCachePath))
            {
                try
                {
                    var json = File.ReadAllText(_wemNameCachePath);
                    var cached = System.Text.Json.JsonSerializer.Deserialize<Dictionary<string, string>>(json);
                    if (cached != null && cached.Count > 0)
                    {
                        _wemIdToName = cached;
                        _wemMapBuilt = true;
                        Respond(new { type = "info", message = $"WEM names: loaded {_wemIdToName.Count} entries from cache" });
                        return;
                    }
                }
                catch { /* ignore corrupt cache — fall through to rebuild */ }
            }

            BuildWemNameMapFromBanks();
            _wemMapBuilt = true;
        }
    }

    // ─── Scan WWise AkAudioEvent assets ───────────────────────────────

    /// <summary>
    /// Scan all AkAudioEvent .uasset files under WwiseAudio/Events,
    /// extract media entries, and return organized audio data.
    /// Also supplements the WEM ID→name map.
    /// </summary>
    private static void HandleScanWwiseEvents(JObject cmd)
    {
        EnsureProvider();

        // Locate the WwiseAudio root and Events subfolder
        string? wwiseRoot = null;
        var eventAssets = new List<string>();

        foreach (var file in _provider!.Files)
        {
            var path = file.Value.Path;
            var lower = path.ToLowerInvariant();

            int idx = lower.IndexOf("wwiseaudio/");
            if (idx >= 0 && wwiseRoot == null)
                wwiseRoot = path[..(idx + "WwiseAudio/".Length)];

            // Collect .uasset files under Events subfolder
            if (idx >= 0 && lower.EndsWith(".uasset"))
            {
                var afterRoot = lower[(idx + "wwiseaudio/".Length)..];
                if (afterRoot.StartsWith("events/"))
                    eventAssets.Add(path);
            }
        }

        if (wwiseRoot == null || eventAssets.Count == 0)
        {
            Respond(new { type = "wwise_scan_result", found = false, wwise_root = "", audio = Array.Empty<object>() });
            return;
        }

        Respond(new
        {
            type = "progress",
            current = 0,
            total = eventAssets.Count,
            message = $"Scanning {eventAssets.Count} WWise events..."
        });

        var audioEntries = new List<object>();
        var seenMedia = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var eventsPrefix = wwiseRoot + "Events/";
        int scanned = 0;

        foreach (var assetPath in eventAssets)
        {
            if (_cts.Token.IsCancellationRequested) break;
            scanned++;
            if (scanned % 50 == 0 || scanned == eventAssets.Count)
                Respond(new
                {
                    type = "progress",
                    current = scanned,
                    total = eventAssets.Count,
                    message = $"Scanning WWise events: {scanned}/{eventAssets.Count}"
                });

            try
            {
                var objectPath = assetPath[..^7]; // strip .uasset
                var pkg = _provider.LoadPackage(objectPath);
                if (pkg == null) continue;

                foreach (var obj in pkg.GetExports())
                {
                    if (obj.ExportType != "AkAudioEvent") continue;

                    // Serialize on a bounded-stack thread to avoid StackOverflow
                    // from deeply recursive WWise object graphs.
                    var jObj = RunOnBoundedThread(() =>
                    {
                        var settings = new Newtonsoft.Json.JsonSerializerSettings
                        {
                            Formatting = Formatting.None,
                            ReferenceLoopHandling = ReferenceLoopHandling.Ignore,
                            MaxDepth = 48,
                            // Reject $type tokens — see _propsSettings comment.
                            TypeNameHandling = TypeNameHandling.None,
                            Error = (_, args) => args.ErrorContext.Handled = true
                        };
                        var json = JsonConvert.SerializeObject(obj, settings);
                        return JObject.Parse(json);
                    }, 10_000, out _);

                    if (jObj == null) continue;

                    // Relative path of this event within Events/
                    var relPath = assetPath.StartsWith(eventsPrefix, StringComparison.OrdinalIgnoreCase)
                        ? assetPath[eventsPrefix.Length..]
                        : assetPath;
                    var eventDir = Path.GetDirectoryName(relPath)?.Replace('\\', '/') ?? "";

                    // Navigate the serialized JSON to extract media entries
                    // Path: EventCookedData → EventLanguageMap → [*].Value → Media → [*]
                    var langMap = jObj.SelectToken("EventCookedData.EventLanguageMap") as JArray;
                    if (langMap == null) continue;

                    foreach (var langEntry in langMap)
                    {
                        var mediaArray = langEntry.SelectToken("Value.Media") as JArray;
                        if (mediaArray == null) continue;

                        foreach (var mediaItem in mediaArray)
                        {
                            var debugName = mediaItem.Value<string>("DebugName") ?? "";
                            var mediaPath = mediaItem.Value<string>("MediaPathName") ?? "";
                            var mediaId = mediaItem.Value<long?>("MediaId") ?? 0;

                            if (string.IsNullOrEmpty(debugName) || string.IsNullOrEmpty(mediaPath)) continue;

                            // Deduplicate across events (same wem may be in multiple events)
                            if (!seenMedia.Add($"{eventDir}/{mediaPath}")) continue;

                            // Build full VFS path for the .wem
                            var fullWemPath = wwiseRoot + mediaPath;
                            var cleanName = Path.GetFileNameWithoutExtension(debugName);

                            audioEntries.Add(new
                            {
                                debug_name = cleanName,
                                media_id = mediaId,
                                wem_vfs_path = fullWemPath,
                                event_name = obj.Name,
                                event_folder = eventDir
                            });

                            _wemIdToName.TryAdd(mediaId.ToString(), cleanName);
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                Respond(new { type = "warning", message = $"WWise scan: skipped {Path.GetFileName(assetPath)}: {SanitizeMessage(ex.Message)}" });
            }
        }

        var cancelled = _cts.Token.IsCancellationRequested;
        Respond(new
        {
            type = "wwise_scan_result",
            found = true,
            wwise_root = wwiseRoot,
            events_prefix = eventsPrefix,
            total_events = eventAssets.Count,
            total_audio = audioEntries.Count,
            remaining = cancelled ? eventAssets.Count - scanned : 0,
            cancelled,
            audio = audioEntries
        });
    }

    // ─── Export WWise audio with proper naming ────────────────────────

    /// <summary>
    /// Export .wem files from the VFS, rename to friendly names, and convert
    /// to the target audio format. Output is placed in the Events folder structure.
    /// </summary>
    private static Task HandleExportWwiseAudio(JObject cmd)
    {
        // Extracted .wem payloads are sizable; honor the parent's memory
        // policy and gate each entry through it.
        ResolveMemoryPolicy(cmd, adaptDop: false);
        EnsureProvider();
        var outputDir = cmd.Value<string>("output_dir") ?? throw new ArgumentException("output_dir required");
        var audioFormat = NormalizeAudioFormat(cmd.Value<string>("audio_format") ?? "wav");
        var entries = cmd["entries"] as JArray ?? throw new ArgumentException("entries required");

        _audioFormat = audioFormat;
        Directory.CreateDirectory(outputDir);

        // Local degree of parallelism — do NOT touch the global _exportThreads
        // here: audio exports run via RunExportWithCancelSupport while another
        // export/scan may still be draining queued commands, and clobbering the
        // global would desync the serialize-thread cap for concurrent work.
        var dop = ResolveExportThreads(cmd);

        var total = entries.Count;
        int current = 0;
        var succeeded = new ConcurrentQueue<string>();
        var failed = new ConcurrentQueue<object>();
        // Guards the "resolve entry → claim output name" step so duplicate
        // target names fail deterministically instead of racing on the same
        // .wem/.wav paths (sequential code was last-write-wins by accident).
        var claimedOutputs = new ConcurrentSet();
        var claimLock = new object();
        var pacer = new ProgressPacer();

        Respond(new { type = "progress", current = 0, total, message = $"Exporting {total} audio files..." });

        try
        {
            Parallel.ForEach(entries, new ParallelOptions
            {
                MaxDegreeOfParallelism = dop,
                CancellationToken = _cts.Token
            },
            entry =>
            {
                var done = Interlocked.Increment(ref current);

                var wemPath = entry.Value<string>("wem_vfs_path") ?? "";
                var targetName = entry.Value<string>("target_name") ?? "";
                var targetFolder = entry.Value<string>("target_folder") ?? "";

                // Memory gate: extracted banks park whole .wem payloads in
                // managed memory before conversion, so back off near the cap.
                try { WaitUnderMemoryGate(_cts.Token); }
                catch (MemoryLimitExceededException mle)
                {
                    failed.Enqueue(new { path = wemPath, error = SanitizeMessage(mle.Message) });
                    return;
                }

                // Sanitize parent-supplied path components: each folder segment is
                // run through SanitizeName, traversal segments are rejected, and
                // SafeJoin enforces containment in outputDir.
                var safeName = SanitizeName(targetName);
                if (string.IsNullOrEmpty(safeName))
                {
                    failed.Enqueue(new { path = wemPath, error = "Invalid target_name" });
                    return;
                }

                string safeSubDir;
                try
                {
                    safeSubDir = SafeJoinFolderSegments(outputDir, targetFolder);
                }
                catch (Exception ex)
                {
                    failed.Enqueue(new { path = wemPath, error = $"Invalid target_folder: {SanitizeMessage(ex.Message)}" });
                    return;
                }

                // Claim the unique output key (.wem temp + final .wav both stem
                // from safeSubDir + safeName) before doing any work.
                var outKey = $"{safeSubDir}::{safeName}";
                lock (claimLock)
                {
                    if (!claimedOutputs.Add(outKey))
                    {
                        failed.Enqueue(new { path = wemPath, error = "Duplicate target name in folder" });
                        return;
                    }
                }

                pacer.Emit(done, total, $"Exporting audio: {safeName}");

                try
                {
                    Directory.CreateDirectory(safeSubDir);

                    var wemTempPath = SafeJoin(safeSubDir, $"{safeName}.wem");

                    // Stream the payload to its temp file rather than holding
                    // the whole .wem in managed memory — large banks are the
                    // audio pipeline's biggest allocation.
                    if (!StreamProviderFileTo(wemPath, wemTempPath))
                    {
                        if (!_provider!.TrySaveAsset(wemPath, out var data) || data == null)
                        {
                            failed.Enqueue(new { path = wemPath, error = "File not found in VFS" });
                            return;
                        }
                        File.WriteAllBytes(wemTempPath, data);
                    }

                    var converted = ConvertWithVgmstream(wemTempPath, safeSubDir, audioFormat);
                    if (converted)
                    {
                        try { File.Delete(wemTempPath); } catch { }
                    }
                    // Keep .wem as fallback if conversion failed
                    succeeded.Enqueue(safeName);
                }
                catch (Exception ex)
                {
                    failed.Enqueue(new { path = wemPath, error = SanitizeMessage(ex.Message) });
                }
            });
        }
        catch (OperationCanceledException)
        {
            // Expected when the user cancels — fall through to the payload below.
        }

        var cancelled = _cts.Token.IsCancellationRequested;
        Respond(new
        {
            type = "export_done",
            succeeded = succeeded.ToList(),
            failed = failed.ToList(),
            total = entries.Count,
            cancelled,
            remaining = cancelled ? total - Volatile.Read(ref current) : 0,
            warning_count = 0,
        });
        return Task.CompletedTask;
    }

    // ─── FMOD Studio banks (scan + export via vgmstream) ──────────────

    /// <summary>
    /// Resolve a .bank to a file on disk vgmstream can open. Loose banks are
    /// used in place; VFS-packed banks are extracted once per session into a
    /// temp cache (cleared on init).
    /// </summary>
    // Serializes cache lookup/materialization — HandleExportFmodAudio workers
    // run concurrently and frequently target subsongs of the SAME bank, so the
    // _fmodBankCache dictionary now needs thread-safe access, not just
    // deduped materialization.
    private static readonly object _fmodBankCacheLock = new();

    private static string? GetFmodBankLocalPath(string vfsPath, out string? error)
    {
        error = null;
        if (_looseFiles.TryGetValue(vfsPath, out var diskPath) && File.Exists(diskPath))
            return diskPath;

        lock (_fmodBankCacheLock)
        {
            if (_fmodBankCache.TryGetValue(vfsPath, out var cached) && File.Exists(cached))
                return cached;

            if (_provider == null || !_provider.TrySaveAsset(vfsPath, out var data) || data == null)
            {
                error = "Bank not found in VFS";
                return null;
            }

            var cacheDir = Path.Combine(Path.GetTempPath(), "CUE4ParseCLI_fmod");
            Directory.CreateDirectory(cacheDir);
            // Hash prefix keeps same-named banks from different folders apart.
            var hash = ComputeSha256Hex(System.Text.Encoding.UTF8.GetBytes(vfsPath))[..16];
            var name = SanitizeName(Path.GetFileName(vfsPath));
            if (string.IsNullOrEmpty(name)) name = "bank.bank";
            var tempPath = Path.Combine(cacheDir, $"{hash}_{name}");
            File.WriteAllBytes(tempPath, data);
            _fmodBankCache[vfsPath] = tempPath;
            return tempPath;
        }
    }

    /// <summary>Run vgmstream-cli with captured stdout. Null if it couldn't run.</summary>
    private static (int ExitCode, string Stdout)? RunVgmstreamCapture(IEnumerable<string> args, int timeoutMs)
    {
        InitVgmstream();
        if (_vgmstreamPath == null) return null;

        var psi = new ProcessStartInfo
        {
            FileName = _vgmstreamPath,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true
        };
        foreach (var a in args) psi.ArgumentList.Add(a);

        try
        {
            using var proc = Process.Start(psi);
            if (proc == null) return null;
            // Drain both pipes concurrently — a blocked stderr pipe would
            // deadlock a sequential ReadToEnd on stdout.
            var soTask = proc.StandardOutput.ReadToEndAsync();
            var seTask = proc.StandardError.ReadToEndAsync();
            if (!proc.WaitForExit(timeoutMs))
            {
                try { proc.Kill(); } catch { }
                return null;
            }
            return (proc.ExitCode, soTask.GetAwaiter().GetResult());
        }
        catch
        {
            return null;
        }
    }

    /// <summary>
    /// Probe every subsong in a bank via `vgmstream-cli -m -I -S 0`; one JSON
    /// object per subsong. Falls back to per-subsong probes if the batched
    /// form only reported the first stream.
    /// </summary>
    private static List<JObject> ProbeFmodBank(string localPath)
    {
        var result = new List<JObject>();
        var run = RunVgmstreamCapture(new[] { "-m", "-I", "-S", "0", localPath }, 120_000);
        if (run == null) return result;
        result.AddRange(ParseJsonObjects(run.Value.Stdout));

        if (result.Count == 1)
        {
            var totalSubsongs = result[0].SelectToken("streamInfo.total")?.Value<int>() ?? 1;
            for (int i = 2; i <= totalSubsongs; i++)
            {
                if (_cts.Token.IsCancellationRequested) break;
                var sub = RunVgmstreamCapture(new[] { "-m", "-I", "-s", i.ToString(), localPath }, 30_000);
                if (sub == null) break;
                var objs = ParseJsonObjects(sub.Value.Stdout);
                if (objs.Count == 0) break;
                result.Add(objs[0]);
            }
        }
        return result;
    }

    private static List<JObject> ParseJsonObjects(string stdout)
    {
        var objs = new List<JObject>();
        foreach (var line in stdout.Split('\n'))
        {
            var trimmed = line.Trim();
            if (!trimmed.StartsWith('{')) continue;
            try { objs.Add(JObject.Parse(trimmed)); } catch { /* non-JSON chatter */ }
        }
        if (objs.Count == 0 && stdout.Contains('{'))
        {
            // Pretty-printed output — try the whole capture as one object.
            try { objs.Add(JObject.Parse(stdout[stdout.IndexOf('{')..])); } catch { }
        }
        return objs;
    }

    /// <summary>
    /// Enumerate every FMOD .bank (VFS + loose) and list its playable streams
    /// with names from the FSB name table where present. .strings.bank holds
    /// only the GUID→event-path table — no sample data — so it's skipped.
    /// </summary>
    private static void HandleScanFmodBanks(JObject cmd)
    {
        EnsureProvider();

        var bankPaths = new List<string>();
        foreach (var file in _provider!.Files)
        {
            var lower = file.Value.Path.ToLowerInvariant();
            if (lower.EndsWith(".bank") && !lower.EndsWith(".strings.bank"))
                bankPaths.Add(file.Value.Path);
        }
        foreach (var (loosePath, _) in _looseFiles)
        {
            var lower = loosePath.ToLowerInvariant();
            if (lower.EndsWith(".bank") && !lower.EndsWith(".strings.bank"))
                bankPaths.Add(loosePath);
        }

        if (bankPaths.Count == 0)
        {
            Respond(new { type = "fmod_scan_result", found = false, total_banks = 0, total_streams = 0, banks = Array.Empty<object>() });
            return;
        }

        InitVgmstream();
        if (_vgmstreamPath == null)
        {
            Respond(new { type = "warning", message = "FMOD scan: vgmstream unavailable — banks cannot be decoded" });
            Respond(new { type = "fmod_scan_result", found = false, total_banks = bankPaths.Count, total_streams = 0, banks = Array.Empty<object>() });
            return;
        }

        Respond(new { type = "progress", current = 0, total = bankPaths.Count, message = $"Scanning {bankPaths.Count} FMOD bank(s)..." });

        var banks = new List<object>();
        int totalStreams = 0, scanned = 0;

        foreach (var bankPath in bankPaths)
        {
            if (_cts.Token.IsCancellationRequested) break;
            scanned++;
            Respond(new { type = "progress", current = scanned, total = bankPaths.Count, message = $"Scanning FMOD bank: {Path.GetFileName(bankPath)}" });

            try
            {
                var local = GetFmodBankLocalPath(bankPath, out var err);
                if (local == null)
                {
                    Respond(new { type = "warning", message = $"FMOD scan: {Path.GetFileName(bankPath)}: {err}" });
                    continue;
                }

                var infos = ProbeFmodBank(local);
                if (infos.Count == 0) continue; // metadata-only bank (no FSB payload)

                var streams = new List<object>();
                foreach (var info in infos)
                {
                    var index = info.SelectToken("streamInfo.index")?.Value<int>() ?? (streams.Count + 1);
                    // Single-stream banks report index 0 (no subsong selection);
                    // vgmstream's -s flag is 1-based, so normalize.
                    if (index <= 0) index = streams.Count + 1;
                    var name = info.SelectToken("streamInfo.name")?.Value<string>() ?? "";
                    var sampleRate = info.Value<int?>("sampleRate") ?? 0;
                    var samples = info.Value<long?>("numberOfSamples") ?? 0;
                    var durationMs = sampleRate > 0 ? (long)(samples * 1000.0 / sampleRate) : 0;
                    streams.Add(new { index, name, sample_rate = sampleRate, duration_ms = durationMs });
                }

                totalStreams += streams.Count;
                banks.Add(new { bank_vfs_path = bankPath, bank_name = Path.GetFileName(bankPath), streams });
            }
            catch (Exception ex)
            {
                Respond(new { type = "warning", message = $"FMOD scan: skipped {Path.GetFileName(bankPath)}: {SanitizeMessage(ex.Message)}" });
            }
        }

        var cancelled = _cts.Token.IsCancellationRequested;
        Respond(new
        {
            type = "fmod_scan_result",
            found = banks.Count > 0,
            total_banks = banks.Count,
            total_streams = totalStreams,
            remaining = cancelled ? bankPaths.Count - scanned : 0,
            cancelled,
            banks
        });
    }

    /// <summary>
    /// Export individual FMOD bank streams to WAV. Each entry is
    /// { bank_vfs_path, stream_index, target_name, target_folder }; replies
    /// with the standard export_done payload (succeeded carries disk paths).
    /// </summary>
    private static void HandleExportFmodAudio(JObject cmd)
    {
        // Materializing banks to temp files is the audio pipeline's biggest
        // allocation; honor the parent's memory policy and gate each entry.
        ResolveMemoryPolicy(cmd, adaptDop: false);
        EnsureProvider();
        var outputDir = cmd.Value<string>("output_dir") ?? throw new ArgumentException("output_dir required");
        var entries = cmd["entries"] as JArray ?? throw new ArgumentException("entries required");

        Directory.CreateDirectory(outputDir);

        // Local degree of parallelism — see HandleExportWwiseAudio for why the
        // global _exportThreads is deliberately not clobbered here.
        var dop = ResolveExportThreads(cmd);

        var total = entries.Count;
        int current = 0;
        var succeeded = new ConcurrentQueue<string>();
        var failed = new ConcurrentQueue<object>();
        // Two subsongs of different banks may map to the same output file in
        // the same target folder; claim output paths so parallel writers don't
        // interleave bytes into one .wav.
        var claimedOutputs = new ConcurrentSet();
        var claimLock = new object();
        var pacer = new ProgressPacer();

        Respond(new { type = "progress", current = 0, total, message = $"Exporting {total} FMOD stream(s)..." });

        try
        {
            Parallel.ForEach(entries, new ParallelOptions
            {
                MaxDegreeOfParallelism = dop,
                CancellationToken = _cts.Token
            },
            entry =>
            {
                var done = Interlocked.Increment(ref current);

                var bankPath = entry.Value<string>("bank_vfs_path") ?? "";
                var streamIndex = entry.Value<int?>("stream_index") ?? 0;
                var targetName = entry.Value<string>("target_name") ?? "";
                var targetFolder = entry.Value<string>("target_folder") ?? "";

                // Memory gate — banks materialize to whole temp files.
                try { WaitUnderMemoryGate(_cts.Token); }
                catch (MemoryLimitExceededException mle)
                {
                    failed.Enqueue(new { path = $"{bankPath}#{streamIndex}", error = SanitizeMessage(mle.Message) });
                    return;
                }

                var safeName = SanitizeName(targetName);
                if (string.IsNullOrEmpty(safeName) || streamIndex <= 0)
                {
                    failed.Enqueue(new { path = bankPath, error = "Invalid target_name or stream_index" });
                    return;
                }

                string safeSubDir;
                try
                {
                    safeSubDir = SafeJoinFolderSegments(outputDir, targetFolder);
                }
                catch (Exception ex)
                {
                    failed.Enqueue(new { path = bankPath, error = $"Invalid target_folder: {SanitizeMessage(ex.Message)}" });
                    return;
                }

                var outKey = $"{safeSubDir}::{safeName}";
                lock (claimLock)
                {
                    if (!claimedOutputs.Add(outKey))
                    {
                        failed.Enqueue(new { path = $"{bankPath}#{streamIndex}", error = "Duplicate target name in folder" });
                        return;
                    }
                }

                pacer.Emit(done, total, $"Exporting FMOD audio: {safeName}");

                try
                {
                    var local = GetFmodBankLocalPath(bankPath, out var err);
                    if (local == null)
                    {
                        failed.Enqueue(new { path = bankPath, error = err ?? "bank unavailable" });
                        return;
                    }

                    Directory.CreateDirectory(safeSubDir);
                    var outPath = SafeJoin(safeSubDir, $"{safeName}.wav");
                    var run = RunVgmstreamCapture(new[] { "-s", streamIndex.ToString(), "-o", outPath, local }, 60_000);
                    if (run == null || run.Value.ExitCode != 0 || !File.Exists(outPath))
                    {
                        failed.Enqueue(new { path = $"{bankPath}#{streamIndex}", error = "vgmstream conversion failed" });
                        return;
                    }
                    succeeded.Enqueue(outPath);
                }
                catch (Exception ex)
                {
                    failed.Enqueue(new { path = $"{bankPath}#{streamIndex}", error = SanitizeMessage(ex.Message) });
                }
            });
        }
        catch (OperationCanceledException)
        {
            // Expected when the user cancels — fall through to the payload below.
        }

        var cancelled = _cts.Token.IsCancellationRequested;
        Respond(new
        {
            type = "export_done",
            succeeded = succeeded.ToList(),
            failed = failed.ToList(),
            total = entries.Count,
            cancelled,
            remaining = cancelled ? total - Volatile.Read(ref current) : 0,
            warning_count = 0,
        });
    }

    // ─── Export video (FileMediaSource + raw video files) ─────────────

    /// <summary>
    /// Export movie assets — either FileMediaSource UObjects (resolves the
    /// embedded FilePath reference to a VFS path before writing) or raw
    /// video leaves (.bk2/.mp4/.webm/.mov, written byte-for-byte).
    ///
    /// Each entry is { vfs_path, kind } where kind is "file_media_source"
    /// or "raw_video". Replies with the standard export_done payload.
    /// </summary>
    private static Task HandleExportVideo(JObject cmd)
    {
        // Video payloads are the largest allocations in the pipeline; honor
        // the parent's memory policy and gate every entry through it.
        ResolveMemoryPolicy(cmd, adaptDop: false);
        EnsureProvider();
        var outputDir = cmd.Value<string>("output_dir") ?? throw new ArgumentException("output_dir required");
        var entries = cmd["entries"] as JArray ?? throw new ArgumentException("entries required");

        Directory.CreateDirectory(outputDir);

        int total = entries.Count, current = 0;
        var succeeded = new List<string>();
        var failed = new List<object>();

        Respond(new { type = "progress", current = 0, total, message = $"Exporting {total} video file(s)..." });

        foreach (var entry in entries)
        {
            if (_cts.Token.IsCancellationRequested) break;

            var vfsPath = entry.Value<string>("vfs_path") ?? "";
            var kind = (entry.Value<string>("kind") ?? "raw_video").ToLowerInvariant();
            if (string.IsNullOrEmpty(vfsPath))
            {
                failed.Add(new { path = vfsPath, error = "Empty vfs_path" });
                continue;
            }

            // Memory gate — the per-entry failure path below keeps a single
            // gate timeout from aborting the remaining video exports.
            try { WaitUnderMemoryGate(_cts.Token); }
            catch (MemoryLimitExceededException mle)
            {
                failed.Add(new { path = vfsPath, error = SanitizeMessage(mle.Message) });
                continue;
            }
            current++;

            if (current % 5 == 0 || current == total)
                Respond(new { type = "progress", current, total, message = $"Exporting video: {Path.GetFileName(vfsPath)}" });

            try
            {
                if (kind == "raw_video")
                {
                    var name = SanitizeName(Path.GetFileName(vfsPath));
                    if (string.IsNullOrEmpty(name))
                    {
                        failed.Add(new { path = vfsPath, error = "Invalid filename" });
                        continue;
                    }
                    var outPath = SafeJoin(outputDir, name);

                    // Large videos stream straight to disk — buffering a
                    // multi-GB cutscene whole is what the memory cap prevents.
                    if (StreamProviderFileTo(vfsPath, outPath))
                    {
                        succeeded.Add(outPath);
                        continue;
                    }

                    if (!_provider!.TrySaveAsset(vfsPath, out var data) || data == null)
                    {
                        failed.Add(new { path = vfsPath, error = "File not found in VFS" });
                        continue;
                    }
                    File.WriteAllBytes(outPath, data);
                    succeeded.Add(outPath);
                    continue;
                }

                if (kind == "file_media_source")
                {
                    var resolved = ResolveFileMediaSource(vfsPath, out var resolveErr);
                    if (resolved == null)
                    {
                        failed.Add(new { path = vfsPath, error = resolveErr ?? "FileMediaSource resolution failed" });
                        continue;
                    }
                    var name = SanitizeName(Path.GetFileName(resolved));
                    if (string.IsNullOrEmpty(name))
                    {
                        failed.Add(new { path = vfsPath, error = "Invalid filename after resolve" });
                        continue;
                    }
                    var outPath = SafeJoin(outputDir, name);

                    if (StreamProviderFileTo(resolved, outPath))
                    {
                        succeeded.Add(outPath);
                        continue;
                    }

                    if (!_provider!.TrySaveAsset(resolved, out var data) || data == null)
                    {
                        failed.Add(new { path = vfsPath, error = $"Resolved file not found: {resolved}" });
                        continue;
                    }
                    File.WriteAllBytes(outPath, data);
                    succeeded.Add(outPath);
                    continue;
                }

                failed.Add(new { path = vfsPath, error = $"Unknown video kind: {kind}" });
            }
            catch (Exception ex)
            {
                failed.Add(new { path = vfsPath, error = SanitizeMessage(ex.Message) });
            }
        }

        var cancelledVideo = _cts.Token.IsCancellationRequested;
        Respond(new
        {
            type = "export_done",
            succeeded,
            failed,
            total = entries.Count,
            cancelled = cancelledVideo,
            remaining = cancelledVideo ? entries.Count - current : 0,
            warning_count = 0,
        });
        return Task.CompletedTask;
    }

    /// <summary>
    /// Load <paramref name="assetPath"/> as a UObject, look up its FileMediaSource
    /// FilePath (UE FFilePath struct), and resolve it against the VFS using
    /// (1) literal match, (2) Movies/&lt;basename&gt;, (3) Movies/**/&lt;basename&gt; glob.
    /// Returns the resolved VFS path, or null + sets <paramref name="error"/>.
    /// </summary>
    private static string? ResolveFileMediaSource(string assetPath, out string? error)
    {
        error = null;
        try
        {
            var lower = assetPath.ToLowerInvariant();
            string objectPath = assetPath;
            foreach (var ext in new[] { ".uasset", ".umap", ".ubulk" })
            {
                if (objectPath.EndsWith(ext, StringComparison.OrdinalIgnoreCase))
                {
                    objectPath = objectPath[..^ext.Length];
                    break;
                }
            }

            var pkg = _provider!.LoadPackage(objectPath);
            if (pkg == null)
            {
                error = "Package failed to load";
                return null;
            }

            string? filePath = null;
            foreach (var obj in pkg.GetExports())
            {
                if (obj.ExportType != "FileMediaSource") continue;

                // Serialize on a bounded-stack thread (mirrors WWise scan).
                var jObj = RunOnBoundedThread(() =>
                {
                    var settings = new Newtonsoft.Json.JsonSerializerSettings
                    {
                        Formatting = Formatting.None,
                        ReferenceLoopHandling = ReferenceLoopHandling.Ignore,
                        MaxDepth = 32,
                        TypeNameHandling = TypeNameHandling.None,
                        Error = (_, args) => args.ErrorContext.Handled = true
                    };
                    var json = JsonConvert.SerializeObject(obj, settings);
                    return JObject.Parse(json);
                }, 5_000, out _);

                if (jObj == null) continue;
                // FFilePath comes through as `{ FilePath: { FilePath: "..." } }`.
                filePath = jObj.SelectToken("FilePath.FilePath")?.Value<string>()
                            ?? jObj.SelectToken("FilePath")?.Value<string>();
                if (!string.IsNullOrEmpty(filePath)) break;
            }

            if (string.IsNullOrEmpty(filePath))
            {
                error = "FilePath not found on FileMediaSource";
                return null;
            }

            // 1. Literal match (sometimes the path is already VFS-rooted).
            var normalized = filePath.Replace('\\', '/').TrimStart('/');
            if (_provider.Files.ContainsKey(normalized))
                return normalized;

            var basename = Path.GetFileName(normalized);
            if (string.IsNullOrEmpty(basename))
            {
                error = "FilePath has no basename";
                return null;
            }

            // 2. Movies/<basename>
            foreach (var key in _provider.Files.Keys)
            {
                if (key.EndsWith("/" + basename, StringComparison.OrdinalIgnoreCase) &&
                    key.Contains("/Movies/", StringComparison.OrdinalIgnoreCase))
                    return key;
            }

            // 3. Any **/<basename> match — last resort so a renamed Movies folder still works.
            foreach (var key in _provider.Files.Keys)
            {
                if (key.EndsWith("/" + basename, StringComparison.OrdinalIgnoreCase))
                    return key;
            }

            error = $"No VFS entry matched basename {basename}";
            return null;
        }
        catch (Exception ex)
        {
            error = SanitizeMessage(ex.Message);
            return null;
        }
    }

    /// <summary>Force-rebuild the WEM name map, deleting any stale disk cache.</summary>
    private static void HandleRebuildWemCache()
    {
        EnsureProvider();
        _wemIdToName = new(StringComparer.OrdinalIgnoreCase);
        _wemMapBuilt = false;
        if (_wemNameCachePath != null && File.Exists(_wemNameCachePath))
            try { File.Delete(_wemNameCachePath); } catch { }
        EnsureWemNameMap();
        Respond(new { type = "wem_cache_rebuilt", count = _wemIdToName.Count });
    }

    /// <summary>Scan all .bnk files in the provider and build the WEM ID → name map.</summary>
    private static void BuildWemNameMapFromBanks()
    {
        if (_provider == null) return;

        var bnkFiles = _provider.Files
            .Where(f => f.Value.Path.EndsWith(".bnk", StringComparison.OrdinalIgnoreCase))
            .Select(f => f.Value.Path)
            .ToList();

        if (bnkFiles.Count == 0)
        {
            Respond(new { type = "info", message = "WEM names: no soundbank files found in VFS" });
            return;
        }

        Respond(new { type = "info", message = $"WEM names: scanning {bnkFiles.Count} soundbank(s)..." });

        foreach (var bnkPath in bnkFiles)
        {
            try
            {
                if (!_provider.TrySaveAsset(bnkPath, out var bnkData)) continue;
                using var ar = new FWwiseArchive(bnkPath, bnkData);
                var wwise = new WwiseReader(ar, new WwiseArchiveSource(), bnkData.Length);
                ProcessWwiseBank(wwise);
            }
            catch (Exception ex)
            {
                Respond(new { type = "warning", message = $"WEM map: skipped {Path.GetFileName(bnkPath)}: {SanitizeMessage(ex.Message)}" });
            }
        }

        Respond(new { type = "info", message = $"WEM names: built {_wemIdToName.Count} entries" });

        // Persist to disk cache for future sessions
        if (_wemNameCachePath != null)
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(_wemNameCachePath)!);
                File.WriteAllText(_wemNameCachePath,
                    System.Text.Json.JsonSerializer.Serialize(_wemIdToName));
            }
            catch { /* non-fatal if cache write fails */ }
        }
    }

    /// <summary>
    /// Walk a parsed WwiseReader's hierarchy tables and populate _wemIdToName.
    /// Maps SourceId (the .wem numeric filename) → the debug event name from BankStrMap.
    /// Note: CUE4Parse exposes direct Event→Action→Sound links only;
    /// multi-level container children are not accessible in this API version.
    /// </summary>
    private static void ProcessWwiseBank(WwiseReader wwise)
    {
        var hierarchies = wwise.Hierarchies;
        // Formerly IdToString — same STID name table, renamed upstream.
        var idToString = wwise.BankIDToFileName;
        if (hierarchies == null || idToString == null || idToString.Count == 0) return;

        // Build a flat ID → AbstractHierarchy lookup for this bank
        var byId = new Dictionary<uint, AbstractHierarchy>(hierarchies.Length);
        foreach (var h in hierarchies)
            if (h.Data != null)
                byId[h.Data.Id] = h.Data;

        // Each entry in IdToString is a named Wwise object (typically an event or sound)
        foreach (var (id, rawName) in idToString)
        {
            if (!byId.TryGetValue(id, out var hier)) continue;
            var name = SanitizeName(rawName);

            switch (hier)
            {
                case HierarchyEvent evt:
                    // Follow each EventAction → direct sound reference
                    foreach (var actionId in evt.EventActionIds)
                    {
                        if (!byId.TryGetValue(actionId, out var actionHier)) continue;
                        if (actionHier is not HierarchyEventAction action) continue;
                        // Direct sound: EventAction.ReferencedId is a SoundSfxVoice
                        if (byId.TryGetValue(action.ReferencedId, out var target) &&
                            target is HierarchySoundSfxVoice sound && sound.Source != null)
                        {
                            _wemIdToName.TryAdd(sound.Source.SourceId.ToString(), name);
                        }
                    }
                    break;

                case HierarchySoundSfxVoice voice when voice.Source != null:
                    // Directly-named sound node — maps immediately
                    _wemIdToName.TryAdd(voice.Source.SourceId.ToString(), name);
                    break;
            }
        }
    }

    /// <summary>Replace characters that are invalid in file names with underscores.</summary>
    private static string SanitizeName(string name)
    {
        var invalid = Path.GetInvalidFileNameChars();
        var sb = new System.Text.StringBuilder(name.Length);
        foreach (var c in name)
            sb.Append(Array.IndexOf(invalid, c) >= 0 || c is ' ' or '\\' or '/' ? '_' : c);
        return sb.ToString().Trim('_');
    }

    // ─── Helpers ──────────────────────────────────────────────────────

    /// <summary>
    /// Force the concurrent-read flag on every mounted VFS reader. CUE4Parse
    /// sets <c>IsConcurrent=true</c> via PostLoadReader on current builds so
    /// each Extract clones the container stream instead of sharing one — the
    /// precondition for parallel package loading. A defensive pass after mount
    /// guarantees that for any pinned build that does not, at no cost when
    /// already true. Wrapped in try/catch: listing mounted VFS failing is not
    /// fatal for sequential exports (threads=1), which remain safe either way.
    /// </summary>
    private static void EnsureConcurrentReaders()
    {
        try
        {
            foreach (var vfs in _provider!.MountedVfs)
            {
                try { vfs.IsConcurrent = true; }
                catch { /* older builds without the flag — reads stay shared */ }
            }
        }
        catch { /* best effort — only matters for multi-worker exports */ }
    }

    private static void EnsureProvider()
    {
        if (_provider == null)
            throw new InvalidOperationException("Not initialized. Send 'init' command first.");
    }

    // ─── Throttled progress ───────────────────────────────────────────
    /// <summary>
    /// Per-handler progress pacing. Emits on the 0th and final asset, every
    /// 10th asset (the historical folder-export cadence), or whenever at
    /// least <see cref="TimeFloorMs"/> has passed — whichever comes first.
    /// Parallel workers call this concurrently; the rate-limiter keeps the
    /// per-message stdout + Python dispatch cost constant instead of
    /// multiplying with worker count.
    /// </summary>
    private sealed class ProgressPacer
    {
        public const int TimeFloorMs = 150;
        private object _lock = new();
        private long _lastEmitMs = long.MinValue; // forces an early first emit

        public void Emit(int done, int total, string message)
        {
            if (done <= 0 || done >= total)
            {
                Respond(new { type = "progress", current = done, total, message });
                return;
            }
            if (done % 10 == 0)
            {
                lock (_lock) _lastEmitMs = Environment.TickCount64;
                Respond(new { type = "progress", current = done, total, message });
                return;
            }
            var now = Environment.TickCount64;
            lock (_lock)
            {
                if (now - _lastEmitMs < TimeFloorMs) return;
                _lastEmitMs = now;
            }
            Respond(new { type = "progress", current = done, total, message });
        }
    }

    private static void Respond(object obj)
    {
        var json = System.Text.Json.JsonSerializer.Serialize(obj, _writeOpts);
        lock (_respondLock)
        {
            Console.Out.WriteLine(json);
            Console.Out.Flush();
        }
        CheckForVersionMismatchHint(json);
    }

    private static void RespondError(string message)
    {
        Respond(new { type = "error", message });
    }

    // Tally version-mismatch-shaped messages. The struct-layout-doesn't-match
    // signature is unmistakable: reads return sizes that are negative or bigger
    // than what's left in the archive. Once we hit a small threshold, emit a
    // single distinct message so the GUI can prompt the user to pick another
    // UE version. Re-arms on the next init.
    private static void CheckForVersionMismatchHint(string json)
    {
        if (_versionMismatchHintEmitted) return;
        if (!json.Contains("\"type\":\"warning\"") && !json.Contains("\"type\":\"error\"")) return;

        bool matched = false;
        foreach (var p in _versionMismatchPatterns)
        {
            if (json.Contains(p)) { matched = true; break; }
        }
        if (!matched) return;

        _versionMismatchCount++;
        if (_versionMismatchCount < VersionMismatchHintThreshold) return;

        _versionMismatchHintEmitted = true;
        var hint = $"Detected {_versionMismatchCount} read errors typical of a UE-version mismatch. " +
                   $"The current selection ({_currentUeVersion}) likely doesn't match this game's engine version. " +
                   "Try a different UE Version (e.g. an adjacent UE5_x or UE4_x) and re-mount.";
        Respond(new
        {
            type = "version_warning",
            message = hint,
            current_version = _currentUeVersion,
            error_count = _versionMismatchCount,
        });
    }
}
