import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';

export default defineConfig({
  site: 'https://getmeridian.org',
  output: 'static',
  integrations: [
    sitemap({
      filter: (page) => !page.includes('/CLAUDE'),
      i18n: {
        defaultLocale: 'en',
        locales: {
          en: 'en-US',
          ru: 'ru-RU',
          fa: 'fa-IR',
          zh: 'zh-Hans',
        },
      },
    }),
  ],
  i18n: {
    locales: ['en', 'ru', 'fa', 'zh'],
    defaultLocale: 'en',
  },
  build: {
    format: 'directory',
  },
  prefetch: {
    prefetchAll: true,
    defaultStrategy: 'viewport',
  },
  markdown: {
    shikiConfig: {
      themes: {
        light: 'github-light',
        dark: 'github-dark',
      },
      defaultColor: false,
    },
  },
});
