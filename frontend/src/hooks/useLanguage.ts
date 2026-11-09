import { create } from "zustand";
import { translations, type Lang, type Translations } from "../i18n/translations";

const STORAGE_KEY = "transit-lang";

function loadLang(): Lang {
  try {
    const s = localStorage.getItem(STORAGE_KEY);
    if (s === "bg" || s === "en") return s;
  } catch {}
  return "bg";
}

type LangState = {
  lang: Lang;
  t: Translations;
  setLang: (lang: Lang) => void;
};

export const useLangStore = create<LangState>((set) => ({
  lang: loadLang(),
  t: translations[loadLang()] as Translations,
  setLang: (lang) => {
    try { localStorage.setItem(STORAGE_KEY, lang); } catch {}
    set({ lang, t: translations[lang] as Translations });
  },
}));
