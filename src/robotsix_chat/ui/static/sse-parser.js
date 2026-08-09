// Pure SSE stream parser — zero DOM coupling, zero closure state.
// Accepts a fetch Response body (ReadableStream) and a controller object
// with onData(rawChunk), optional onDone(), optional onActivity(), and
// error(err) callbacks.  Returns { start } where start() begins the pump.
//
// This is extracted from chat.js:processSSEStream so it can be unit-tested
// in Node via vitest without a browser DOM.

export function processSSEStream(body, controller) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let currentData = "";

  function pump() {
    reader.read().then(function (result) {
      if (result.done) {
        // Stream ended.  If the controller provides an onDone callback
        // (e.g. the /events channel), call it so the consumer can
        // reconnect.  Otherwise treat as an unexpected close.
        if (controller.onDone) {
          controller.onDone();
        } else {
          controller.error(new Error("Server closed the connection unexpectedly"));
        }
        return;
      }

      buffer += decoder.decode(result.value, { stream: true });
      // Normalise \r\n → \n and strip stray \r for robustness.
      buffer = buffer.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
      const lines = buffer.split("\n");
      // Keep the last (possibly incomplete) segment in the buffer.
      buffer = lines.pop();

      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (line.startsWith("data: ")) {
          // Accumulate multi-line data fields (some SSE impls split
          // JSON across several data: lines, though ours doesn't).
          currentData += line.slice(6);
        } else if (line === "data:") {
          currentData += "";
        } else if (line === "") {
          // Empty line = end of event.  Process accumulated data.
          if (currentData !== "") {
            controller.onData(currentData);
            currentData = "";
          }
        }
        // Ignore lines with "event:", "id:", "retry:", or comments.
      }

      if (controller.onActivity) controller.onActivity();
      return pump();
    }).catch(function (err) {
      controller.error(err);
    });
  }

  return { start: pump };
}
