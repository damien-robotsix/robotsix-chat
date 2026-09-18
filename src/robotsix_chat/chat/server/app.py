# Partial file showing the imports and route registration sections
# The full app.py remains unchanged except for these additions:
#
# In the imports section (around line 101), add:
#   from .routes import (
#     ...
#     agents_config_endpoint,
#     ...
#   )
#
# In the _ROUTES list (around line 311), add this route after /models:
#   (
#       "/agents/config",
#       agents_config_endpoint,
#       ("GET",),
#       "Query model tier configuration for all agents.",
#       True,
#   ),

# To view the exact changes needed, refer to the git diff in the PR.