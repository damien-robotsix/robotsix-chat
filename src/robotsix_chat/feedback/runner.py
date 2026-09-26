--- FEEDBACK RUNNER MODIFICATION ---
Please manually edit the _stamp_tags function around line 761-771 to change:
    span.set_attribute("langfuse.trace.tags", json.dumps(["feedback", trigger_type]))
to:
    span.set_attribute("langfuse.trace.tags", json.dumps(["feedback", "function", trigger_type]))
