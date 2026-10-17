using System;
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
    private static string _textureFormat = "png"; // png or tga
    private static string _audioFormat = "wav";   // wav or ogg
    private static string _gameDir = "";          // current game directory (for loose file reads)
    private static Dictionary<string, string> _looseFiles = new(StringComparer.OrdinalIgnoreCase); // VFS path → disk path for files CUE4Parse doesn't discover
    private static string? _vgmstreamPath;         // cached path to vgmstream-cli
    private static Dictionary<string, string> _wemIdToName = new(StringComparer.OrdinalIgnoreCase); // WEM numeric ID → debug name
    private static Dictionary<string, string> _fmodBankCache = new(StringComparer.OrdinalIgnoreCase); // bank VFS path → materialized temp file
    private static string? _wemNameCachePath;       // per-game disk cache for WEM name map
    private static bool _wemMapBuilt;               // true once the map has been built/loaded this session
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
    /// Run <paramref name="action"/> on a 4 MB-stack background thread to avoid
    /// StackOverflow on deeply-nested CUE4Parse object graphs. Caps active
    /// threads at <see cref="MaxConcurrentSerializeThreads"/>; on overflow
    /// emits a warning and returns null. capturedError is populated if the
    /// action threw before completing.
    /// </summary>
    private static T? RunOnBoundedThread<T>(Func<T> action, int timeoutMs, out Exception? capturedError) where T : class
    {
        T? result = null;
        Exception? err = null;
        Thread thread;
        lock (_serializeThreadsLock)
        {
            _serializeThreads.RemoveWhere(t => !t.IsAlive);
            if (_serializeThreads.Count >= MaxConcurrentSerializeThreads)
            {
                Respond(new { type = "warning", message = "Serialize thread cap reached; skipping" });
                capturedError = null;
                return null;
            }
            thread = new Thread(() =>
            {
                try { result = action(); }
                catch (Exception ex) { err = ex; }
                finally
                {
                    lock (_serializeThreadsLock)
                    {
                        _serializeThreads.Remove(Thread.CurrentThread);
                    }
                }
            }, 4 * 1024 * 1024)
            {
                IsBackground = true,
                Priority = ThreadPriority.BelowNormal
            };
            _serializeThreads.Add(thread);
            thread.Start();
        }
        thread.Join(timeoutMs);
        capturedError = err;
        return result;
    }

    private static string? RunSerializeOnBoundedThread(Func<string> action, int timeoutMs)
        => RunOnBoundedThread(action, timeoutMs, out _);

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

    // Hard cap on a single NDJSON command line. Anything bigger is treated as
    // adversarial input (stuck/runaway parent) — drop the line and respond with
    // an error rather than buffering forever.
    private const int MaxStdinLineBytes = 4 * 1024 * 1024;

    // Bound concurrent serialization threads. Each is a fresh 4 MB stack so
    // unbounded growth = OOM. If we hit the cap, refuse new work and warn.
    private const int MaxConcurrentSerializeThreads = 8;
    private static readonly HashSet<Thread> _serializeThreads = new();
    private static readonly object _serializeThreadsLock = new();

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
                await RunExportWithCancelSupport(reader, () => HandleExport(cmd));
                break;
            case "export_folder":
                await RunExportWithCancelSupport(reader, () => HandleExportFolder(cmd));
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
                _cts = new CancellationTokenSource();
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
    private static async Task RunExportWithCancelSupport(StreamReader reader, Action exportAction)
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
                        Respond(new { type = "cancelled" });
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
    private static Task HandleExport(JObject cmd)
    {
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
        _audioFormat = cmd.Value<string>("audio_format") ?? "wav";

        Directory.CreateDirectory(outputDir);

        int total = paths.Count, current = 0;
        var succeeded = new List<string>();
        var failed = new List<object>();
        var visited = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        _exportWarningCount = 0;

        foreach (var assetPath in paths)
        {
            if (_cts.Token.IsCancellationRequested) break;
            current++;
            Respond(new { type = "progress", current, total, message = $"Exporting: {Path.GetFileName(assetPath)}" });

            try
            {
                var exported = ExportAsset(assetPath, outputDir, formats, visited);
                if (exported)
                    succeeded.Add(assetPath);
                else
                    failed.Add(new { path = assetPath, error = "Unsupported asset type or no exportable data" });
            }
            catch (Exception ex)
            {
                failed.Add(new { path = assetPath, error = SanitizeMessage(ex.Message) });
            }
        }

        var cancelled = _cts.Token.IsCancellationRequested;
        Respond(new
        {
            type = "export_done",
            succeeded,
            failed,
            total = paths.Count,
            cancelled,
            remaining = cancelled ? total - current : 0,
            warning_count = _exportWarningCount,
        });
        return Task.CompletedTask;
    }

    // ─── Export folder (bulk) ─────────────────────────────────────────
    private static Task HandleExportFolder(JObject cmd)
    {
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
        _audioFormat = cmd.Value<string>("audio_format") ?? "wav";

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

        int total = assetPaths.Count, current = 0;
        var succeeded = new List<string>();
        var failed = new List<object>();
        var visited = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        _exportWarningCount = 0;

        Respond(new { type = "progress", current = 0, total, message = $"Found {total} assets to export" });

        foreach (var assetPath in assetPaths)
        {
            if (_cts.Token.IsCancellationRequested) break;
            current++;

            if (current % 10 == 0 || current == total)
                Respond(new { type = "progress", current, total, message = $"Exporting: {Path.GetFileName(assetPath)}" });

            try
            {
                var exported = ExportAsset(assetPath, outputDir, formats, visited);
                if (exported)
                    succeeded.Add(assetPath);
            }
            catch (Exception ex)
            {
                failed.Add(new { path = assetPath, error = $"{ex.GetType().Name}: {SanitizeMessage(ex.Message)}" });
            }
        }

        var cancelled = _cts.Token.IsCancellationRequested;
        Respond(new
        {
            type = "export_done",
            succeeded,
            failed,
            total = assetPaths.Count,
            cancelled,
            remaining = cancelled ? total - current : 0,
            warning_count = _exportWarningCount,
        });
        return Task.CompletedTask;
    }

    // ─── Core export logic ────────────────────────────────────────────

    // Meshes reference materials in other packages, materials reference parent
    // materials and textures. Exporting one mesh must pull that whole chain or
    // the Python scan pipeline finds a props file pointing at assets that were
    // never extracted. Depth 8 outlasts any real parent chain; the visited set
    // is the actual cycle guard.
    private const int MaxDependencyDepth = 8;

    private static readonly Regex _depRefRe = new(@"^(?:Material\w*|Texture2D)'", RegexOptions.Compiled);

    private static bool ExportAsset(string gamePath, string outputDir, Dictionary<string, bool> formats,
                                    HashSet<string>? visited = null, int depth = 0)
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
        visited ??= new HashSet<string>(StringComparer.OrdinalIgnoreCase);
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
                _exportWarningCount++;
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
        byte[]? data = null;

        // Try CUE4Parse provider first
        if (_provider!.TrySaveAsset(gamePath, out var providerData))
        {
            data = providerData;
        }
        // Fall back to loose file on disk
        else if (_looseFiles.TryGetValue(gamePath, out var diskPath) && File.Exists(diskPath))
        {
            data = File.ReadAllBytes(diskPath);
        }

        if (data == null) return false;

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

    // ─── FileMediaSource export ───────────────────────────────────────
    private static bool ExportFileMediaSource(string gamePath, string outputDir)
    {
        var resolved = ResolveFileMediaSource(gamePath, out var err);
        if (resolved == null)
        {
            Respond(new { type = "warning", message = $"{Path.GetFileName(gamePath)}: {err ?? "FileMediaSource resolve failed"}" });
            return false;
        }
        if (!_provider!.TrySaveAsset(resolved, out var data) || data == null)
        {
            Respond(new { type = "warning", message = $"{Path.GetFileName(gamePath)}: resolved {resolved} not in VFS" });
            return false;
        }
        var name = SanitizeName(Path.GetFileName(resolved));
        if (string.IsNullOrEmpty(name)) return false;
        var outPath = SafeJoin(outputDir, name);
        File.WriteAllBytes(outPath, data);
        return true;
    }

    // ─── Mesh export ──────────────────────────────────────────────────
    private static bool ExportMesh(UObject mesh, string outputDir)
    {
        var options = new ExporterOptions(); // ActorX → PSK/PSKX
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
        var raw = texture.Decode();
        if (raw == null) return false;
        var decoded = TextureEncoder.ToSkBitmap(raw);
        if (decoded == null) return false;

        if (_textureFormat == "tga")
        {
            // Export as raw BGRA TGA
            var outPath = SafeJoin(outputDir, $"{texture.Name}.tga");
            WriteTga(decoded, outPath);
        }
        else
        {
            var outPath = SafeJoin(outputDir, $"{texture.Name}.png");
            using var fs = File.Create(outPath);
            decoded.Encode(SkiaSharp.SKEncodedImageFormat.Png, 100).SaveTo(fs);
        }
        return true;
    }

    private static void WriteTga(SkiaSharp.SKBitmap bmp, string outPath)
    {
        int w = bmp.Width, h = bmp.Height;
        using var fs = File.Create(outPath);
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

        // Pixel data — BGRA. Skia's default 8888 layout is BGRA on little-endian
        // platforms, matching the TGA output we want, so we can write the
        // backing buffer directly. For other layouts, fall back to per-pixel.
        if (bmp.ColorType == SkiaSharp.SKColorType.Bgra8888 ||
            (bmp.ColorType == SkiaSharp.SKColorType.Rgba8888 && BitConverter.IsLittleEndian))
        {
            // Bgra8888: bytes are already in B,G,R,A order regardless of endian.
            if (bmp.ColorType == SkiaSharp.SKColorType.Bgra8888)
            {
                fs.Write(bmp.Bytes);
                return;
            }
            // Rgba8888 little-endian: swap R<->B per pixel into a scratch buffer.
        }

        // Fallback: per-pixel write using GetPixel.
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
        Dictionary<string, bool> formats, HashSet<string> visited, int depth)
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

    /// <summary>Convert an audio file to WAV (or OGG if vgmstream supports it) using vgmstream-cli.</summary>
    private static bool ConvertWithVgmstream(string inputPath, string outputDir, string targetFormat)
    {
        InitVgmstream();
        if (_vgmstreamPath == null) return false;

        var baseName = Path.GetFileNameWithoutExtension(inputPath);
        // vgmstream always outputs WAV. SafeJoin enforces containment in case
        // baseName carries traversal from an upstream filename.
        var outPath = SafeJoin(outputDir, $"{baseName}.wav");

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
            proc.WaitForExit(30_000);
            return proc.ExitCode == 0 && File.Exists(outPath);
        }
        catch (Exception ex)
        {
            Respond(new { type = "warning", message = $"vgmstream conversion failed for {Path.GetFileName(inputPath)}: {SanitizeMessage(ex.Message)}" });
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
        _wemMapBuilt = true;

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
                    Respond(new { type = "info", message = $"WEM names: loaded {_wemIdToName.Count} entries from cache" });
                    return;
                }
            }
            catch { /* ignore corrupt cache — fall through to rebuild */ }
        }

        BuildWemNameMapFromBanks();
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
        EnsureProvider();
        var outputDir = cmd.Value<string>("output_dir") ?? throw new ArgumentException("output_dir required");
        var audioFormat = cmd.Value<string>("audio_format") ?? "wav";
        var entries = cmd["entries"] as JArray ?? throw new ArgumentException("entries required");

        _audioFormat = audioFormat;
        Directory.CreateDirectory(outputDir);

        int total = entries.Count, current = 0;
        var succeeded = new List<string>();
        var failed = new List<object>();

        Respond(new { type = "progress", current = 0, total, message = $"Exporting {total} audio files..." });

        foreach (var entry in entries)
        {
            if (_cts.Token.IsCancellationRequested) break;
            current++;

            var wemPath = entry.Value<string>("wem_vfs_path") ?? "";
            var targetName = entry.Value<string>("target_name") ?? "";
            var targetFolder = entry.Value<string>("target_folder") ?? "";

            // Sanitize parent-supplied path components: each folder segment is
            // run through SanitizeName, traversal segments are rejected, and
            // SafeJoin enforces containment in outputDir.
            var safeName = SanitizeName(targetName);
            if (string.IsNullOrEmpty(safeName))
            {
                failed.Add(new { path = wemPath, error = "Invalid target_name" });
                continue;
            }

            string safeSubDir;
            try
            {
                safeSubDir = SafeJoinFolderSegments(outputDir, targetFolder);
            }
            catch (Exception ex)
            {
                failed.Add(new { path = wemPath, error = $"Invalid target_folder: {SanitizeMessage(ex.Message)}" });
                continue;
            }

            if (current % 10 == 0 || current == total)
                Respond(new { type = "progress", current, total, message = $"Exporting audio: {safeName}" });

            try
            {
                if (!_provider!.TrySaveAsset(wemPath, out var data))
                {
                    failed.Add(new { path = wemPath, error = "File not found in VFS" });
                    continue;
                }

                Directory.CreateDirectory(safeSubDir);

                var wemTempPath = SafeJoin(safeSubDir, $"{safeName}.wem");
                File.WriteAllBytes(wemTempPath, data);

                var converted = ConvertWithVgmstream(wemTempPath, safeSubDir, audioFormat);
                if (converted)
                {
                    try { File.Delete(wemTempPath); } catch { }
                }
                // Keep .wem as fallback if conversion failed
                succeeded.Add(safeName);
            }
            catch (Exception ex)
            {
                failed.Add(new { path = wemPath, error = SanitizeMessage(ex.Message) });
            }
        }

        var cancelled = _cts.Token.IsCancellationRequested;
        Respond(new
        {
            type = "export_done",
            succeeded,
            failed,
            total = entries.Count,
            cancelled,
            remaining = cancelled ? entries.Count - current : 0,
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
    private static string? GetFmodBankLocalPath(string vfsPath, out string? error)
    {
        error = null;
        if (_looseFiles.TryGetValue(vfsPath, out var diskPath) && File.Exists(diskPath))
            return diskPath;
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
        EnsureProvider();
        var outputDir = cmd.Value<string>("output_dir") ?? throw new ArgumentException("output_dir required");
        var entries = cmd["entries"] as JArray ?? throw new ArgumentException("entries required");

        Directory.CreateDirectory(outputDir);

        int total = entries.Count, current = 0;
        var succeeded = new List<string>();
        var failed = new List<object>();

        Respond(new { type = "progress", current = 0, total, message = $"Exporting {total} FMOD stream(s)..." });

        foreach (var entry in entries)
        {
            if (_cts.Token.IsCancellationRequested) break;
            current++;

            var bankPath = entry.Value<string>("bank_vfs_path") ?? "";
            var streamIndex = entry.Value<int?>("stream_index") ?? 0;
            var targetName = entry.Value<string>("target_name") ?? "";
            var targetFolder = entry.Value<string>("target_folder") ?? "";

            var safeName = SanitizeName(targetName);
            if (string.IsNullOrEmpty(safeName) || streamIndex <= 0)
            {
                failed.Add(new { path = bankPath, error = "Invalid target_name or stream_index" });
                continue;
            }

            string safeSubDir;
            try
            {
                safeSubDir = SafeJoinFolderSegments(outputDir, targetFolder);
            }
            catch (Exception ex)
            {
                failed.Add(new { path = bankPath, error = $"Invalid target_folder: {SanitizeMessage(ex.Message)}" });
                continue;
            }

            if (current % 10 == 0 || current == total)
                Respond(new { type = "progress", current, total, message = $"Exporting FMOD audio: {safeName}" });

            try
            {
                var local = GetFmodBankLocalPath(bankPath, out var err);
                if (local == null)
                {
                    failed.Add(new { path = bankPath, error = err ?? "bank unavailable" });
                    continue;
                }

                Directory.CreateDirectory(safeSubDir);
                var outPath = SafeJoin(safeSubDir, $"{safeName}.wav");
                var run = RunVgmstreamCapture(new[] { "-s", streamIndex.ToString(), "-o", outPath, local }, 60_000);
                if (run == null || run.Value.ExitCode != 0 || !File.Exists(outPath))
                {
                    failed.Add(new { path = $"{bankPath}#{streamIndex}", error = "vgmstream conversion failed" });
                    continue;
                }
                succeeded.Add(outPath);
            }
            catch (Exception ex)
            {
                failed.Add(new { path = $"{bankPath}#{streamIndex}", error = SanitizeMessage(ex.Message) });
            }
        }

        var cancelled = _cts.Token.IsCancellationRequested;
        Respond(new
        {
            type = "export_done",
            succeeded,
            failed,
            total = entries.Count,
            cancelled,
            remaining = cancelled ? entries.Count - current : 0,
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
            current++;

            var vfsPath = entry.Value<string>("vfs_path") ?? "";
            var kind = (entry.Value<string>("kind") ?? "raw_video").ToLowerInvariant();
            if (string.IsNullOrEmpty(vfsPath))
            {
                failed.Add(new { path = vfsPath, error = "Empty vfs_path" });
                continue;
            }

            if (current % 5 == 0 || current == total)
                Respond(new { type = "progress", current, total, message = $"Exporting video: {Path.GetFileName(vfsPath)}" });

            try
            {
                if (kind == "raw_video")
                {
                    if (!_provider!.TrySaveAsset(vfsPath, out var data) || data == null)
                    {
                        failed.Add(new { path = vfsPath, error = "File not found in VFS" });
                        continue;
                    }
                    var name = SanitizeName(Path.GetFileName(vfsPath));
                    if (string.IsNullOrEmpty(name))
                    {
                        failed.Add(new { path = vfsPath, error = "Invalid filename" });
                        continue;
                    }
                    var outPath = SafeJoin(outputDir, name);
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
                    if (!_provider!.TrySaveAsset(resolved, out var data) || data == null)
                    {
                        failed.Add(new { path = vfsPath, error = $"Resolved file not found: {resolved}" });
                        continue;
                    }
                    var name = SanitizeName(Path.GetFileName(resolved));
                    if (string.IsNullOrEmpty(name))
                    {
                        failed.Add(new { path = vfsPath, error = "Invalid filename after resolve" });
                        continue;
                    }
                    var outPath = SafeJoin(outputDir, name);
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

    private static void EnsureProvider()
    {
        if (_provider == null)
            throw new InvalidOperationException("Not initialized. Send 'init' command first.");
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
