Fix broker-skill tool generation hardcoding every parameter annotation to `str`; tool JSON schemas now reflect each parameter's real type (int/bool/list/str), so pydantic-ai builds correct schemas.
