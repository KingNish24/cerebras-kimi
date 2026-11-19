/** True when a model reply is an OpenUI Lang program rather than Markdown. */
export function hasOpenUILang(content: string | null | undefined): boolean {
  if (!content) return false;
  if (content.includes("```openui-lang")) return true;
  return /(^|\n)\s*root\s*=/.test(content);
}
