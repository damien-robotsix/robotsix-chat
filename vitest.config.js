import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // jsdom environment for tests that need DOM APIs (e.g. chat-app
    // integration tests).  Pure unit tests (SSE parser) can override
    // to "node" per-suite.
    environment: "jsdom",
    // Look for test files under tests/js/.
    include: ["tests/js/**/*.test.js"],
    // Timeout after 10 s (SSE parser tests are async but fast).
    testTimeout: 10_000,
  },
});
