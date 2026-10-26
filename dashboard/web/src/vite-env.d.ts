/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Absolute base of the Postern API. Empty means the same origin, which is how
   *  the bundle served by FastAPI itself works; Vercel builds set it. */
  readonly VITE_POSTERN_API?: string
}
interface ImportMeta {
  readonly env: ImportMetaEnv
}
