// Pure helpers for the ```suggestions fenced block that the chat UI turns
// into clickable answer chips.
//
// These are DOM-free string transforms kept in their own module so they can
// be unit-tested (see tests/js/suggestions.test.js) — chat.js imports them
// and owns the DOM-coupled chip rendering (renderSuggestionChips,
// disableStaleSuggestionChips).

// Parses a ```suggestions fenced block from the assistant message text.
// Returns { cleanText: string, suggestions: string[] | null }: cleanText has
// the fenced block removed; suggestions is null when no block is present.
export var SUGGESTIONS_RE = /```suggestions\s*\n([\s\S]*?)```/;

// Matches a complete ```suggestions opener and everything after it, so the
// whole (still-streaming, possibly unclosed) block is stripped from the
// text shown mid-stream.
export var STREAMING_SUGGESTIONS_RE = /\n*```suggestions[\s\S]*$/;

// Matches a trailing, still-being-typed fence: three backticks optionally
// followed by a prefix of the word "suggestions", anchored to the end. This
// hides the fence in the tokens between "```" and "```suggestions" so the
// raw marker never flashes. Requires all three backticks, so inline code
// being typed ("`" / "``") is left alone.
export var PARTIAL_SUGGESTIONS_FENCE_RE =
  /\n*```(?:s(?:u(?:g(?:g(?:e(?:s(?:t(?:i(?:o(?:n(?:s)?)?)?)?)?)?)?)?)?)?)?$/;

// Remove any trailing (complete or partial) ```suggestions block from text
// that is being rendered DURING streaming, so the operator never sees the
// raw fence or its option lines. The finalised bubble re-parses the full
// text via parseSuggestions and renders the chips.
export function stripStreamingSuggestions(raw) {
  var stripped = raw.replace(STREAMING_SUGGESTIONS_RE, "");
  if (stripped === raw) {
    stripped = raw.replace(PARTIAL_SUGGESTIONS_FENCE_RE, "");
  }
  return stripped;
}

export function parseSuggestions(raw) {
  var match = SUGGESTIONS_RE.exec(raw);
  if (!match) return { cleanText: raw, suggestions: null };

  var blockContent = match[1];
  var lines = blockContent.split("\n");
  var suggestions = [];
  for (var i = 0; i < lines.length; i++) {
    var trimmed = lines[i].trim();
    if (trimmed) suggestions.push(trimmed);
  }

  var cleanText = raw.slice(0, match.index) + raw.slice(match.index + match[0].length);
  // Collapse trailing blank lines that may be left behind.
  cleanText = cleanText.replace(/\n{3,}$/, "\n\n").trimEnd();

  return {
    cleanText: cleanText,
    suggestions: suggestions.length > 0 ? suggestions : null,
  };
}
