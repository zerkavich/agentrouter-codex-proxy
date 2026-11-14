import { english } from './messages.js';

const storageKey = 'proxy-panel-language';
let language = 'ru';
try {
  const saved = localStorage.getItem(storageKey);
  language = saved === 'ru' || saved === 'en' ? saved : /^(ru|be)/i.test(navigator.language || '') ? 'ru' : 'en';
} catch {}

export function getLanguage() { return language; }
export function setLanguage(value) {
  if (value !== 'ru' && value !== 'en') return;
  language = value;
  try { localStorage.setItem(storageKey, value); } catch {}
}
export function locale() { return language === 'ru' ? 'ru-RU' : 'en-US'; }
export function t(message, values = {}) {
  const translated = language === 'en' ? (english[message] ?? message) : message;
  return translated.replace(/\{(v\d+)\}/g, (match, key) => key in values ? String(values[key]) : match);
}
