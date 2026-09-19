/**
 * Customer wording for the things the server reports by code.
 *
 * Warning, check and finding texts are written for whoever maintains the
 * rules, and some name the method behind them. The screen shows what the
 * reader should do instead; the code itself stays in the API response for
 * support, never on screen. Anything not listed gets a generic line rather
 * than the raw text.
 */

/** Template warnings: what to look at in the Word file. */
const WARNING_TEXT: Record<string, string> = {
  "W-HL-GAP": "Some ordinary text sits inside an author instruction and will be removed with it. Check it is not content you want to keep.",
  "W-MARKER-PARSE": "An optional-section instruction could not be understood, so that section will not be handled. Check its wording.",
  "W-MIXED-SYNTAX": "A placeholder uses brackets this template does not use elsewhere, so it will not be filled. Retype it with the usual brackets.",
  "W-FIELD-CODE": "A placeholder sits inside a Word field. Check the field is not doing something else.",
  "W-DUP-STATIC": "Two passages have almost the same text. Check that is intended.",
  "W-NESTED-COND": "One optional section sits inside another. Check both start and end where you expect.",
  "W-UNSLOTTED-FIELD": "A field has no place in the document to appear.",
  "W-SPLIT-PLACEHOLDER": "A placeholder was typed in pieces, so it cannot be filled. Retype it in one go.",
};

export function warningText(code: string | null | undefined): string {
  return (code && WARNING_TEXT[code]) || "Part of this template needs a second look before you generate.";
}

/** Template-studio checks, by the code the server files them under. */
const LINT_TEXT: Record<string, string> = {
  object_not_lockable: "This item cannot be saved as it is. Check where it starts and ends.",
  overlapping_anchor_ranges: "Two items cover the same text. Adjust one so they do not overlap.",
  overlapping_regions: "Two sections cover the same paragraph. Adjust one so they do not overlap.",
  duplicate_slug: "Two items share a name. Rename one of them.",
  block_boundary_guessed: "Where this section ends was inferred. Check the end point.",
  condition_needs_a_source_column: "An optional section depends on a value nothing in the document fills, so your data needs a column for it.",
  field_not_in_dictionary: "This field is new to your organisation, so you will match it to a column by hand.",
  compiler_warning: "Part of this template needs a second look.",
  ...Object.fromEntries(Object.entries(WARNING_TEXT)),
};

export function lintText(code: string | null | undefined, severity?: string | null): string {
  if (code && LINT_TEXT[code]) return LINT_TEXT[code];
  return severity === "blocking"
    ? "This needs fixing before the template can be published."
    : "This is worth a look before you publish.";
}

export function severityLabel(severity: string | null | undefined): string {
  if (severity === "blocking") return "Must fix";
  if (severity === "warning") return "Check";
  return "Note";
}

/** Document checks. The server sends prose, so the match is on its subject. */
const QA_TEXT: [RegExp, string][] = [
  [/placeholder|bracket|mergefield|merge field|control token|mask/i, "Some placeholders were left unfilled."],
  [/instruction/i, "Some author instructions were left in the document."],
  [/required|missing|absent|no value/i, "A required value is missing."],
  [/overflow|exceed|too long|max_len|does not fit/i, "A value is too long for the space it goes in."],
  [/branch|condition|section/i, "An optional section could not be decided."],
  [/static|layout|structure|changed|content loss|lost/i, "The fixed text or layout differs from the template."],
  [/doubled|format|date/i, "A value's format needs checking."],
  [/row|repeat/i, "A table row needs checking."],
];

export function qaNoteText(note: unknown): string {
  const raw = String(note ?? "");
  const minor = /^warning \(non-blocking\):/i.test(raw);
  const hit = QA_TEXT.find(([re]) => re.test(raw));
  const text = hit ? hit[1] : "This document needs a check before it is used.";
  return minor ? `Minor: ${text}` : text;
}

/** Distinct customer lines for a list of notes, in order. */
export function qaNoteLines(notes: unknown[]): string[] {
  return [...new Set((notes ?? []).map(qaNoteText))];
}
