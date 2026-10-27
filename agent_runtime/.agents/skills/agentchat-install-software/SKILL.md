---
name: agentchat-install-software
description: Install or transfer Classic Mac software onto the Macintosh attached to AgentChat, preserving forks and Finder metadata and verifying that the application runs.
---

# Install Classic Macintosh software

Read imac_get_context for the connected machine and capabilities. Use the
original downloaded archive with imac_inspect_archive before conversion.
Inspection returns format, SHA-256, logical HFS paths, both fork sizes,
application type/creator and import problems without changing the Macintosh.
Follow next_start for additional entries. The paths are names inside the
archive; destination is a new absolute HFS folder on the connected Macintosh.

## Choose an import

Prefer imac_import_archive with the inspected SHA-256. Keep the original archive
on the host for verification or recovery. For a large collection, use paths to
select the application and its required sibling sounds, fonts, maps, plug-ins
or documentation. Selecting a folder includes its subtree; selecting a file
includes both forks. The importer retains archive-relative parent directories.
It cannot overwrite an existing destination; choose a fresh folder and preserve
the user's existing installation.

For example, after discovering these exact names with inspection:

```json
{"host_path":"/tmp/game.sit","destination":"Disk:Games:New game","paths":["Game/Game","Game/Sounds","Game/Maps"],"expected_sha256":"the inspected digest"}
```

The importer returns native paths, fork sizes, hashes and Finder metadata.
Empty data plus a nonempty resource fork is normal for many 68K applications.
Treat APPL/creator metadata and real resource contents as evidence; do not
declare corruption based on an empty data fork. The resource fork of a host
AppleDouble sidecar starts inside its container, not at byte zero of that file.

If an import fails, inspect the named problem. Select a needed subset for an
entry/size limit or conflicting auxiliary entries; use a different archive if
the original fails integrity checks. Installed unar handles BinHex and nested
StuffIt: there is ordinarily no need to write another decoder. Keep binary
forks out of model arguments when the archive helper can transfer them directly.
If a download returned zero bytes or HTML, correct the download before decoding.

## Verify the result

Inspect imported files with imac_file_info and compare both fork sizes and
Finder metadata with the import report. Use bounded imac_read_file base64
slices if a byte-level check is needed. Open the application through Finder,
then verify its process and screenshot. On a VM, use available desktop input
to enter the intended game/document and demonstrate one meaningful action.
On a physical Mac, report any action that needs unavailable input tooling.

Keep verification proportional to the task: a running title screen establishes
launch, not successful gameplay or a complete installation. Preserve the
user's other programs and installed files when cleaning up temporary artifacts.
