/// <reference types="vite/client" />

/**
 * Build-time configuration.
 *
 * VITE_API_BASE is normally empty: nginx and the Vite dev server both proxy
 * /api to the API container, so the browser talks to its own origin. Set it
 * only for a deployment where the API lives on a different host.
 */
interface ImportMetaEnv {
  readonly VITE_API_BASE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
