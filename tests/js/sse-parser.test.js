// Unit tests for the SSE stream parser (sse-parser.js).
//
// The parser is pure (zero DOM), so these run in Node environment without
// jsdom.  We construct synthetic ReadableStreams that emit SSE-encoded
// byte chunks and verify the controller callbacks receive the expected
// frames and lifecycle events.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { processSSEStream } from "../../src/robotsix_chat/ui/static/sse-parser.js";

// Helper: create a ReadableStream that pushes byte chunks from an array
// of strings, then closes.  A short delay (0 ms) between chunks lets the
// pump's microtask chain advance.
function sseStream(chunks, { delayMs = 0 } = {}) {
  const encoder = new TextEncoder();
  let i = 0;
  return new ReadableStream({
    async pull(controller) {
      if (i >= chunks.length) {
        controller.close();
        return;
      }
      if (delayMs > 0) {
        await new Promise((r) => setTimeout(r, delayMs));
      }
      controller.enqueue(encoder.encode(chunks[i]));
      i++;
    },
  });
}

// Helper: build a fresh controller + collected-frames array.
function makeController() {
  const frames = [];
  const activities = [];
  const errors = [];
  const state = { doneCalled: false };
  return {
    frames,
    activities,
    errors,
    doneCalled: state, // so onDone can mutate it by reference
    controller: {
      onData(raw) {
        frames.push(raw);
      },
      onActivity() {
        activities.push(true);
      },
      onDone() {
        state.doneCalled = true;
      },
      error(err) {
        errors.push(err);
      },
    },
  };
}

// Helper: pump a stream to completion and return the collected state.
async function pumpStream(chunks, opts) {
  const { frames, activities, errors, doneCalled: doneCalledState, controller } =
    makeController();
  const stream = sseStream(chunks, opts);
  const parser = processSSEStream(stream, controller);
  parser.start();
  // Wait for the stream to be fully consumed.  The pump chain is microtask-
  // driven; a short setTimeout is enough when all chunks arrive immediately.
  // For byte-by-byte tests with many chunks, use a longer timeout.
  const timeout = (chunks && chunks.length > 10) ? 200 : 50;
  await new Promise((r) => setTimeout(r, timeout));
  return { frames, activities, errors, doneCalled: doneCalledState.doneCalled };
}

