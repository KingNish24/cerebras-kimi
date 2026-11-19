import { describe, expect, it } from "vitest";

import {
  hasGenerativeUi,
  parseGenerativeUi,
} from "@/lib/generative-ui/parse-ogui";

const QUIZ = [
  "Here is a quick quiz for you.",
  "",
  "```ogui-meta",
  '{"initialHeight": 320, "placeholderMessages": ["Building quiz..."]}',
  "```",
  "```ogui-css",
  ".q-opt { padding: 8px; }",
  "```",
  "```ogui-html",
  '<div id="quiz"><button class="q-opt">A</button></div>',
  "```",
  "```ogui-js",
  "function check() { return 42; }",
  "```",
  "```ogui-expr",
  'document.getElementById("quiz").addEventListener("click", check);',
  "```",
  "```ogui-expr",
  "check();",
  "```",
  "",
  "Answer one at a time and tell me your score!",
].join("\n");

describe("hasGenerativeUi", () => {
  it("detects ogui fences only", () => {
    expect(hasGenerativeUi(QUIZ)).toBe(true);
    expect(hasGenerativeUi("plain **markdown** answer")).toBe(false);
    expect(hasGenerativeUi(null)).toBe(false);
    expect(hasGenerativeUi("")).toBe(false);
  });
});

describe("parseGenerativeUi", () => {
  it("splits prose and widget segments in order", () => {
    const segs = parseGenerativeUi(QUIZ);
    expect(segs.map((s) => s.kind)).toEqual(["prose", "widget", "prose"]);
    const w = segs[1];
    if (w.kind !== "widget") throw new Error("expected widget");
    expect(w.widget.initialHeight).toBe(320);
    expect(w.widget.placeholderMessages).toEqual(["Building quiz..."]);
    expect(w.widget.cssComplete).toBe(true);
    expect(w.widget.htmlComplete).toBe(true);
    expect(w.widget.html).toContain('id="quiz"');
    expect(w.widget.jsFunctions).toContain("function check()");
    expect(w.widget.jsExpressions).toHaveLength(2);
    expect(w.widget.generating).toBe(false);
  });

  it("keeps a streaming widget generating until fences close", () => {
    const partial = [
      "Intro text",
      "```ogui-css",
      ".a { color: red; }",
      "```",
      "```ogui-html",
      "<div>partial",
    ].join("\n");
    const segs = parseGenerativeUi(partial);
    const w = segs.find((s) => s.kind === "widget");
    if (!w || w.kind !== "widget") throw new Error("expected widget");
    expect(w.widget.cssComplete).toBe(true);
    expect(w.widget.htmlComplete).toBe(false);
    expect(w.widget.html).toBe("<div>partial");
    expect(w.widget.generating).toBe(true);
  });

  it("drops unclosed expr fences so partial JS never runs", () => {
    const content = [
      "```ogui-html",
      "<div/>",
      "```",
      "```ogui-expr",
      'console.log("done");',
      "```",
      "```ogui-expr",
      'console.log("par',
    ].join("\n");
    const segs = parseGenerativeUi(content);
    const w = segs.find((s) => s.kind === "widget");
    if (!w || w.kind !== "widget") throw new Error("expected widget");
    expect(w.widget.jsExpressions).toEqual(['console.log("done");']);
  });

  it("starts a new widget at each ogui-meta fence", () => {
    const content = [
      "```ogui-html",
      "<div>one</div>",
      "```",
      "Middle prose.",
      "```ogui-meta",
      "{}",
      "```",
      "```ogui-html",
      "<div>two</div>",
      "```",
    ].join("\n");
    const widgets = parseGenerativeUi(content).filter(
      (s) => s.kind === "widget",
    );
    expect(widgets).toHaveLength(2);
    if (widgets[0].kind !== "widget" || widgets[1].kind !== "widget") return;
    expect(widgets[0].widget.html).toContain("one");
    expect(widgets[1].widget.html).toContain("two");
  });

  it("returns a single prose segment when no ogui fence exists", () => {
    const segs = parseGenerativeUi("Just an answer.");
    expect(segs).toEqual([{ kind: "prose", text: "Just an answer." }]);
  });
});
