---
name: agentchat-edit-files
description: Read, create or modify files on the Classic Macintosh attached to AgentChat using HFS paths and native fork-preserving operations.
---

# Work with Macintosh files

Use imac_get_context and mounted-volume HFS paths. Ordinary host filesystem
tools cannot read those paths. Paginate imac_list_directory using next_index;
imac_file_info distinguishes folders, both forks, Finder codes and lock state.

Read text through imac_read_file with encoding=text. It translates MacRoman/CR
to host text/LF. Use encoding=base64 for arbitrary bytes, executable content,
resource forks or byte-exact comparisons. Follow next_offset until the needed
range is complete; a truncated slice is not a complete file.

For a text correction, imac_replace_in_file performs an exact replacement and
requires a unique match unless replace_all is selected. For a whole fork,
imac_write_file writes atomically and preserves the other fork and metadata.
Use expected_sha256 from a complete read to detect a stale version; a slice's
hash covers only that slice. create_only protects a new file from replacing
an existing one. Paths/components must fit the native limits.

For a large raw file already on the relay host, imac_import_file accepts an
absolute host_path and Macintosh HFS destination. It streams at most 64 MiB,
keeps binary content out of model arguments/results, and verifies the source
SHA-256 plus native size/CRC before committing. Its expected_sha256 checks the
HOST source rather than a previous destination. create_only defaults true;
explicit false replaces only the selected fork while preserving the other fork
and Finder metadata. A raw file does not include an application's forks and
Finder metadata; transfer software through imac_import_archive. The host must
be shared with a separately hosted backend for host_path to resolve there.
Large transfers can take minutes, with a bounded 600-second provider timeout.
If it times out, inspect the destination before retrying; do not send another
prompt to poll progress. Ordinary writes remain limited to 16 MiB.

Inspect the resulting content and file_info after an edit. Finder type/creator
changes use imac_set_file_info and preserve the forks. A resource-only Classic
application does not need invented data-fork bytes. For copying applications
or many files with forks, use the software-install skill and archive importer.

mkdir creates one folder whose parent must exist. rename stays in the same
parent. delete removes one exact file or an empty directory; it does not recurse.
If an operation times out or the connection changes, inspect the current file
before retrying a mutation. Keep unrelated files and the user's current drafts.
