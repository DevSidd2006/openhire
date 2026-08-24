# Deploy

Render deployment config for the landing page in [`pages/`](../pages).

`render.yaml` defines a Render **Static Site** service (`openhire-landing`) that
publishes the `pages/` directory as-is — no build step, since it's plain HTML.

## Connect it on Render

1. Push this branch to GitHub (`origin/main` is already connected to
   `DevSidd2006/OpenHire`).
2. In the Render dashboard: **New → Blueprint**.
3. Select the `OpenHire` repo.
4. When prompted for the Blueprint file path, enter `deploy/render.yaml`
   (Render looks for `render.yaml` at the repo root by default — point it
   here instead).
5. Click **Apply**. Render creates the `openhire-landing` static site and
   deploys `pages/index.html`.

Alternatively, skip the Blueprint and create the service manually:
**New → Static Site** → connect the repo → leave **Build Command** empty →
set **Publish Directory** to `pages`.

Every push to `main` that touches `pages/` will auto-redeploy once connected.

Note: `pages/index.html` is currently a placeholder (empty body) — replace it
with the real landing page content whenever that's ready; no redeploy config
changes needed.
