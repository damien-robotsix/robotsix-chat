// Helpers for the ```suggestions fenced block that the chat UI turns into
// clickable answer chips.
//
// String transforms (parseSuggestions, stripStreamingSuggestions) are pure
// and DOM-free.  The DOM-coupled chip helpers (renderSuggestionChips,
// disableStaleSuggestionChips) also live here so they can be unit-tested
// under jsdom (see tests/js/suggestions.test.js).  chat.js imports
// everything from this single module.

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

// ---- DOM-coupled chip helpers -----------------------------------------

// Render clickable suggestion chips below *afterElement*.  Each chip calls
// *onSubmit(chipText)* when clicked.  When *disabled* is true the chips are
// rendered inert (visible for context, not clickable).
export function renderSuggestionChips(suggestions, onSubmit, afterElement, disabled) {
  var container = document.createElement("div");
  container.className = "suggestion-chips";
  if (disabled) container.classList.add("suggestion-chips--stale");
  for (var i = 0; i < suggestions.length; i++) {
    var chip = document.createElement("button");
    chip.type = "button";
    chip.className = "suggestion-chip";
    chip.textContent = suggestions[i];
    if (disabled) {
      chip.disabled = true;
      chip.classList.add("suggestion-chip--stale");
      chip.title = suggestions[i];
    } else {
      chip.title = "Click to reply: " + suggestions[i];
      chip.addEventListener("click", (function (text) {
        return function () { onSubmit(text); };
      })(suggestions[i]));
    }
    container.appendChild(chip);
  }
  if (afterElement && afterElement.parentNode) {
    afterElement.parentNode.insertBefore(container, afterElement.nextSibling);
  }
  return container;
}

// Disable every non-disabled suggestion chip under *root* (typically the
// chat container).  Called when a newer message supersedes the decision the
// chips answered.
export function disableStaleSuggestionChips(root) {
  var chips = root.querySelectorAll(".suggestion-chip:not([disabled])");
  for (var i = 0; i < chips.length; i++) {
    chips[i].disabled = true;
    chips[i].classList.add("suggestion-chip--stale");
    if (chips[i].parentNode) {
      chips[i].parentNode.classList.add("suggestion-chips--stale");
    }
  }
}
