/**
 * Locating the OpenUI Lang program inside a model reply.
 *
 * The tutor prompt asks for a bare program ("Your ENTIRE response must be
 * valid openui-lang code"), and most replies are one. Models still prefix a
 * sentence of prose now and then — "I'll lay out the derivation …root = Col(…)」
 * — and that single line is enough to fail the parser outright, which is what
 * left those turns as raw source (or blank) in the transcript. These helpers
 * find the program inside a mixed reply, so the chat can render it instead of
 * falling back to another renderer.
 */

/** A statement line: `root = …`, `name = …`, `$state = …`. */
const STATEMENT = /^\s*(?:\$?[A-Za-z_][\w$]*)\s*=/;
/** `root = Component(` — a program must define root this way. */
const ROOT = /\broot\s*=\s*[A-Za-z_$][\w$]*\s*\(/;
const OPENUI_FENCE = /```(?:openui-lang|openui)\s*\n([\s\S]*?)```/i;

export type OpenUILangExtraction = {
  /** The program text, or "" when the reply contains none. */
  program: string;
  /** Prose that preceded the program (trimmed), or "". */
  preamble: string;
  /** Prose that followed the program (trimmed), or "". */
  trailing: string;
  /** Whether a program was found. */
  found: boolean;
};

const EMPTY: OpenUILangExtraction = {
  program: "",
  preamble: "",
  trailing: "",
  found: false,
};

function buildExtraction(
  source: string,
): OpenUILangExtraction {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");

  // A line belongs to the program while it is a statement, a blank line, or an
  // indented continuation. Flush-left prose is dropped rather than ending the
  // program — a model sometimes narrates between two statements.
  const programLines: string[] = [];
  let last = -1;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (STATEMENT.test(line)) {
      last = i;
      programLines.push(line);
      continue;
    }
    if (!line.trim()) {
      programLines.push("");
      continue;
    }
    if (/^\s+\S/.test(line) || /^\s*[\]\)}]/.test(line)) {
      programLines.push(line);
    }
  }

  if (last === -1) return EMPTY;

  const trailingStart = lines.slice(last + 1).findIndex((line) => line.trim());
  const trailing =
    trailingStart === -1 ? "" : lines.slice(last + 1 + trailingStart).join("\n").trim();
  const program = programLines.join("\n").trim();
  if (!program) return EMPTY;

  return {
    program,
    preamble: "",
    trailing,
    found: true,
  };
}

/**
 * Split a reply into prose and the OpenUI Lang program it carries.
 *
 * Accepts a fenced ```openui-lang block, a program that starts on its own line,
 * and a program that begins mid-sentence after a prose prefix.
 */
export function extractOpenUILang(
  content: string | null | undefined,
): OpenUILangExtraction {
  if (!content) return EMPTY;
  const source = String(content);

  const fence = OPENUI_FENCE.exec(source);
  if (fence) {
    const inner = buildExtraction(fence[1]);
    if (inner.found) {
      return {
        ...inner,
        preamble: source.slice(0, fence.index).trim(),
        trailing: source.slice(fence.index + fence[0].length).trim(),
      };
    }
  }

  const root = ROOT.exec(source);
  if (!root) return EMPTY;

  const start = root.index;
  const preamble = source.slice(0, start).trim();
  const rest = buildExtraction(source.slice(start));
  if (!rest.found) return EMPTY;

  return { ...rest, preamble };
}

/** True when a model reply carries an OpenUI Lang program, with or without prose. */
export function hasOpenUILang(content: string | null | undefined): boolean {
  if (!content) return false;
  if (content.includes("```openui-lang")) return true;
  return extractOpenUILang(content).found;
}

/**
 * The program to hand the renderer for a reply.
 *
 * A reply with no program is plain prose, and still renders through OpenUI — as
 * a `Markdown` node — so the chat has one renderer for every answer (and the
 * inline-maths handling that comes with it).
 */
export function toOpenUIProgram(content: string | null | undefined): string {
  const source = String(content ?? "");
  const extraction = extractOpenUILang(source);
  if (extraction.found) return extraction.program;
  return `root = Markdown(${JSON.stringify(source)})`;
}
