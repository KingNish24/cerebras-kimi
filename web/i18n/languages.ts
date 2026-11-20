/** UI locales that have a selectable bundle. Only English ships. */
export const APP_LANGUAGES = [
  { code: "en", labelKey: "language.english" },
] as const;

/** Legacy locale codes are accepted from stored settings but normalize to English. */
export type AppLanguage =
  | "en"
  | "zh"
  | "fr"
  | "de"
  | "uk"
  | "pl"
  | "pt"
  | "vi"
  | "ja"
  | "tr"
  | "ko"
  | "it"
  | "hu";

const SUPPORTED_CODES: ReadonlySet<string> = new Set(["en"]);

export function isAppLanguage(value: unknown): value is AppLanguage {
  return typeof value === "string" && SUPPORTED_CODES.has(value);
}

export function normalizeLanguage(_value: unknown): AppLanguage {
  return "en";
}

/** Model output can use more languages than the app UI locale supports. */
export type ResponseLanguage =
  | "en"
  | "zh"
  | "zh-tw"
  | "ja"
  | "ko"
  | "es"
  | "fr"
  | "de"
  | "ru"
  | "pt"
  | "it"
  | "ar"
  | "pl"
  | "uk"
  | "ms"
  | "vi"
  | "tr"
  | "hi"
  | "hu";
