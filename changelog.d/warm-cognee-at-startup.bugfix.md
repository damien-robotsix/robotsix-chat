Pay cognee's cold-start cost in a background task at server startup instead of
billing it to the first turns after a restart. `recall` calls `setup()` inside
the caller's timeout, so the opening turns of every restart were charged for
cognee's import, configuration, and the lazy vector-store opens that the first
search triggers — live, that ran past the recall deadline and those turns
proceeded memory-less. The warm-up also issues one throwaway search, because
configuration alone was not enough: the first search was observed exceeding the
deadline on its own even after setup had completed.
