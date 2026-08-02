import { defineConfig, loadEnv } from 'vite'
import tailwindcss from '@tailwindcss/vite'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.dirname(fileURLToPath(import.meta.url))

/**
 * Mirrors Laravel's @include() so these pages port to resources/views/ as-is.
 *
 *   <!-- @include('partials.sidebar', {"nav":"dashboard"}) -->
 *
 * Dropping into Blade means deleting the `<!--` / `-->` and renaming the file.
 * Partials read their data with Blade's own `{{ $var }}` syntax.
 */
function bladeIncludes() {
  const expand = (html, depth = 0) => {
    if (depth > 6) return html
    return html.replace(
      /<!--\s*@include\(\s*'([^']+)'\s*(?:,\s*(\{[\s\S]*?\}))?\s*\)\s*-->/g,
      (match, view, json) => {
        const file = path.join(root, view.replace(/\./g, path.sep) + '.html')
        if (!fs.existsSync(file)) return `<!-- missing view: ${view} -->`
        let partial = fs.readFileSync(file, 'utf8')
        const data = json ? JSON.parse(json) : {}
        partial = partial.replace(/\{\{\s*\$([A-Za-z_]\w*)\s*\}\}/g, (_, key) =>
          key in data ? String(data[key]) : ''
        )
        return expand(partial, depth + 1)
      }
    )
  }
  return {
    name: 'blade-includes',
    transformIndexHtml: { order: 'pre', handler: (html) => expand(html) },
    handleHotUpdate({ file, server }) {
      if (file.includes(`${path.sep}partials${path.sep}`)) {
        server.ws.send({ type: 'full-reload' })
        return []
      }
    },
  }
}

/**
 * Substitutes %VITE_API_BASE% in the HTML. Vite only does this automatically
 * for `import.meta.env` in JS, and the value has to reach a <meta> tag in the
 * document head so it is readable before any module runs.
 */
function apiBase(mode) {
  const value = loadEnv(mode, root, 'VITE_').VITE_API_BASE ?? 'http://127.0.0.1:8000'
  return {
    name: 'api-base',
    transformIndexHtml: {
      order: 'post',
      handler: (html) => html.replaceAll('%VITE_API_BASE%', value.replace(/\/$/, '')),
    },
  }
}

export default defineConfig(({ mode }) => ({
  plugins: [bladeIncludes(), apiBase(mode), tailwindcss()],
  build: {
    rollupOptions: {
      input: {
        dashboard: path.resolve(root, 'index.html'),
        email: path.resolve(root, 'email.html'),
        telegram: path.resolve(root, 'telegram.html'),
        settings: path.resolve(root, 'settings.html'),
        login: path.resolve(root, 'login.html'),
      },
    },
  },
}))
