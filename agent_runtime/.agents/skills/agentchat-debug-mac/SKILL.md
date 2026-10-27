---
name: agentchat-debug-mac
description: Diagnose applications or the desktop on the Macintosh attached to AgentChat using native process, memory and screenshot inspection and optional QEMU debugging.
---

# Inspect and debug the Macintosh

Start with imac_get_context, then choose the evidence needed for the reported
problem: screenshot, process list, AgentChat window bounds, heap diagnostics
or the recent client event log. imac_list_windows covers AgentChat's windows,
not every application's internal controls. Native process IDs identify a
particular launch; reacquire them if an application quits or restarts.

imac_process_info exposes the application's native partition base, size,
launch identity and CPU ticks. imac_read_process_memory reads relative to that
partition; imac_find_process_memory searches a bounded region for exact text
or bytes. Follow pagination and keep reads focused. Live bytes can change;
separate system/CFM allocations are not enumerated by a partition read.
These tools inspect memory and do not write it or set process breakpoints.

## Optional VM debugger

Use vm_* only when get_context reports that the relay enables them. vm_pause
halts the entire machine, not a selected application. Its lease expires if the
relay remains alive. While paused, use vm_registers/vm_read_memory and then
vm_resume; native Mac tools cannot reply until the CPU resumes. If a native
call times out, inspect VM state instead of submitting another identical call.

Registers are the current PowerPC CPU context, not another process's saved
registers. Virtual memory follows that CPU's active mapping. OS exception
handlers may have address translation disabled; inspect MSR/SRR state when a
virtual read of a previously valid application address fails. Physical memory
reads have different address semantics. Failed reads are not proof that the
application data vanished.

For desktop input, inspect the screenshot and foreground process first.
vm_input targets the active application and may send a chat prompt if AgentChat
is foreground. A physical Mac can still use native inspection/file tools even
when VM pause and input are unavailable. Finish with an observed result and
describe any capability needed to go further.
