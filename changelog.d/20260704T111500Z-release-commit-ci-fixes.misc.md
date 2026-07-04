Fix red main after the first automated release: re-lock `uv.lock` for the
bumped 0.2.0 version, and exclude the towncrier-generated `CHANGELOG.md`
from the mdformat and typos hooks (it is written only by the release
workflow, so hooks must not demand hand edits to it).
