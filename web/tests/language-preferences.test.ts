import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

import { resolveResponseLanguage } from "../context/app-shell-storage";
import { APP_LANGUAGES, isAppLanguage, normalizeLanguage } from "../i18n/languages";
import { formatDate, formatTime, getLocale } from "../lib/datetime";

test("only English is a supported interface locale", () => {
  assert.deepEqual(APP_LANGUAGES.map(({ code }) => code), ["en"]);
  assert.equal(isAppLanguage("en"), true);
  for (const raw of ["zh", "fr-FR", "de-AT", "uk-UA", "pl-PL", "pt-BR", "vi-VN", "ja-JP", "tr-TR", "ko-KR", "it-IT", "hu-HU", "xx"]) {
    assert.equal(isAppLanguage(raw), false);
    assert.equal(normalizeLanguage(raw), "en");
  }
});

test("response language remains independent from the interface language", () => {
  assert.equal(resolveResponseLanguage("zh", "en"), "zh");
  assert.equal(resolveResponseLanguage("en", "zh"), "en");
  assert.equal(resolveResponseLanguage("fr", "en"), "fr");
  assert.equal(resolveResponseLanguage("en", "fr"), "en");
  assert.equal(resolveResponseLanguage("uk", "en"), "uk");
  assert.equal(resolveResponseLanguage("en", "uk"), "en");
  assert.equal(resolveResponseLanguage("vi", "en"), "vi");
  assert.equal(resolveResponseLanguage("en", "vi"), "en");
  assert.equal(resolveResponseLanguage("vi-VN", "en"), "vi");
  assert.equal(resolveResponseLanguage("Vietnamese", "en"), "vi");
  assert.equal(resolveResponseLanguage("Tiếng Việt", "en"), "vi");
  assert.equal(resolveResponseLanguage("ja", "vi"), "ja");
});

test("response language accepts and normalizes the extended registry", () => {
  assert.equal(resolveResponseLanguage("ja", "en"), "ja");
  assert.equal(resolveResponseLanguage("zh-tw", "en"), "zh-tw");
  assert.equal(resolveResponseLanguage("Japanese", "en"), "ja");
  assert.equal(resolveResponseLanguage("zh-cn", "en"), "zh");
  assert.equal(resolveResponseLanguage("pt-BR", "en"), "pt");
  assert.equal(resolveResponseLanguage("pt_PT", "en"), "pt");
  assert.equal(resolveResponseLanguage("vi_VN", "en"), "vi");
  assert.equal(resolveResponseLanguage("Português", "en"), "pt");
  assert.equal(resolveResponseLanguage("ms-MY", "en"), "ms");
  assert.equal(resolveResponseLanguage("Malay", "en"), "ms");
  for (const raw of ["hi", "hi-IN", "hi_IN", "Hindi", "हिन्दी", "हिंदी"]) {
    assert.equal(resolveResponseLanguage(raw, "en"), "hi");
  }
  assert.equal(resolveResponseLanguage("klingon", "zh"), "en");
});

test("missing response language falls back to the English interface", () => {
  assert.equal(resolveResponseLanguage(null, "en"), "en");
  assert.equal(resolveResponseLanguage(undefined, "en"), "en");
  assert.equal(resolveResponseLanguage(null, "zh"), "en");
});


test("dates and times use the English locale", () => {
  const date = new Date("2026-03-05T14:05:00Z");
  assert.equal(getLocale("en"), "en-US");
  assert.equal(formatDate(date, "en", {
    year: "numeric", month: "long", day: "numeric", timeZone: "UTC",
  }), "March 5, 2026");
  assert.equal(formatTime(date, "en", {
    hour: "2-digit", minute: "2-digit", timeZone: "UTC",
  }), "02:05 PM");
});
