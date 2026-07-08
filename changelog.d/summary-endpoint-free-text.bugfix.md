`POST /summary` no longer forces a fixed 5-field JSON schema (purpose, pending_work,
pending_questions, blockers, relevant_info). The cheap summary-tier model spent most of its turn
trying to satisfy that schema and often ran past its token budget before producing valid JSON,
making the summary panel slow or stuck on "Updating…". It now returns `{"summary": "<plain text>"}`
— a few unconstrained sentences, no schema to fail.
