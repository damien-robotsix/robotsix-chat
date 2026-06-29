Fix a `PydanticSchemaGenerationError` ("Unable to generate pydantic-core schema for
`CheckLoopRegistry`") that crashed the chat agent: the extracted check-loop tools were bound with
`functools.partial`, whose signature still exposed the injected runtime state (`registry`,
`settings`, `channel`, …), so the provider's tool-schema builder tried to JSON-schema the
non-pydantic `CheckLoopRegistry`. The tools are now thin closures that capture state lexically,
exposing only the model-facing parameters.
