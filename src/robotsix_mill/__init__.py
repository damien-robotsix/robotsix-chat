"""Shadow package: override ``stages/towncrier.py`` with a patched version.

Python finds this ``__init__.py`` first (``src/`` is at position 1 in
``sys.path`` via ``PYTHONPATH``).  We delegate to the installed
``robotsix_mill`` package while injecting our local overrides into
the ``stages`` sub-package's ``__path__`` so that ``stages/towncrier``
is loaded from here.

All other submodules (including ``_resources``) resolve from the
installed package, so ``importlib.resources.files`` and ``__file__``-
relative lookups continue to work correctly.
"""

from __future__ import annotations

import sys
from pathlib import Path

_LOCAL_DIR = Path(__file__).parent
_LOCAL_STAGES = str(_LOCAL_DIR / "stages")

# ---------------------------------------------------------------------------
# 1.  Temporarily remove ``src/`` from sys.path so ``import robotsix_mill``
#     finds the installed package, not ourselves.
# ---------------------------------------------------------------------------
_src_parent = str(_LOCAL_DIR.parent)  # the ``src/`` directory
_src_entries = [p for p in sys.path if p == _src_parent]
for p in _src_entries:
    sys.path.remove(p)

# ---------------------------------------------------------------------------
# 2.  Discard our half-built module object and import the real package.
# ---------------------------------------------------------------------------
del sys.modules["robotsix_mill"]
import robotsix_mill  # noqa: E402 — must happen after sys.path manipulation

# ---------------------------------------------------------------------------
# 3.  Restore sys.path so the rest of the process can find src/ modules.
# ---------------------------------------------------------------------------
for p in reversed(_src_entries):
    sys.path.insert(0, p)

# ---------------------------------------------------------------------------
# 4.  Eagerly import ``stages`` and inject our local override directory
#     at the front of its ``__path__`` so ``towncrier.py`` is loaded from
#     our local copy.  Must happen BEFORE any code imports
#     ``robotsix_mill.stages.towncrier``.
# ---------------------------------------------------------------------------
import robotsix_mill.stages  # noqa: E402

if _LOCAL_STAGES not in robotsix_mill.stages.__path__:
    robotsix_mill.stages.__path__.insert(0, _LOCAL_STAGES)

# ---------------------------------------------------------------------------
# 5.  Patch ``load_agent_definition`` to prefer local overrides in
#     ``agent_definitions/``.  When a YAML file exists under our local
#     ``src/robotsix_mill/agent_definitions/`` directory, use it instead
#     of the installed copy.  This allows the repo to extend or override
#     agent guidance (e.g. add a CI workflow edit checklist) without
#     forking the entire mill package.
# ---------------------------------------------------------------------------
import robotsix_mill.agents.yaml_loader  # type: ignore[import-untyped]  # noqa: E402

_original_load_agent_definition = robotsix_mill.agents.yaml_loader.load_agent_definition
_LOCAL_AGENT_DEFINITIONS = str(_LOCAL_DIR / "agent_definitions")


def _load_with_local_overrides(path: Path) -> object:
    local_path = Path(_LOCAL_AGENT_DEFINITIONS) / path.name
    if local_path.is_file():
        return _original_load_agent_definition(local_path)
    return _original_load_agent_definition(path)


robotsix_mill.agents.yaml_loader.load_agent_definition = _load_with_local_overrides
