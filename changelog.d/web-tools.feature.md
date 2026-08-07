Chat agents and subsessions can now search and fetch the web. `WebFetch` and
`WebSearch` were denied along with the filesystem and shell built-ins, so a
research subsession had no way to look anything up — and because a refused tool
call is indistinguishable from an empty result, it reported "sources fetched, all
empty" instead of saying it could not search. The filesystem and shell stay
denied; these two only read.
