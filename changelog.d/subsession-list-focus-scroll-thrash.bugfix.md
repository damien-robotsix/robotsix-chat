Fix the subsessions panel becoming unusable while a subsession is actively running: every
`subsession_updated` SSE frame (fired frequently for in-flight work) fully wiped and rebuilt the
entire panel, which reset the panel's scroll position and destroyed-and-recreated the reply textarea
for any expanded subsession — stealing input focus mid-keystroke and making it impossible to type a
continuous reply. The list now reconciles in place: each row's non-interactive header
(status/meta/actions) is still rebuilt cheaply on every update, but the transcript and reply
textarea are built once and never touched again by a refresh.
