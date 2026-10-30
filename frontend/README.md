# OfferFlow AI frontend — public site and help (Package D1)

Next.js App Router + TypeScript, one project for public routes and (next)
authenticated app routes. Public pages are static, dependency-light, and
honest about project status.

## Local development

```sh
cd frontend
npm install
npm run dev
```

Open `http://127.0.0.1:4344` in a browser. API calls rewrite to
`OFFERFLOW_BACKEND_URL` (default `http://backend:8000` in Compose; set it to
`http://127.0.0.1:8384` when running the frontend outside Compose against a
local backend).

Production build check:

```sh
npm run build
```

## Compose service

The `frontend` service builds this directory, serves the dev server on
loopback-only port 4344, and proxies `/api/*` to the backend over the
internal network. Sources are bind-mounted for hot reload; `node_modules`
and `.next` live in named volumes. Resetting with `docker compose down -v`
also drops `frontend_modules` and `frontend_next`.

## Structure

- `app/layout.tsx`, `app/globals.css` — shell, navigation, footer, styles
- `app/page.tsx` — home
- `app/how-it-works/`, `app/demo/`, `app/self-hosting/`, `app/docs/`,
  `app/data/`, `app/source/` — public content routes
- `app/app/login/`, `app/app/` — sign-in, organization picker, upload
- `app/app/runs/[id]/` — run status, checks, offers, review answers,
  approve, export, downloads
- `lib/api.ts` — session + CSRF API client; `middleware.ts` — app auth gate
