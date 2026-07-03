Fix cognee Langfuse tracing: register an explicitly-configured OTLP logger instance so cognee
traffic reaches the dedicated project instead of defaulting to Langfuse US cloud with the main
project's credentials.
