Deploy contract: remove duplicate config volume mount (chat-config and config both bound
/home/app/config), which made every container create fail with 'Duplicate mount point'.
