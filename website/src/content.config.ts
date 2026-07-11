import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';

const docs = defineCollection({
  // [a-z]* excludes CLAUDE.md (uppercase) from the content collection
  loader: glob({ pattern: '**/[a-z]*.md', base: './src/content/docs' }),
  schema: z.object({
    title: z.string(),
    description: z.string().optional(),
    order: z.number().optional(),
    section: z.string().optional(),
  }),
});

const blog = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/blog' }),
  schema: z.object({
    title: z.string(),
    description: z.string(),
    date: z.coerce.date(),
    author: z.string(),
    category: z.string(),
    tags: z.array(z.string()),
    readingTime: z.number(),
  }),
});

export const collections = { docs, blog };
