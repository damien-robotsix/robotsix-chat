Drop the GHA layer cache from the PR image-scan job: exporting the multi-GB
image's layers to the cache API took 45-55 minutes per run while a cold build
takes ~4 minutes, delaying every PR and the release verify gate.
