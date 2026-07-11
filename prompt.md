You are a senior front-end engineer. Build a complete, runnable single-page website based on the specifications below. Provide the output EXACTLY file-for-file.



\# Rules

\- Write complete, production-ready code for every file. Do not use placeholders like "insert code here".

\- Stack: React 18 + TypeScript + Vite + Tailwind CSS v3 + framer-motion + lenis + lucide-react. No other libraries, no routing, no backend.

\- Design Specifications: \[INSERT GENERAL DESIGN THEME, e.g., "A dark-themed portfolio for a 3D artist", "A minimalist landing page for a coffee brand"].

\- Signature features: \[INSERT ANY SPECIFIC INTERACTIVITY, e.g., "A hero section with a horizontal scroll effect", "Custom cursor tracking"].



\# Step 0 — Assets \& Media

\- \[DESCRIBE REQUIRED ASSETS HERE, e.g., "Assume there is a logo.svg in the public folder", "Use placeholder images from Unsplash matching the theme of nature"].



\# Step 1 — Scaffold + install

Create the file tree below, then:

```bash

npm install

Dependencies are pinned in package.json (framer-motion ^11, lenis ^1, lucide-react ^0.469, react/react-dom ^18, vite ^6, tailwindcss ^3, typescript ^5).



Step 2 — Configuration Files (create each verbatim)

package.json

JSON





{

&#x20; "name": "vite-react-project",

&#x20; "private": true,

&#x20; "version": "0.0.0",

&#x20; "type": "module",

&#x20; "scripts": {

&#x20;   "dev": "vite",

&#x20;   "build": "tsc \&\& vite build",

&#x20;   "preview": "vite preview"

&#x20; },

&#x20; "dependencies": {

&#x20;   "framer-motion": "^11.18.2",

&#x20;   "lenis": "^1.3.25",

&#x20;   "lucide-react": "^0.469.0",

&#x20;   "react": "^18.3.1",

&#x20;   "react-dom": "^18.3.1"

&#x20; },

&#x20; "devDependencies": {

&#x20;   "@types/react": "^18.3.18",

&#x20;   "@types/react-dom": "^18.3.5",

&#x20;   "@vitejs/plugin-react": "^4.3.4",

&#x20;   "autoprefixer": "^10.4.20",

&#x20;   "postcss": "^8.4.49",

&#x20;   "tailwindcss": "^3.4.17",

&#x20;   "typescript": "^5.6.3",

&#x20;   "vite": "^6.0.7"

&#x20; }

}

vite.config.ts

TypeScript





import { defineConfig } from 'vite';

import react from '@vitejs/plugin-react';



export default defineConfig({

&#x20; plugins: \[react()],

});

tsconfig.json

JSON





{

&#x20; "compilerOptions": {

&#x20;   "target": "ES2020",

&#x20;   "useDefineForClassFields": true,

&#x20;   "lib": \["ES2020", "DOM", "DOM.Iterable"],

&#x20;   "module": "ESNext",

&#x20;   "skipLibCheck": true,

&#x20;   "moduleResolution": "bundler",

&#x20;   "allowImportingTsExtensions": true,

&#x20;   "isolatedModules": true,

&#x20;   "moduleDetection": "force",

&#x20;   "noEmit": true,

&#x20;   "jsx": "react-jsx",

&#x20;   "strict": true,

&#x20;   "noUnusedLocals": true,

&#x20;   "noUnusedParameters": true,

&#x20;   "noFallthroughCasesInSwitch": true

&#x20; },

&#x20; "include": \["src", "vite.config.ts"]

}

postcss.config.js

JavaScript





export default {

&#x20; plugins: {

&#x20;   tailwindcss: {},

&#x20;   autoprefixer: {},

&#x20; },

};

tailwind.config.js

JavaScript





/\*\* @type {import('tailwindcss').Config} \*/

export default {

&#x20; content: \['./index.html', './src/\*\*/\*.{ts,tsx}'],

&#x20; theme: {

&#x20;   extend: {

&#x20;     // \[GENERATE ANY SPECIFIC COLORS OR FONTS REQUIRED BY THE DESIGN HERE]

&#x20;   },

&#x20; },

&#x20; plugins: \[],

};

index.html

HTML





<!doctype html>

<html lang="en">

&#x20; <head>

&#x20;   <meta charset="UTF-8" />

&#x20;   <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />

&#x20;   <title>\[INSERT PROJECT TITLE]</title>

&#x20; </head>

&#x20; <body>

&#x20;   <div id="root"></div>

&#x20;   <script type="module" src="/src/main.tsx"></script>

&#x20; </body>

</html>

src/main.tsx

TypeScript





import { StrictMode } from 'react';

import { createRoot } from 'react-dom/client';

import App from './App';

import './index.css';



createRoot(document.getElementById('root')!).render(

&#x20; <StrictMode>

&#x20;   <App />

&#x20; </StrictMode>,

);

Step 3 — Core Logic \& UI Components

src/lib/useLenis.ts

TypeScript





import { useEffect } from 'react';

import Lenis from 'lenis';



export function useLenis() {

&#x20; useEffect(() => {

&#x20;   if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

&#x20;   const lenis = new Lenis({ lerp: 0.1 });

&#x20;   let rafId = 0;

&#x20;   const raf = (time: number) => {

&#x20;     lenis.raf(time);

&#x20;     rafId = requestAnimationFrame(raf);

&#x20;   };

&#x20;   rafId = requestAnimationFrame(raf);

&#x20;   return () => {

&#x20;     cancelAnimationFrame(rafId);

&#x20;     lenis.destroy();

&#x20;   };

&#x20; }, \[]);

}

src/components/Reveal.tsx

TypeScript





import { motion, useReducedMotion } from 'framer-motion';

import type { CSSProperties, ReactNode } from 'react';



type Tag = 'div' | 'span' | 'li' | 'h2' | 'p' | 'section';



interface RevealProps {

&#x20; children: ReactNode;

&#x20; delay?: number;

&#x20; className?: string;

&#x20; as?: Tag;

&#x20; inline?: boolean;

}



export default function Reveal({

&#x20; children,

&#x20; delay = 0,

&#x20; className,

&#x20; as = 'div',

&#x20; inline = false,

}: RevealProps) {

&#x20; const reduce = useReducedMotion();



&#x20; if (reduce) {

&#x20;   const Plain = as;

&#x20;   return <Plain className={className}>{children}</Plain>;

&#x20; }



&#x20; const Comp = motion\[as] as React.ComponentType<{

&#x20;   className?: string;

&#x20;   style?: CSSProperties;

&#x20;   initial?: object;

&#x20;   whileInView?: object;

&#x20;   viewport?: object;

&#x20;   transition?: object;

&#x20;   children?: ReactNode;

&#x20; }>;



&#x20; return (

&#x20;   <Comp

&#x20;     className={className}

&#x20;     style={inline ? { display: 'inline-block', willChange: 'transform, opacity' } : undefined}

&#x20;     initial={{ opacity: 0, y: 30 }}

&#x20;     whileInView={{ opacity: 1, y: 0 }}

&#x20;     viewport={{ once: true, amount: 0.3 }}

&#x20;     transition={{ duration: 0.8, ease: \[0.16, 1, 0.3, 1], delay }}

&#x20;   >

&#x20;     {children}

&#x20;   </Comp>

&#x20; );

}

Step 4 — Generate Project Specific Files

Based on the design requirements provided in the rules, generate the following files with complete styling, responsive layouts, and content:



src/index.css

\[Generate the global CSS, import necessary Google Fonts (e.g., Inter, Playfair Display, or others matching the theme), and add custom Tailwind utility classes or animations required for the design.]



src/App.tsx

\[Generate the main App component. It must implement useLenis() for smooth scrolling and compose all the structural sections of the page in order.]



src/sections/\[Name].tsx

\[Generate as many section components as needed (e.g., Hero, About, Features, Footer). Break them down logically. Use the <Reveal> component for scroll animations where appropriate. Use lucide-react for icons.]



Step 5 — Run + verify

Bash





npm run dev      # http://localhost:5173

npm run build    # must pass tsc + vite build with no errors

Acceptance checklist (must all be true)

\[INSERT SPECIFIC ACCEPTANCE CRITERIA 1, e.g., "The navigation bar must be sticky and change color on scroll."]



\[INSERT SPECIFIC ACCEPTANCE CRITERIA 2]



Entrance animations and scroll interactions perform smoothly and respect prefers-reduced-motion.



npm run build passes clean. No console errors.



Fully responsive design across mobile (<768px), tablet, and desktop views.

