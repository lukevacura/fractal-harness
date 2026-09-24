"""Contract-chunked agent harness.

An edge is a claim `{pre} => {post}` tied to the source it reads. Edges are
verified by probes, cached, and invalidated when their sources change.
"""

# Bump when probe semantics change; part of every edge fingerprint, so a bump
# makes every verified edge stale.
CHECKER_VERSION = "2"  # 2: grep probes are line-based, support `exclude`

__version__ = "0.1.0"
