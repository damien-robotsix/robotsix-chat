Fix the chat→calendar-agent broker integration: requests now send the prompt under the `instruction`
key the calendar agent requires (it previously sent `message`, which the agent rejected with
"Request body must contain an 'instruction' key"). Also corrects the default `calendar_agent_id` to
the agent's real broker id `robotsix-calendar`.
