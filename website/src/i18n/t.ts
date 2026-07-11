/**
 * Build-time translation helper.
 *
 * Used in Astro component frontmatter to render translated text at build time.
 * Client-side data-t swapping still works as a fallback for non-locale pages.
 */
import { translations } from './translations';

export type TranslateFunction = (key: string, fallback: string) => string;

export function createT(locale: string): TranslateFunction {
  const dict = locale !== 'en' ? translations[locale] : undefined;
  return (key: string, fallback: string) => dict?.[key] ?? fallback;
}
