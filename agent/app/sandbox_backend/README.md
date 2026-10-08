# Sandbox backend

- `filesystem.py` is the implemented path-confined, suffix-allowlisted atomic
  file sandbox.
- `protocol.py` defines the typed request, event, network policy, resource quota,
  and response envelope for a future disposable container/VM executor.

The protocol types do not imply that untrusted-code execution is enabled.
