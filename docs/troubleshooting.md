# Troubleshooting

If you hit something that isn't covered here, please
[open an issue](https://github.com/exterminathan/EfficientAssetRipper/issues/new)
with the affected log file from `logs/` (with AES keys redacted).

---

## "Everything is not running" / scan returns nothing

EfficientAssetRipper relies on the [Everything](https://www.voidtools.com/)
desktop app for instant file search on **Windows**. If it isn't running,
asset discovery falls back to the built-in file walker, which is slow on
large folders but finds the same files.

**Linux/macOS:** Everything is Windows-only software. Scans always use the
built-in walker there (the Everything row in the setup wizard reports this),
so a missing or disabled Everything is not a fault on those platforms.

Fixes (Windows):

1. Confirm Everything is installed and running (look for the magnifying-glass
   icon in the system tray).
2. Open **Settings** → check the **Everything DLL** path. The default is
   `C:\Program Files\Everything\Everything64.dll`.
3. Click **Test All Paths & SDKs** in Settings. The "Everything IPC"
   row should report **PASS**.
4. If you have multiple Everything installs, make sure the running
   instance and the DLL path point to the same version.

---

## "Blender exited with code N"

Per-asset Blender failures are logged in `logs/batch_*.log`. Common cases:

| Code | Likely cause | Fix |
|------|-------------|-----|
| 1 | Generic Python exception inside Blender | Check `logs/batch_*.log` for the asset's `error` field |
| 2 | Wrong Blender version (PSK addon missing or incompatible) | Use Blender 4.0+; verify the addon name in Settings → Processing |
| -1 | Timed out | Bump **Settings → Processing → Timeout per Asset** |
| 0xC0000005 | Native crash, often a malformed PSK | Try one asset in isolation; report with the PSK if reproducible |

Manual reproduction: copy the `cmd` line from the log file, run it from a
shell, and you'll see the full Blender stderr.

---

## "AES key didn't unlock archive"

The Unpacker reports "X archives need AES keys" when one or more `.pak`
files refused decryption. AES keys live in the active profile only —
the Unpacker tab no longer has its own editor.

Fixes:

1. The **fastest path**: when **Mount Archives** detects encrypted
   archives, a dialog opens automatically with the affected archive
   names + GUIDs. Paste the matching key, click **OK**, and the
   profile is updated and remounted in one go.
2. Manual edit: open **Manage Profiles**, scroll to **AES Keys**.
3. Verify the GUID and key are entered correctly. The key is a 64-char
   hex string (32 bytes); the GUID is `00000000000000000000000000000000`
   for most games' main key.
4. Try **0x** prefix on the key — some sources include it, some don't.
   Both work, the unpacker normalises.
5. If the same archive is decrypted fine by another tool but not us,
   check the **UE Version** in the profile — wrong version = "wrong"
   key even if the key itself is right.
6. Per-archive AES keys (multiple GUIDs) are supported — add a row per
   key.

> ⚠️ **Never paste AES keys into a public bug report.** The crash
> reporter and `redact_sensitive` strip keys from logs, but pasted text
> won't go through that filter.

---

## Asset shows "no_props" or "no_materials"

These statuses come from the **scanner**, not Blender:

- **no_props**: a `.psk` exists but no companion `.props.txt`. Cause:
  the unpacker exported the mesh but skipped its property file (look
  for the corresponding mesh in the unpacker tree and re-export with
  the **Props** format checkbox on).
- **no_materials**: props file was found but lists zero materials, AND
  the binary `MATT0000` chunk inside the PSK was empty. This is rare;
  usually means the asset is something other than a renderable mesh
  (e.g. a collision shape).

For **no_props**, opening **Asset Detail** (double-click in the browser)
shows the exact paths the resolver looked for.

---

## Texture export warnings (per-asset decode failures)

| Warning | Meaning | Fix |
|---------|---------|-----|
| `Detex decompression failed: not initialized` | Was a CLI bug: BC7/BC6H textures routed to a Windows-only native library on Linux. **Fixed** — the CLI now uses the pure-C# decoder. Update to the current build and re-export; if it still appears, report it. |
| `Failed to decompress Zlib data (Expected: N, Result: 0)` (texture) | This specific asset's virtual-texture tile payload is unreadable even upstream of the tool — verified against two different CUE4Parse builds. Known in Islands of Insight: the 8 leftover `ASophia/Characters/.../old/...` character-skin textures that shipped in broken/partial form. The game itself doesn't read them (dead content from earlier builds); there is nothing to fix. |
| `There is no game file with the path "Paper2D/PlaceholderTextures/..."` | The asset references an engine placeholder texture not cooked into the game. Harmless for meshes etc.; the referenced file simply doesn't exist in the archives. |

Texture exports are per-asset: a decode failure on one file does not
affect any other asset in the batch, and the summary counts it.

---

## Crash dialog appeared — what now?

The dialog has three buttons:

- **Copy report** — full crash JSON to clipboard for pasting elsewhere.
- **Open GitHub issue** — opens a pre-filled new-issue page. Review the
  body before submitting; it includes the traceback, OS/Python/Qt
  versions, and the last 200 log lines.
- **Continue** — dismiss; the app may still be usable depending on
  what crashed.

A copy of the report is always written to `logs/crash_*.json` even if
you click **Continue**. The crash JSON has already been run through the
redaction filter (AES keys, hex blobs ≥ 32 chars are masked).

---

## How do I add a new game profile?

1. Click **Manage Profiles** (toolbar).
2. Click **New** → enter a name (e.g. "JediSurvivor"). The new profile
   is created on disk immediately with empty defaults.
3. Fill in:
   - **Game folder** — points at the `Pak/` (or equivalent) directory
     of the game install.
   - **UE Version** — pick from the dropdown, or type the exact
     `EGame` enum value (e.g. `GAME_UE5_3`).
   - **Mounted folder** — where the unpacker writes exported files.
     Also where the PSK Picker reads from.
   - **Output folder** — where Blender writes `.blend` files.
   - **AES Keys** — one row per GUID/key pair (see above).
4. Click **OK** to commit.

Path edits in the Unpacker tab don't persist back to the profile unless
**Auto-save Unpacker tab edits** is checked on that profile — that lets
you do one-off mounts without overwriting the saved values.

---

## My queue was interrupted

`JobManager` writes a checkpoint after every job. On the next launch,
if a checkpoint exists for the active profile, the app will prompt:

> Resume previous batch? — N asset(s) still pending

Pick **Yes** to load the remaining assets and start processing where
you left off. **No** discards the checkpoint. If the checkpoint is for
a different profile, it's left untouched and the prompt fires next time
you switch back to that profile.

The checkpoint file is at `cache/queue_checkpoint.json` — safe to
delete manually if you want a clean slate.

---

## Reset everything to defaults

- **Settings dialog** has a **Restore Defaults** button — repopulates
  every Settings field. Doesn't write until you click **OK**.
- **Color Scheme** dialog has **Reset to Default** for the active
  custom scheme (built-in schemes are immutable so the button is
  disabled there).
- **Profiles** can be deleted via Manage Profiles → **Delete**. There
  must be at least one profile, so delete-all isn't possible.
- **QSettings** lives in the Windows registry at
  `HKCU\Software\EfficientAssetRipper` — or an INI file at
  `~/.config/EfficientAssetRipper/EfficientAssetRipper.ini` on Linux.
  Deleting that key (registry key on Windows, INI file on Linux) resets
  everything to first-launch state.

---

## Logs

| File | Purpose |
|------|---------|
| `logs/batch_<ts>.log` | One entry per processed asset (NDJSON). Written by `JobManager`. |
| `logs/crash_<ts>.json` | Structured crash report. Written by the crash reporter on uncaught exceptions / Qt fatal messages. |

Both are redacted: AES keys, hex blobs, and field names containing
`key`/`aes`/`password`/`token`/`secret` are masked before write.
