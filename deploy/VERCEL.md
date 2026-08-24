# Deploy via Vercel

Vercel config for this repo, mirroring [`render.yaml`](render.yaml) /
[`README.md`](README.md) for Render.

`vercel.json` currently points at the same landing page in
[`pages/`](../pages) — no build step, since it's plain HTML. Per
[`DEPLOYMENT.md`](DEPLOYMENT.md), Vercel is the intended home for the real
React **frontend** once it exists; this is a placeholder pointed at the
landing page in the meantime so the connect steps below already work today.

## Connect it on Vercel

1. Push this branch to GitHub (`origin/main` is already connected to
   `DevSidd2006/OpenHire`).
2. In the Vercel dashboard: **Add New → Project**.
3. Import the `OpenHire` repo.
4. In **Configure Project**, leave the framework preset as "Other" — no
   build command needed for a plain static file.
5. Deploy. Vercel picks up `outputDirectory: "pages"` from
   [`vercel.json`](vercel.json) and serves `pages/index.html`.

Every push to `main` auto-redeploys once connected (Vercel also gives you a
preview deployment per pull request, on by default).

## When the real frontend lands

Once the React app exists (e.g. under `frontend/` or `web/`), update
`vercel.json`:

- `outputDirectory` → the app's build output (e.g. `dist` or `.next`)
- `buildCommand` → the actual build command (e.g. `npm run build`), or drop
  it entirely and let Vercel auto-detect the framework

At that point `pages/` stays on Render as the standalone landing page (see
[`README.md`](README.md)), and Vercel serves the app instead.
