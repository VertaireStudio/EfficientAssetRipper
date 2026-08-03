# Building UE5 Test Builds

How to package a UE5 project into every archive format EfficientAssetRipper
supports, so each code path — loose files, legacy `.pak`, IoStore, AES-encrypted
indexes, chunked builds — can be demoed or regression-tested against a game you
control.

Everything here assumes UE 5.x. The Crypto plugin and IoStore switches exist in
UE 4.25+ as well, but the menu paths differ slightly.

---

## Why bother

The public test corpus for an unpacker is other people's shipped games, which
you can't check in, can't share, and can't re-cook when a code path changes.
A throwaway UE5 project with a handful of meshes and materials gives you a
redistributable fixture that exercises the whole pipeline: mount → browse →
export → resolve → Blender.

---

## Setting the AES key

Encryption is configured in **Edit → Project Settings → Project → Crypto**.

1. Click **Generate New Encryption Key**.
2. Enable **Encrypt Pak Index**. This one is mandatory — see below.
3. Optionally enable **Encrypt Asset Files** (or **Encrypt All Asset Files**)
   and **Encrypt Ini Files**. The latter also encrypts `AssetRegistry.bin`,
   which is closer to what shipped games look like.
4. Restart the editor. Crypto settings are read at cook time, not at launch.

The key is written to `Config/DefaultCrypto.ini` as **base64**:

```ini
[/Script/CryptoKeys.CryptoKeysSettings]
EncryptionKey=Gk3nR0...==
bEncryptPakIndex=True
bEncryptAllAssetFiles=True
```

### Encrypt Pak Index is what triggers the AES prompt

CUE4Parse only populates `IFileProvider.RequiredKeys` when an archive's *index*
is encrypted — that's what the CLI reports as `unmounted_count` in its
`init_done` reply ([`cue4parse_cli/Program.cs`](../cue4parse_cli/Program.cs)),
and what makes the Unpacker emit `aes_keys_required` and open the AES prompt
dialog.

If you encrypt only the asset files and leave the index in the clear, the
archive mounts silently with zero required keys and the prompt never appears —
the failure surfaces later as garbled asset reads instead. Any build meant to
demo the AES flow needs **Encrypt Pak Index** checked.

### Converting the key to the format the app expects

`DefaultCrypto.ini` stores base64; the profile dialog and the CLI want hex.
Convert with PowerShell:

```powershell
$b64 = 'PASTE_THE_BASE64_HERE=='
'0x' + [BitConverter]::ToString([Convert]::FromBase64String($b64)).Replace('-','')
```

The result is `0x` followed by 64 hex characters (32 bytes, AES-256). Paste
that into **Manage Profiles → AES Keys**, or into the prompt dialog that opens
on mount.

Leave the GUID as `00000000000000000000000000000000` — UE's primary encryption
key uses the null GUID, which matches the CLI's default when the field is
omitted. Secondary keys are a different story: if you set
`SecondaryEncryptionKeys=(Name="...",Guid="...",Key="...")` in
`DefaultCrypto.ini` to encrypt individual chunks under separate keys, each one
needs its own row with its real GUID.

---

## The packaging matrix

All switches below live in **Project Settings → Packaging** unless noted.
Cook each variant into its own staging directory so you end up with parallel
folders and one EfficientAssetRipper profile pointed at each.

| Variant | Use Pak File | Use Io Store | Crypto | Output | Exercises |
|---------|--------------|--------------|--------|--------|-----------|
| Loose cooked | off | off | n/a | `Content/` tree of `.uasset` / `.uexp` / `.ubulk` | Directory structure preserved 1:1, no unpack step |
| Legacy pak | on | off | off | `pakchunk0-Windows.pak` | Classic `.pak` mount + browse |
| IoStore | on | on | off | `.pak` + `.utoc` + `.ucas` | UE5 default; the shape most shipped games take |
| IoStore + encrypted | on | on | on | same, index encrypted | AES prompt → merge into profile → remount |
| Chunked + encrypted | on | on | on + **Generate Chunks** | `pakchunk0..N` | Multi-archive mounting, secondary keys, per-chunk failures |

Encryption is a pak/IoStore feature. A loose cooked build cannot be encrypted,
which is why that cell is `n/a`.

Two extra switches worth enabling on at least one variant:

- **Create Compressed Cooked Packages** — Oodle-compressed chunks. Exercises
  CUE4Parse's Oodle DLL auto-download on first use.
- **Generate Chunks** — needs Primary Asset Labels assigned to a few assets to
  actually split anything. Produces multiple `pakchunkN` files, so the
  unmounted-archive list in the AES prompt shows more than one row.

---

## Where to point `game_dir`

| Build variant | Profile `game_dir` |
|---------------|--------------------|
| Loose cooked | `<Staged>/Windows/<Project>/Content` |
| Pak / IoStore / encrypted | `<Staged>/Windows/<Project>/Content/Paks` |

Also set **UE Version** on the profile to match the engine you cooked with.
Auto-detect reads the shipped executable, so it works on a full staged build
but not on a bare `Paks` folder you copied out by hand.

---

## Cooking the variants

The reliable path is to toggle the checkboxes in Project Settings between
builds and package normally (**Platforms → Windows → Package Project**), each
time into a fresh output folder. Tedious, but it's the configuration UE
actually reads.

For repeat runs, drive UAT directly and vary the switches per invocation:

```powershell
& "$env:UE_ROOT\Engine\Build\BatchFiles\RunUAT.bat" BuildCookRun `
  -project="C:\Path\To\MyGame.uproject" `
  -platform=Win64 -clientconfig=Shipping `
  -cook -stage -package -build `
  -pak -iostore -compressed `
  -stagingdirectory="C:\Builds\iostore-encrypted"
```

Drop `-iostore` for a legacy pak build; drop `-pak` as well for loose cooked
files. Crypto is still picked up from `Config/DefaultCrypto.ini`, so toggling
encryption on and off between UAT runs means editing that file (or the Crypto
settings page) rather than passing a flag. Verify the exact flag set against
your engine version — UAT's switches drift between releases.

---

## Verifying each build mounted correctly

For each profile, open the **Unpacker** tab and click **Mount Archives**, then
check the status line:

| Expectation | Meaning |
|-------------|---------|
| `N archives, M files` with no key prompt | Unencrypted variant mounted cleanly |
| `X archives need AES keys` + prompt dialog | Encrypted variant; index encryption is working |
| `0 archives` on a loose build but files browsable | Expected — loose `.uasset` files are registered directly, not as archives |
| `0 archives, 0 files` | Wrong `game_dir`, or a UE version mismatch |

The loose-cooked variant is the least-travelled path in the codebase and worth
smoke-testing first: the CLI's explicit loose-file scan only covers
`.upk` / `.wem` / `.ewem` / `.bnk`, so loose `.uasset` discovery depends
entirely on CUE4Parse's own directory scan in `DefaultFileProvider`.

---

## Publishing a demo build

If the build ships alongside the docs as a public fixture, publish the hex key
with it — the whole point is that readers can reproduce the AES flow. That's
the opposite of the rule for real games: `logs/` and issue reports should have
keys redacted, per [troubleshooting.md](troubleshooting.md).

Keep the fixture small. A few meshes with distinct texture suffixes (`_C`,
`_N`, `_ORM`) covers the resolver's classification path, and a material with a
parent chain covers `_resolve_parent_chain`, without shipping a multi-GB pak.
