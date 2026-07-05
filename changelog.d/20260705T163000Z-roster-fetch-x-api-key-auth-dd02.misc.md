Roster fetch now authenticates to central-deploy with the X-API-Key header instead of Bearer:
verify_auth never accepted Bearer tokens, which only went unnoticed while the deploy server ran with
auth disabled.