describe("processSSEStream", () => {
  // ------------------------------------------------------------------
  // Basic single-frame delivery
  // ------------------------------------------------------------------
  it("delivers a single data frame", async () => {
    const { frames, errors } = await pumpStream([
      "data: hello\n\n",
    ]);
    expect(frames).toEqual(["hello"]);
    expect(errors).toHaveLength(0);
  });

  it("delivers a JSON frame", async () => {
    const { frames, errors } = await pumpStream([
      'data: {"type":"token","content":"hi"}\n\n',
    ]);
    expect(frames).toEqual(['{"type":"token","content":"hi"}']);
    expect(errors).toHaveLength(0);
  });

  // ------------------------------------------------------------------
  // Multi-frame delivery
  // ------------------------------------------------------------------
  it("delivers multiple frames", async () => {
    const { frames, errors } = await pumpStream([
      "data: one\n\ndata: two\n\ndata: three\n\n",
    ]);
    expect(frames).toEqual(["one", "two", "three"]);
    expect(errors).toHaveLength(0);
  });

  // ------------------------------------------------------------------
  // Chunk-boundary splitting
  // ------------------------------------------------------------------
  it("handles a frame split across two chunks", async () => {
    const { frames, errors } = await pumpStream([
      "data: he",
      "llo\n\n",
    ]);
    expect(frames).toEqual(["hello"]);
    expect(errors).toHaveLength(0);
  });

  it("handles a frame split mid-line across chunks", async () => {
    const { frames, errors } = await pumpStream([
      "da",
      "ta: abc\n\n",
    ]);
    expect(frames).toEqual(["abc"]);
    expect(errors).toHaveLength(0);
  });

  it("handles a frame split after data prefix", async () => {
    const { frames, errors } = await pumpStream([
      "data: ",
      "payload\n\n",
    ]);
    expect(frames).toEqual(["payload"]);
    expect(errors).toHaveLength(0);
  });

  // ------------------------------------------------------------------
  // Multi-line data: accumulation (SSE spec: data: lines are joined)
  // ------------------------------------------------------------------
  it("accumulates multi-line data fields into one frame", async () => {
    const { frames, errors } = await pumpStream([
      "data: line1\ndata: line2\n\n",
    ]);
    // The parser joins consecutive data: lines without a separator.
    expect(frames).toEqual(["line1line2"]);
    expect(errors).toHaveLength(0);
  });

  it("accumulates three data: lines", async () => {
    const { frames, errors } = await pumpStream([
      "data: a\ndata: b\ndata: c\n\n",
    ]);
    expect(frames).toEqual(["abc"]);
    expect(errors).toHaveLength(0);
  });

  // ------------------------------------------------------------------
  // Empty data: line (just "data:") — contributes empty string
  // ------------------------------------------------------------------
  it("handles bare data: line", async () => {
    const { frames, errors } = await pumpStream([
      "data:\n\n",
    ]);
    // The frame is accumulated as "" but the empty-string guard in the
    // parser suppresses it (currentData !== "").
    expect(frames).toEqual([]);
    expect(errors).toHaveLength(0);
  });

  // ------------------------------------------------------------------
  // Line-ending normalisation: \r\n and \r
  // ------------------------------------------------------------------
  it("normalises CRLF (\\r\\n) line endings", async () => {
    const { frames, errors } = await pumpStream([
      "data: hello\r\n\r\n",
    ]);
    expect(frames).toEqual(["hello"]);
    expect(errors).toHaveLength(0);
  });

  it("normalises bare CR (\\r) line endings", async () => {
    const { frames, errors } = await pumpStream([
      "data: hello\r\r",
    ]);
    expect(frames).toEqual(["hello"]);
    expect(errors).toHaveLength(0);
  });

  it("handles mixed line endings", async () => {
    const { frames, errors } = await pumpStream([
      "data: hello\r\ndata: world\r\n\n",
    ]);
    expect(frames).toEqual(["helloworld"]);
    expect(errors).toHaveLength(0);
  });

  // ------------------------------------------------------------------
  // Blank-line termination (the empty line that ends an event)
  // ------------------------------------------------------------------
  it("does not deliver a frame without a terminating blank line", async () => {
    const { frames, errors } = await pumpStream([
      "data: incomplete",
    ]);
    // The stream ends without a blank line — the buffered data is never
    // flushed, so no frame is delivered.
    expect(frames).toEqual([]);
    expect(errors).toHaveLength(0);
  });

  // ------------------------------------------------------------------
  // onDone: stream end with onDone present
  // ------------------------------------------------------------------
  it("calls onDone when the stream ends normally", async () => {
    const { doneCalled, errors } = await pumpStream([
      "data: x\n\n",
    ]);
    expect(doneCalled).toBe(true);
    expect(errors).toHaveLength(0);
  });

  // ------------------------------------------------------------------
  // onDone absent: error on unexpected close
  // ------------------------------------------------------------------
  it("calls error when stream ends and no onDone callback", async () => {
    const stream = sseStream(["data: x\n\n"]);
    const errors = [];
    const controller = {
      onData() {},
      error(err) {
        errors.push(err);
      },
    };
    const parser = processSSEStream(stream, controller);
    parser.start();
    await new Promise((r) => setTimeout(r, 50));
    expect(errors).toHaveLength(1);
    expect(errors[0].message).toContain("unexpectedly");
  });

  // ------------------------------------------------------------------
  // onActivity callback
  // ------------------------------------------------------------------
  it("calls onActivity after each chunk", async () => {
    const { activities } = await pumpStream([
      "data: a\n\n",
      "data: b\n\n",
    ]);
    // Two chunks → two pump iterations → two activity calls.
    expect(activities.length).toBeGreaterThanOrEqual(2);
  });

  // ------------------------------------------------------------------
  // Ignores non-data SSE fields (event:, id:, retry:, comments)
  // ------------------------------------------------------------------
  it("ignores event:, id:, retry:, and comment lines", async () => {
    const { frames, errors } = await pumpStream([
      "event: update\nid: 42\nretry: 5000\n: this is a comment\ndata: payload\n\n",
    ]);
    expect(frames).toEqual(["payload"]);
    expect(errors).toHaveLength(0);
  });

  // ------------------------------------------------------------------
  // Chunked delivery across many small chunks (torture test)
  // ------------------------------------------------------------------
  it("handles byte-by-byte delivery", async () => {
    const text = "data: hello\n\n";
    const encoder = new TextEncoder();
    const fullBytes = encoder.encode(text);
    const frames = [];
    const errors = [];

    // Emit one byte per chunk by pushing all bytes upfront in start(),
    // then close.  The pump will read them one at a time.
    let i = 0;
    const stream = new ReadableStream({
      start(controller) {
        for (; i < fullBytes.length; i++) {
          controller.enqueue(new Uint8Array([fullBytes[i]]));
        }
        controller.close();
      },
    });

    const controller = {
      onData(raw) {
        frames.push(raw);
      },
      onDone() {
        // expected: stream ends normally after all bytes are consumed
      },
      error(err) {
        errors.push(err);
      },
    };

    const parser = processSSEStream(stream, controller);
    parser.start();
    // The pump chain is microtask-driven; a short setTimeout is enough
    // when all bytes are already buffered in the stream.
    await new Promise((r) => setTimeout(r, 50));
    expect(frames).toEqual(["hello"]);
    expect(errors).toHaveLength(0);
  });

  // ------------------------------------------------------------------
  // Multiple events in a single chunk
  // ------------------------------------------------------------------
  it("delivers two events from one chunk", async () => {
    const { frames, errors } = await pumpStream([
      "data: first\n\ndata: second\n\n",
    ]);
    expect(frames).toEqual(["first", "second"]);
    expect(errors).toHaveLength(0);
  });

  // ------------------------------------------------------------------
  // Error propagation: ReadableStream errors surface via controller.error
  // ------------------------------------------------------------------
  it("propagates stream read errors to controller.error", async () => {
    const testError = new Error("Read failed");
    const stream = new ReadableStream({
      start(controller) {
        controller.error(testError);
      },
    });
    const errors = [];
    const controller = {
      onData() {},
      error(err) {
        errors.push(err);
      },
    };
    const parser = processSSEStream(stream, controller);
    parser.start();
    await new Promise((r) => setTimeout(r, 50));
    expect(errors).toHaveLength(1);
    expect(errors[0]).toBe(testError);
  });

  // ------------------------------------------------------------------
  // AbortError propagation: the parser passes errors through unchanged
  // so the caller can discriminate AbortError vs real errors.
  // ------------------------------------------------------------------
  it("propagates AbortError so caller can discriminate", async () => {
    // Simulate a DOMException-like AbortError as thrown by a cancelled
    // fetch ReadableStream.
    const abortErr = new Error("The operation was aborted.");
    abortErr.name = "AbortError";
    const stream = new ReadableStream({
      start(controller) {
        controller.error(abortErr);
      },
    });
    const errors = [];
    const controller = {
      onData() {},
      error(err) {
        errors.push(err);
      },
    };
    const parser = processSSEStream(stream, controller);
    parser.start();
    await new Promise((r) => setTimeout(r, 50));
    expect(errors).toHaveLength(1);
    expect(errors[0].name).toBe("AbortError");
  });
});
